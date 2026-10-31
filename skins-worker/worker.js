/**
 * Загрузка скинов MineForFriends by TEJKA (Cloudflare Worker).
 *
 * Принимает PNG от лаунчера и кладёт его в публичный репозиторий GitHub, откуда
 * скины читает мод CustomSkinLoader у всех игроков — без Radmin и без ПК хоста.
 *
 *   POST /upload   тело — PNG; заголовки X-Nick, X-Token, X-Model (default|slim)
 *                  ответ {"ok", "message"}
 *
 * В репозитории скинов:
 *   skins/<Ник>.json        профиль CustomSkinAPI {"username", "skins": {<модель>: <хэш>}}
 *   skins/textures/<хэш>    PNG скина
 *   owners/<ник>.json       {"nick", "token": sha256 токена} — кто занял ник
 * Первый, кто загрузил скин на ник, задаёт его токен; дальше менять скин этого ника
 * можно только с тем же токеном. Сбросить ник: удалить owners/<ник>.json в репозитории.
 *
 * Настройки Worker:
 *   SKINS_REPO     переменная, например TejkaFan/MineForFriends-skins
 *   GITHUB_TOKEN   секрет: fine-grained токен с правом Contents: Read and write
 *                  только на репозиторий скинов
 */
const NICK_RE = /^[A-Za-z0-9_]{3,16}$/;
const MAX_SKIN = 1 << 20;
const BRANCH = "main";
const PNG_SIGNATURE = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a];

export default {
  async fetch(request, env) {
    const {pathname} = new URL(request.url);
    if (request.method === "POST" && pathname === "/upload") {
      try {
        return await upload(request, env);
      } catch (e) {
        console.error(e);
        return reply(502, "GitHub сейчас недоступен, попробуй позже");
      }
    }
    if (request.method === "GET" && pathname === "/") return reply(200, "MineForFriends: загрузка скинов работает");
    return reply(404, "Не найдено");
  },
};

function reply(status, message) {
  return new Response(JSON.stringify({ok: status === 200, message}), {
    status, headers: {"Content-Type": "application/json; charset=utf-8"},
  });
}

function pngSize(bytes) {
  // Размер из заголовка IHDR
  if (bytes.length < 24 || PNG_SIGNATURE.some((v, i) => bytes[i] !== v)) return null;
  if (String.fromCharCode(...bytes.subarray(12, 16)) !== "IHDR") return null;
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  return [view.getUint32(16), view.getUint32(20)];
}

function validSkinSize(w, h) {
  // 64x64, старый 64x32 и HD-скины кратно 64 (до 1024)
  return w % 64 === 0 && w >= 64 && w <= 1024 && (h === w || h === w / 2);
}

async function sha256hex(bytes) {
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)].map(b => b.toString(16).padStart(2, "0")).join("");
}

function toBase64(bytes) {
  let s = "";
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(s);
}

function fromBase64Text(b64) {
  const bin = atob(b64.replace(/\s/g, ""));
  return new TextDecoder().decode(Uint8Array.from(bin, c => c.charCodeAt(0)));
}

async function github(env, method, path, body) {
  const r = await fetch(`https://api.github.com/repos/${env.SKINS_REPO}/${path}`, {
    method,
    headers: {
      "Authorization": `Bearer ${env.GITHUB_TOKEN}`,
      "Accept": "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "MineForFriends-skins",
      ...(body ? {"Content-Type": "application/json"} : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (method === "GET" && r.status === 404) return null;
  if (!r.ok) {
    const error = new Error(`GitHub ${method} ${path}: ${r.status} ${await r.text()}`);
    error.status = r.status;
    throw error;
  }
  return r.json();
}

async function upload(request, env) {
  const nick = request.headers.get("X-Nick") || "";
  const token = request.headers.get("X-Token") || "";
  const model = request.headers.get("X-Model") || "default";
  if (!NICK_RE.test(nick)) return reply(400, "Ник должен быть из 3–16 латинских букв, цифр или _");
  if (token.length < 16 || !["default", "slim"].includes(model)) return reply(400, "Неверный запрос");
  if (Number(request.headers.get("Content-Length") || 0) > MAX_SKIN) {
    return reply(413, "Файл скина слишком большой (максимум 1 МБ)");
  }
  const data = new Uint8Array(await request.arrayBuffer());
  if (!data.length || data.length > MAX_SKIN) return reply(413, "Файл скина слишком большой (максимум 1 МБ)");
  const size = pngSize(data);
  if (!size || !validSkinSize(...size)) {
    return reply(400, "Это не похоже на скин: нужен PNG 64×64 (или 64×32, или HD кратно 64)");
  }

  const key = nick.toLowerCase();
  const tokenHash = await sha256hex(new TextEncoder().encode(token));
  const texture = await sha256hex(data);

  // Всё — одним коммитом; если кто-то успел закоммитить раньше, собираем коммит заново
  for (let attempt = 0; attempt < 3; attempt++) {
    const ownerFile = await github(env, "GET", `contents/owners/${key}.json?ref=${BRANCH}`);
    const owner = ownerFile ? JSON.parse(fromBase64Text(ownerFile.content)) : null;
    if (owner && owner.token !== tokenHash) {
      return reply(403, "Этот ник уже занят другим игроком. Если это твой ник — попроси хоста сбросить его.");
    }
    const ref = await github(env, "GET", `git/ref/heads/${BRANCH}`);
    const head = await github(env, "GET", `git/commits/${ref.object.sha}`);
    const blob = await github(env, "POST", "git/blobs", {content: toBase64(data), encoding: "base64"});
    const tree = [
      {path: `skins/textures/${texture}`, mode: "100644", type: "blob", sha: blob.sha},
      {path: `skins/${nick}.json`, mode: "100644", type: "blob",
        content: JSON.stringify({username: nick, skins: {[model]: texture}})},
      {path: `owners/${key}.json`, mode: "100644", type: "blob", content: JSON.stringify({nick, token: tokenHash})},
    ];
    // Игрок сменил регистр букв в нике — профиль под старым написанием больше не нужен
    if (owner && owner.nick !== nick) tree.push({path: `skins/${owner.nick}.json`, mode: "100644", type: "blob", sha: null});
    const newTree = await github(env, "POST", "git/trees", {base_tree: head.tree.sha, tree});
    const commit = await github(env, "POST", "git/commits", {
      message: `Скин ${nick}`, tree: newTree.sha, parents: [ref.object.sha],
    });
    try {
      await github(env, "PATCH", `git/refs/heads/${BRANCH}`, {sha: commit.sha});
      return reply(200, "Скин загружен");
    } catch (e) {
      if (e.status !== 422) throw e; // 422 — ветка ушла вперёд, пробуем ещё раз
    }
  }
  return reply(503, "Сервер скинов занят, попробуй ещё раз");
}
