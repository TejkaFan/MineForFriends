/**
 * Загрузка скинов MineForFriends by TEJKA (Cloudflare Worker).
 *
 * Принимает PNG от лаунчера и кладёт его в публичный репозиторий GitHub, откуда
 * скины читает мод CustomSkinLoader у всех игроков — без Radmin и без ПК хоста.
 *
 *   POST /upload   тело — PNG скина; заголовки X-Nick, X-Token, X-Model (default|slim)
 *   POST /cape     тело — PNG плаща (пустое — убрать плащ); заголовки X-Nick, X-Token
 *                  Отдельный адрес, а не заголовок: старый Worker на плащ ответит 404, а не
 *                  примет его за скин 64x32.
 *                  ответ {"ok", "message"}
 *
 * В репозитории скинов:
 *   skins/<Ник>.json        профиль CustomSkinAPI {"username", "skins": {<модель>: <хэш>}, "cape": <хэш>}
 *   skins/textures/<хэш>    PNG скина или плаща
 *   owners/<ник>.json       {"nick", "token": sha256 токена} — кто занял ник
 * Первый, кто загрузил скин на ник, задаёт его токен; дальше менять скин этого ника
 * можно только с тем же токеном. Сбросить ник: удалить owners/<ник>.json в репозитории.
 *
 *   POST /report   тело — JSON {kind, title, signature, version, build, os, text}: отчёт об
 *                  ошибке лаунчера. Становится задачей (issue) в закрытом репозитории отчётов;
 *                  повтор той же ошибки (та же signature) — комментарием к открытой задаче.
 *
 *   POST /recipe   рецепт сборки: первая строка тела — JSON рецепта, дальше (по желанию) zip конфигов
 *                  в base64; заголовки X-Nick, X-Token. Становится Pull Request в репозиторий скинов:
 *                  recipes/<id>/recipe.json, files.zip, owner.json. Сборка появится у всех, когда
 *                  автор лаунчера примет PR (Merge). Моды в рецепте — только ссылки на Modrinth.
 *   GET  /recipes  принятые рецепты: [{id, sha}] — сами recipe.json лаунчер берёт с raw.githubusercontent.com
 *
 * Настройки Worker:
 *   SKINS_REPO     переменная, например TejkaFan/MineForFriends-skins
 *   REPORTS_REPO   переменная, по умолчанию TejkaFan/MineForFriends-reports
 *   GITHUB_TOKEN   секрет: fine-grained токен с правами Contents: Read and write и Pull requests:
 *                  Read and write на репозиторий скинов и Issues: Read and write на репозиторий отчётов
 *   LIMITS         привязка KV namespace (по желанию): счётчики защиты от спама. Без неё
 *                  лимиты считаются только в памяти Worker — слабее, но тоже работает.
 *
 * Защита от спама: не больше LIMITS_RULES запросов с одного IP и всего за сутки. Сверх лимита
 * GitHub не трогаем, а отвечаем 429. Одна и та же ошибка дописывается в задачу не больше
 * MAX_COMMENTS раз.
 */
const NICK_RE = /^[A-Za-z0-9_]{3,16}$/;
const MAX_SKIN = 1 << 20;
const MAX_REPORT = 64 * 1024;
const DEFAULT_REPORTS_REPO = "TejkaFan/MineForFriends-reports";
const REPORT_KINDS = {crash: "сбой", error: "ошибка", js: "ошибка интерфейса", manual: "отчёт игрока",
                      game: "вылет игры"};
const BRANCH = "main";
const PNG_SIGNATURE = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a];
const MINUTE = 60, HOUR = 3600, DAY = 86400;
// [кто, сколько, за сколько секунд]: ip — один адрес, all — все вместе
const LIMITS_RULES = {
  upload: [["ip", 6, 10 * MINUTE], ["ip", 30, DAY], ["all", 300, DAY]],
  report: [["ip", 10, HOUR], ["ip", 40, DAY], ["all", 200, DAY]],
  recipe: [["ip", 5, DAY], ["all", 40, DAY]],
};
const MAX_COMMENTS = 50;
const memory = new Map(); // счётчики, пока жив этот экземпляр Worker

export default {
  async fetch(request, env) {
    const {pathname} = new URL(request.url);
    const action = {"/upload": "upload", "/cape": "upload", "/report": "report", "/recipe": "recipe"}[pathname];
    if (request.method === "POST" && action) {
      const wait = await limited(env, action, request.headers.get("CF-Connecting-IP") || "?");
      if (wait) return reply(429, `Слишком много запросов, попробуй через ${wait}`);
    }
    if (request.method === "POST" && action === "upload") {
      try {
        return await upload(request, env, pathname === "/cape");
      } catch (e) {
        console.error(e);
        return reply(502, "GitHub сейчас недоступен, попробуй позже");
      }
    }
    if (request.method === "POST" && pathname === "/report") {
      try {
        return await report(request, env);
      } catch (e) {
        console.error(e);
        return reply(502, "Не удалось сохранить отчёт, GitHub недоступен");
      }
    }
    if (request.method === "POST" && pathname === "/recipe") {
      try {
        return await recipe(request, env);
      } catch (e) {
        console.error(e);
        return reply(502, "GitHub сейчас недоступен, попробуй позже");
      }
    }
    if (request.method === "GET" && pathname === "/recipes") {
      try {
        return await recipes(env);
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

function waitText(seconds) {
  if (seconds < HOUR) return `${Math.max(1, Math.ceil(seconds / MINUTE))} мин`;
  return `${Math.ceil(seconds / HOUR)} ч`;
}

async function limited(env, action, ip) {
  // Окна фиксированные: счётчик сбрасывается в начале каждого периода.
  // Ответ — сколько ждать (текст) или null, если запрос можно пропустить.
  const now = Math.floor(Date.now() / 1000);
  const keys = LIMITS_RULES[action].map(([who, max, period]) => {
    const window = Math.floor(now / period);
    return {key: `${action}:${who === "ip" ? ip : "all"}:${period}:${window}`, max, period,
            left: (window + 1) * period - now};
  });
  const counts = await Promise.all(keys.map(async k => {
    const stored = env.LIMITS ? Number(await env.LIMITS.get(k.key)) || 0 : 0;
    return Math.max(stored, memory.get(k.key) || 0);
  }));
  const over = keys.filter((k, i) => counts[i] >= k.max);
  if (over.length) return waitText(Math.max(...over.map(k => k.left)));
  if (memory.size > 10000) memory.clear();
  keys.forEach((k, i) => memory.set(k.key, counts[i] + 1));
  if (env.LIMITS) {
    try {
      // KV хранит не меньше 60 секунд
      await Promise.all(keys.map((k, i) => env.LIMITS.put(k.key, String(counts[i] + 1),
                                                          {expirationTtl: Math.max(60, k.left + 60)})));
    } catch (e) {
      console.error(e); // кончилась квота записей KV — лучше отказать, чем пустить спам
      return waitText(HOUR);
    }
  }
  return null;
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

function validCapeSize(w, h) {
  // 64x32 и HD-плащи кратно 64, плюс самый старый формат 22x17
  return (w % 64 === 0 && w >= 64 && w <= 1024 && h === w / 2) || (w === 22 && h === 17);
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

async function github(env, method, path, body, repo = env.SKINS_REPO) {
  const r = await fetch(`https://api.github.com/repos/${repo}/${path}`, {
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

async function upload(request, env, cape) {
  const nick = request.headers.get("X-Nick") || "";
  const token = request.headers.get("X-Token") || "";
  const model = request.headers.get("X-Model") || "default";
  if (!NICK_RE.test(nick)) return reply(400, "Ник должен быть из 3–16 латинских букв, цифр или _");
  if (token.length < 16 || !["default", "slim"].includes(model)) {
    return reply(400, "Неверный запрос");
  }
  const what = cape ? "плаща" : "скина";
  if (Number(request.headers.get("Content-Length") || 0) > MAX_SKIN) {
    return reply(413, `Файл ${what} слишком большой (максимум 1 МБ)`);
  }
  const data = new Uint8Array(await request.arrayBuffer());
  if (data.length > MAX_SKIN) return reply(413, `Файл ${what} слишком большой (максимум 1 МБ)`);
  if (!data.length && !cape) return reply(400, "Пустой файл скина");
  if (data.length) {
    const size = pngSize(data);
    if (cape && (!size || !validCapeSize(...size))) {
      return reply(400, "Это не похоже на плащ: нужен PNG 64×32 (или HD кратно 64×32)");
    }
    if (!cape && (!size || !validSkinSize(...size))) {
      return reply(400, "Это не похоже на скин: нужен PNG 64×64 (или 64×32, или HD кратно 64)");
    }
  }

  const key = nick.toLowerCase();
  const tokenHash = await sha256hex(new TextEncoder().encode(token));
  const texture = data.length ? await sha256hex(data) : null;

  // Всё — одним коммитом; если кто-то успел закоммитить раньше, собираем коммит заново
  for (let attempt = 0; attempt < 3; attempt++) {
    const ownerFile = await github(env, "GET", `contents/owners/${key}.json?ref=${BRANCH}`);
    const owner = ownerFile ? JSON.parse(fromBase64Text(ownerFile.content)) : null;
    if (owner && owner.token !== tokenHash) {
      return reply(403, "Этот ник уже занят другим игроком. Если это твой ник — попроси хоста сбросить его.");
    }
    // Скин и плащ меняются по отдельности: вторую текстуру берём из старого профиля
    const profileFile = owner && await github(env, "GET", `contents/skins/${owner.nick}.json?ref=${BRANCH}`);
    const old = profileFile ? JSON.parse(fromBase64Text(profileFile.content)) : {};
    const profile = {username: nick, skins: cape ? old.skins || {} : {[model]: texture}};
    const capeHash = cape ? texture : old.cape;
    if (capeHash) profile.cape = capeHash;

    const ref = await github(env, "GET", `git/ref/heads/${BRANCH}`);
    const head = await github(env, "GET", `git/commits/${ref.object.sha}`);
    const tree = [
      {path: `skins/${nick}.json`, mode: "100644", type: "blob", content: JSON.stringify(profile)},
      {path: `owners/${key}.json`, mode: "100644", type: "blob", content: JSON.stringify({nick, token: tokenHash})},
    ];
    if (texture) {
      const blob = await github(env, "POST", "git/blobs", {content: toBase64(data), encoding: "base64"});
      tree.push({path: `skins/textures/${texture}`, mode: "100644", type: "blob", sha: blob.sha});
    }
    // Игрок сменил регистр букв в нике — профиль под старым написанием больше не нужен
    if (owner && owner.nick !== nick) tree.push({path: `skins/${owner.nick}.json`, mode: "100644", type: "blob", sha: null});
    const newTree = await github(env, "POST", "git/trees", {base_tree: head.tree.sha, tree});
    const done = !cape ? "Скин загружен" : texture ? "Плащ загружен" : "Плащ убран";
    const commit = await github(env, "POST", "git/commits", {
      message: `${cape ? "Плащ" : "Скин"} ${nick}`, tree: newTree.sha, parents: [ref.object.sha],
    });
    try {
      await github(env, "PATCH", `git/refs/heads/${BRANCH}`, {sha: commit.sha});
      return reply(200, done);
    } catch (e) {
      if (e.status !== 422) throw e; // 422 — ветка ушла вперёд, пробуем ещё раз
    }
  }
  return reply(503, "Сервер скинов занят, попробуй ещё раз");
}

function clean(value, max) {
  return String(value ?? "").replace(/[\r\n\t]+/g, " ").trim().slice(0, max);
}

async function report(request, env) {
  if (Number(request.headers.get("Content-Length") || 0) > MAX_REPORT * 4) return reply(413, "Отчёт слишком большой");
  const raw = await request.text();
  if (raw.length > MAX_REPORT) return reply(413, "Отчёт слишком большой");
  let data;
  try {
    data = JSON.parse(raw);
  } catch {
    return reply(400, "Неверный отчёт");
  }
  const signature = String(data?.signature || "");
  const text = String(data?.text || "").slice(0, MAX_REPORT);
  if (!/^[0-9a-f]{12}$/.test(signature) || !text) return reply(400, "Неверный отчёт");

  const kind = REPORT_KINDS[data.kind] || REPORT_KINDS.error;
  const title = `[${signature}] ${kind}: ${clean(data.title, 100) || "без описания"}`;
  const meta = `**Версия:** ${clean(data.version, 30)} (сборка ${clean(data.build, 20)}) · **Windows:** ${clean(data.os, 60)}`;
  // Пять обратных кавычек: в логе могут встретиться тройные
  const body = `${meta}\n\n\`\`\`\`\`text\n${text}\n\`\`\`\`\`\n`;

  // Та же ошибка уже открыта — дописываем комментарием, чтобы не плодить задачи
  const repo = env.REPORTS_REPO || DEFAULT_REPORTS_REPO;
  const open = await github(env, "GET", "issues?state=open&per_page=100", null, repo) || [];
  const same = open.find(i => !i.pull_request && i.title.startsWith(`[${signature}]`));
  if (same && same.comments >= MAX_COMMENTS) return reply(200, "Эта ошибка уже известна автору");
  if (same) {
    await github(env, "POST", `issues/${same.number}/comments`, {body}, repo);
  } else {
    await github(env, "POST", "issues", {title, body}, repo);
  }
  return reply(200, "Отчёт отправлен автору");
}

// ---------- Рецепты сборок ----------

const RECIPE_ID_RE = /^[a-z0-9][a-z0-9_-]{1,39}$/;
const RECIPE_LOADERS = ["vanilla", "fabric", "quilt", "forge", "neoforge"];
const RECIPE_PATH_RE = /^(mods|resourcepacks|shaderpacks)\/[^/\\]{1,200}$/;
const MODRINTH_CDN = "https://cdn.modrinth.com/data/";
const MAX_RECIPE = 7 << 20; // JSON + zip конфигов в base64 (4 МБ zip ≈ 5.4 МБ текста)
const MAX_RECIPE_JSON = 512 * 1024;

function checkRecipe(r) {
  // Ответ — текст ошибки или null. В рецепт попадает только то, что здесь проверено
  if (!r || typeof r !== "object") return "Неверный рецепт";
  if (!RECIPE_ID_RE.test(String(r.id))) return "Неверный id сборки";
  if (!String(r.name || "").trim() || String(r.name).length > 60) return "Название — от 1 до 60 символов";
  if (String(r.description || "").length > 500) return "Описание — до 500 символов";
  if (!/^[0-9a-z.\-_ ]{2,30}$/i.test(String(r.minecraft))) return "Неверная версия Minecraft";
  if (!RECIPE_LOADERS.includes(r.loader)) return "Неизвестный загрузчик";
  if (String(r.loader_version || "").length > 60) return "Неверная версия загрузчика";
  if (!Array.isArray(r.mods) || r.mods.length > 800) return "Слишком много модов";
  for (const m of r.mods) {
    if (!m || !RECIPE_PATH_RE.test(String(m.path)) || !String(m.url).startsWith(MODRINTH_CDN)
        || !/^[0-9a-f]{40}$/.test(String(m.sha1)) || String(m.title || "").length > 100) {
      return `Мод ${String(m?.path).slice(0, 80)}: только файлы с Modrinth`;
    }
  }
  if (!Array.isArray(r.missing || []) || (r.missing || []).length > 800) return "Неверный список модов без Modrinth";
  return null;
}

async function recipe(request, env) {
  const nick = request.headers.get("X-Nick") || "";
  const token = request.headers.get("X-Token") || "";
  if (!NICK_RE.test(nick) || token.length < 16) return reply(400, "Нужен ник (как в TLauncher) — он будет автором сборки");
  if (Number(request.headers.get("Content-Length") || 0) > MAX_RECIPE) return reply(413, "Конфиги сборки слишком большие");
  const raw = await request.text();
  if (raw.length > MAX_RECIPE) return reply(413, "Конфиги сборки слишком большие");
  const cut = raw.indexOf("\n");
  const head = cut < 0 ? raw : raw.slice(0, cut);
  const files = cut < 0 ? "" : raw.slice(cut + 1).trim();
  if (head.length > MAX_RECIPE_JSON) return reply(413, "Рецепт слишком большой");
  let r;
  try {
    r = JSON.parse(head);
  } catch {
    return reply(400, "Неверный рецепт");
  }
  const bad = checkRecipe(r);
  if (bad) return reply(400, bad);

  const entry = {
    id: r.id, name: clean(r.name, 60), description: String(r.description || "").trim().slice(0, 500),
    author: nick, minecraft: r.minecraft, loader: r.loader, loader_version: String(r.loader_version || ""),
    mods: r.mods.map(m => ({path: m.path, url: m.url, sha1: m.sha1, size: Number(m.size) || 0,
                            title: clean(m.title, 100), project: clean(m.project, 20)})),
    missing: (r.missing || []).map(x => clean(x, 120)),
    files: Boolean(files), updated: new Date().toISOString().slice(0, 19) + "Z",
  };
  const dir = `recipes/${entry.id}`;
  const tokenHash = await sha256hex(new TextEncoder().encode(token));
  const existing = await github(env, "GET", `contents/${dir}?ref=${BRANCH}`) || [];
  const ownerFile = existing.find(f => f.name === "owner.json")
    && await github(env, "GET", `contents/${dir}/owner.json?ref=${BRANCH}`);
  const owner = ownerFile ? JSON.parse(fromBase64Text(ownerFile.content)) : null;
  if (owner && owner.token !== tokenHash) {
    return reply(403, `Сборка «${entry.id}» уже есть у ${owner.nick}. Назови свою по-другому.`);
  }

  const ref = await github(env, "GET", `git/ref/heads/${BRANCH}`);
  const head_ = await github(env, "GET", `git/commits/${ref.object.sha}`);
  const tree = [
    {path: `${dir}/recipe.json`, mode: "100644", type: "blob", content: JSON.stringify(entry, null, 1)},
    {path: `${dir}/owner.json`, mode: "100644", type: "blob", content: JSON.stringify({nick, token: tokenHash})},
  ];
  if (files) {
    // base64 уходит в GitHub как есть — Worker его не разбирает (на бесплатном тарифе мало времени CPU)
    const blob = await github(env, "POST", "git/blobs", {content: files, encoding: "base64"});
    tree.push({path: `${dir}/files.zip`, mode: "100644", type: "blob", sha: blob.sha});
  } else if (existing.some(f => f.name === "files.zip")) {
    tree.push({path: `${dir}/files.zip`, mode: "100644", type: "blob", sha: null});
  }
  const newTree = await github(env, "POST", "git/trees", {base_tree: head_.tree.sha, tree});
  const what = owner ? "Обновление сборки" : "Сборка";
  const commit = await github(env, "POST", "git/commits", {
    message: `${what} «${entry.name}» от ${nick}`, tree: newTree.sha, parents: [ref.object.sha],
  });
  const branch = `recipe/${entry.id}-${Date.now()}`;
  await github(env, "POST", "git/refs", {ref: `refs/heads/${branch}`, sha: commit.sha});
  // @ник в тексте заявки GitHub превратит в упоминание — разрываем его невидимым пробелом
  const md = s => String(s).replace(/@/g, "@​");
  const list = entry.mods.slice(0, 150).map(m => `- ${md(m.title || m.path)}`).join("\n");
  const body = [
    `**Автор:** ${nick} · **Minecraft** ${entry.minecraft} · **${entry.loader}** ${md(entry.loader_version)}`,
    entry.description && `> ${md(entry.description).replace(/\n/g, "\n> ")}`,
    `**Модов и ресурспаков с Modrinth:** ${entry.mods.length}` + (entry.mods.length > 150 ? " (ниже первые 150)" : ""),
    list,
    entry.missing.length && `**Нет на Modrinth (у друзей их не будет):** ${md(entry.missing.slice(0, 50).join(", "))}`,
    files ? `**Конфиги:** files.zip, ${(files.length * 3 / 4 / 2 ** 20).toFixed(1)} МБ — загляни внутрь, если сомневаешься` : "**Конфигов нет**",
    "Принять — **Merge pull request**: сборка появится у всех в лаунчере. Отклонить — **Close**.",
  ].filter(Boolean).join("\n\n");
  const pr = await github(env, "POST", "pulls", {title: `${what} «${md(entry.name)}» от ${nick}`, head: branch, base: BRANCH, body});
  return reply(200, `Сборка отправлена на проверку (заявка №${pr.number}). Она появится у всех, когда автор лаунчера её одобрит.`);
}

async function recipes(env) {
  const tree = await github(env, "GET", `git/trees/${BRANCH}?recursive=1`);
  const items = (tree?.tree || [])
    .map(t => [t, /^recipes\/([a-z0-9][a-z0-9_-]{1,39})\/recipe\.json$/.exec(t.path)])
    .filter(([, m]) => m)
    .map(([t, m]) => ({id: m[1], sha: t.sha}));
  return new Response(JSON.stringify({ok: true, recipes: items}), {
    headers: {"Content-Type": "application/json; charset=utf-8"},
  });
}
