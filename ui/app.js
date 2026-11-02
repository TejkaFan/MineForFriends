// Интерфейс лаунчера. Логика живёт в Python (класс Api в launcher.py):
// сюда приходят события через app.emit(), отсюда — вызовы pywebview.api.*
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const api = () => window.pywebview.api;

const TITLES = {play: "Наши серверы", custom: "Свои сборки", server: "Сервер", skin: "Скин", shots: "Скриншоты"};
const state = {packs: [], cloud: null, selected: [], ready: false, busy: false, profiles: [], servers: []};

function icon(name) {
  return `<svg class="i" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${ICONS[name] || ""}</svg>`;
}

function paintIcons(root = document) {
  for (const el of $$("[data-icon]", root)) {
    if (!el.dataset.painted) {
      el.insertAdjacentHTML("afterbegin", icon(el.dataset.icon));
      el.dataset.painted = "1";
    }
  }
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
}

function errText(e) {
  return (e && (e.message || String(e))) || "Неизвестная ошибка";
}

// ---------- Диалоги ----------

function dialog({title, text = "", html = null, buttons = [{label: "Понятно", value: true, primary: true}], wide = false}) {
  return new Promise(resolve => {
    $("#modal-title").textContent = title;
    const body = $("#modal-body");
    body.classList.toggle("form", html !== null);
    if (html !== null) body.innerHTML = html; else body.textContent = text;
    $(".dialog").classList.toggle("wide", wide);
    const actions = $("#modal-actions");
    actions.innerHTML = "";
    for (const b of buttons) {
      const el = document.createElement("button");
      el.className = "btn" + (b.primary ? " primary" : "");
      el.textContent = b.label;
      el.onclick = async () => {
        // check() держит окно открытым, пока данные не прошли проверку
        if (b.check) {
          el.disabled = true;
          const ok = await b.check(body);
          el.disabled = false;
          if (!ok) return;
        }
        $("#modal").classList.add("hidden");
        resolve(b.value);
      };
      actions.appendChild(el);
    }
    paintIcons(body);
    $("#modal").classList.remove("hidden");
  });
}

const showInfo = (title, text) => dialog({title, text});
// Сбои лаунчер отправляет автору сам; остальные ошибки игрок может отправить кнопкой
async function showError(text) {
  const sent = text.includes("Отчёт об ошибке уже отправлен");
  const choice = await dialog({title: "Что-то пошло не так", text, buttons: sent
    ? [{label: "Понятно", value: null, primary: true}]
    : [{label: "Отправить отчёт", value: "report"}, {label: "Понятно", value: null, primary: true}]});
  if (choice === "report") sendReport(text);
}

async function sendReport(text) {
  let message;
  try {
    message = await api().send_report(text);
  } catch (e) {
    message = errText(e);
  }
  showInfo("Отчёт", message);
}

// Ошибки самого интерфейса. Ошибки из Python (RuntimeError) уже показаны игроку или отправлены
function reportJs(error, fallback) {
  if (!window.pywebview?.api?.report_js || error?.name === "RuntimeError") return;
  // Событие вместо ошибки (не загрузилась картинка и т. п.) — «[object Event]» в отчёте ничего не скажет
  if (error instanceof Event) error = {message: `событие ${error.type} от ${error.target?.tagName || "?"}`};
  api().report_js(String(error?.message || fallback || error), String(error?.stack || "")).catch(() => {});
}
window.addEventListener("error", e => reportJs(e.error, `${e.message} (${e.filename}:${e.lineno})`));
window.addEventListener("unhandledrejection", e => reportJs(e.reason));
const confirmBox = (title, text, yes) =>
  dialog({title, text, buttons: [{label: "Отмена", value: false}, {label: yes, value: true, primary: true}]});

// ---------- Навигация и общие элементы ----------

function go(page) {
  for (const b of $$(".nav[data-page]")) b.classList.toggle("active", b.dataset.page === page);
  for (const p of $$(".page")) p.classList.toggle("active", p.id === "page-" + page);
  $("#page-title").textContent = TITLES[page];
  // Списки могли поменяться на соседних вкладках
  if (page === "custom") loadProfiles();
  if (page === "server") loadServerTab();
  if (page === "shots") loadShots();
  // 3D-модель рисуется только на своей вкладке, чтобы не грузить видеокарту зря
  if (skin.viewer) skin.viewer.renderPaused = page !== "skin";
}

function setStatus(scope, text) {
  const el = $(`#${scope}-status`);
  if (el) el.textContent = text;
  $(`#${scope}-progress`)?.classList.remove("hidden");
}

function setProgress(scope, value) {
  const bar = $(`#${scope}-bar`);
  if (bar) bar.style.width = Math.round(Math.max(0, Math.min(1, value)) * 100) + "%";
}

function hideProgress(scope) {
  $(`#${scope}-progress`)?.classList.add("hidden");
}

// Голова со скина: лицо (8,8) и слой шапки (40,8)
const STEVE = ["hhhhhhhh", "hhssssss", "ssssssss", "swbssbws", "sssnnsss", "ssmmmmss", "ssssssss", "ssssssss"];
const STEVE_COLORS = {h: "#3b2a1a", s: "#c99a6e", w: "#ffffff", b: "#3a5fbf", n: "#a8714a", m: "#7a4a2e"};

function drawFace(canvas, b64) {
  const ctx = canvas.getContext("2d");
  ctx.imageSmoothingEnabled = false;
  ctx.clearRect(0, 0, 8, 8);
  if (!b64) {
    STEVE.forEach((row, y) => [...row].forEach((c, x) => {
      ctx.fillStyle = STEVE_COLORS[c];
      ctx.fillRect(x, y, 1, 1);
    }));
    return;
  }
  const img = new Image();
  img.onload = () => {
    ctx.drawImage(img, 8, 8, 8, 8, 0, 0, 8, 8);
    ctx.drawImage(img, 40, 8, 8, 8, 0, 0, 8, 8);
  };
  img.src = "data:image/png;base64," + b64;
}

function setNick(nick) {
  $("#chip-nick").textContent = nick || "Без ника";
}

// ---------- Наши серверы ----------

function renderServers(d) {
  Object.assign(state, {packs: d.packs, cloud: d.cloud, selected: d.selected});
  state.radmin = d.radmin ?? state.radmin;  // toggleCard перерисовывает без свежей проверки
  showRadmin(state.radmin);
  const line = $("#cloud-line");
  if (!d.cloud && !d.packs.length) {
    line.className = "line err";
    line.textContent = "Не удалось получить список наших сборок. Проверь интернет и попробуй чуть позже.";
    $("#cards").innerHTML = "";
    updatePlay();
    return;
  }
  line.className = "line muted";
  line.textContent = d.cloud
    ? "Нажми на карточку, чтобы ставить и обновлять эту сборку. Индикатор обновляется сам."
    : "Список наших сборок недоступен — показаны серверы, запущенные через лаунчер в Radmin.";
  $("#cards").innerHTML = d.packs.map(p => {
    const st = p.status;
    const on = state.selected.includes(p.id);
    const size = p.size ? `${Math.round(p.size / 2 ** 20)} МБ` : "";
    const fill = st && st.max ? Math.min(100, Math.round(st.online / st.max * 100)) : 0;
    const who = st
      ? `<div class="who online">${icon("users")}Игроков: ${st.online} / ${st.max}</div>`
      : `<div class="who off">${icon("moon")}${p.kind === "own" ? "Сервер не запущен" : "Хост сейчас не играет"}</div>`;
    return `<div class="card${on ? " on" : ""}" data-id="${esc(p.id)}">
      <div class="top"><span class="name">${esc(p.name)}</span><span class="badge${st ? " online" : ""}">${st ? "онлайн" : "выключен"}</span></div>
      <div class="desc">${esc([p.description, size].filter(Boolean).join(" · "))}</div>
      ${who}
      <div class="fill"><div style="width:${fill}%"></div></div>
      <div class="pick"><span class="tick">${icon("check")}</span>${on ? "Ставится и обновляется" : "Не ставится"}
        ${p.kind === "own" ? "" : `<button class="mem" data-check="${esc(p.id)}" title="Проверить связь с сервером">${icon("wifi")}</button>`}
        <button class="mem" data-mem="${esc(p.id)}" title="Память для игры в этой сборке">${icon("cpu")}${p.memory} ГБ</button></div>
    </div>`;
  }).join("");
  updatePlay();
}

function updatePlay() {
  const chosen = state.packs.filter(p => state.selected.includes(p.id)).map(p => `«${p.name}»`);
  $("#play-btn").disabled = !state.ready || !chosen.length || state.busy || !state.tlauncher;
  if (!state.ready || state.busy) return;
  $("#play-hint").textContent = !state.tlauncher ? "Сначала установи TLauncher"
    : !state.packs.length ? "Нет списка сборок"
    : chosen.length ? `Обновит ${chosen.join(", ")} и откроет TLauncher`
    : "Отметь хотя бы одну сборку";
}

async function toggleCard(id) {
  if (state.busy) return;
  const ids = state.selected.includes(id) ? state.selected.filter(x => x !== id) : [...state.selected, id];
  if (!ids.length) return showInfo("Нужна хотя бы одна сборка", "Отметь хотя бы одну сборку, которую ставить и обновлять.");
  await api().set_selected(ids);
  renderServers({packs: state.packs, cloud: state.cloud, selected: ids});
}

async function play() {
  state.busy = true;
  updatePlay();
  setProgress("play", 0);
  setStatus("play", "Связь с сервером…");
  try {
    const message = await api().play();
    setStatus("play", message);
    $("#play-hint").textContent = "Лаунчер сейчас закроется";
  } catch (e) {
    setStatus("play", "Ошибка, попробуй ещё раз");
    state.busy = false;
    updatePlay();
    showError(errText(e));
  }
}

// Без Radmin VPN не видно ни наших серверов, ни серверов друзей
function showRadmin(r) {
  const off = r && !r.on;
  $("#radmin-notice").classList.toggle("hidden", !off);
  if (!off) return;
  $("#radmin-title").textContent = r.installed ? "Radmin VPN выключен" : "Radmin VPN не установлен";
  $("#radmin-text").textContent = r.installed
    ? "Запусти Radmin VPN и зайди в нашу сеть — без него не видно серверов и не скачать сборки."
    : "Через Radmin VPN мы играем вместе: поставь его и зайди в нашу сеть (название и пароль спроси у хоста).";
  $("#radmin-btn").innerHTML = `${icon(r.installed ? "player-play" : "cloud-download")}${r.installed ? "Запустить Radmin" : "Скачать Radmin VPN"}`;
}

// Серверы друзей находятся сами через Radmin; адрес вручную — если поиск по сети не дошёл
async function friendHosts() {
  const hosts = await api().friend_hosts();
  const save = async body => {
    try {
      await api().save_friend_hosts($("#friend-hosts", body).value.split(/[\s,;]+/));
      return true;
    } catch (e) {
      $("#friend-err", body).textContent = errText(e);
      return false;
    }
  };
  const html = `<p class="muted">Серверы, запущенные через лаунчер, появляются здесь сами, если ты и хост в одной сети Radmin.
      Если сервер друга не появился — впиши его адрес Radmin (виден у друга в Radmin VPN), по одному в строке.</p>
    <textarea id="friend-hosts" placeholder="26.123.45.67">${esc(hosts.join("\n"))}</textarea>
    <div class="hint" id="friend-err"></div>`;
  const ok = await dialog({title: "Серверы друзей", html,
    buttons: [{label: "Отмена", value: false}, {label: "Сохранить", value: true, primary: true, check: save}]});
  if (ok) {
    $("#cloud-line").textContent = "Ищу серверы друзей…";
    api().refresh_servers();
  }
}

function showTlauncher(installed) {
  state.tlauncher = installed;
  $("#tl-notice").classList.toggle("hidden", installed);
  updatePlay();
}

function installTlauncher() {
  api().install_tlauncher();
  $("#tl-text").textContent = "Открыл сайт TLauncher в браузере: нажми «Download TL» (Скачать), запусти скачанный " +
    "установщик и пройди установку. Как только TLauncher установится, эта плашка исчезнет сама.";
  $("#tl-install").lastChild.textContent = "Открыть сайт ещё раз";
}

// ---------- Свои сборки ----------

let plan = null;
let loaderToken = 0;

function currentMc() {
  return $("input[name=mc-mode]:checked").value === "manual" ? $("#plan-mc").value : plan.minecraft;
}

function setCustomBusy(busy) {
  for (const el of [$("#pick-dir"), $("#pick-zip")]) el.disabled = busy;
  for (const el of $$("#plan input, #plan select, #plan button")) el.disabled = busy;
  if (!busy && plan) {
    $("#plan-mc").disabled = $("input[name=mc-mode]:checked").value !== "manual";
    $("#plan-lver").disabled = $("#plan-loader").value === "vanilla";
  }
}

async function pickPack(kind) {
  const path = await api()[kind === "dir" ? "pick_folder" : "pick_archive"]();
  if (!path) return;
  plan = null;
  $("#plan").classList.add("hidden");
  setCustomBusy(true);
  setProgress("custom", 0);
  setStatus("custom", "Разбираю сборку…");
  try {
    plan = await api().analyze(path);
    showPlan();
    setStatus("custom", "Проверь данные и нажми «Добавить в TLauncher».");
  } catch (e) {
    hideProgress("custom");
    showError("Не удалось разобрать сборку:\n\n" + errText(e));
  }
  setCustomBusy(false);
}

function showPlan() {
  $("#plan-name").value = plan.name;
  $("#plan-auto").textContent = `${plan.minecraft || "—"} (${plan.mc_source})`;
  $("input[name=mc-mode][value=auto]").checked = true;
  $("#plan-mc").innerHTML = plan.releases.map(v => `<option>${esc(v)}</option>`).join("");
  $("#plan-mc").value = plan.minecraft || "";
  $("#plan-loader").value = plan.loader;
  $("#plan-info").textContent = plan.info;
  $("#plan").classList.remove("hidden");
  refreshLoader();
}

async function refreshLoader() {
  if (!plan) return;
  const loader = $("#plan-loader").value, mc = currentMc();
  const token = ++loaderToken;
  // Предупреждение, если на выбранной версии часть модов не запустится
  const fit = await api().fit_count(mc);
  $("#plan-fit").textContent = `На ${mc} подойдут модов: ${fit.fits} из ${fit.total}`;
  $("#plan-fit").classList.toggle("hidden", fit.fits >= fit.total);

  const sel = $("#plan-lver");
  if (loader === "vanilla") {
    sel.innerHTML = `<option value="">—</option>`;
    sel.disabled = true;
    return;
  }
  sel.innerHTML = `<option value="loading">загружаю…</option>`;
  sel.disabled = true;
  const versions = await api().loader_versions(loader, mc);
  if (token !== loaderToken) return; // пока грузили, игрок выбрал другое
  if (versions && !versions.length) {
    sel.innerHTML = `<option value="none">нет для ${esc(mc)}</option>`;
    sel.disabled = false;
    return;
  }
  const list = versions || [];
  const same = loader === plan.loader && mc === plan.minecraft && plan.loader_version;
  if (same && !list.includes(plan.loader_version)) list.unshift(plan.loader_version);
  sel.innerHTML = `<option value="">авто (последняя)</option>` + list.map(v => `<option>${esc(v)}</option>`).join("");
  sel.value = same ? plan.loader_version : "";
  sel.disabled = false;
}

async function addPack() {
  const name = $("#plan-name").value.trim(), mc = currentMc(), loader = $("#plan-loader").value;
  const lv = $("#plan-lver").value;
  if (!name || !mc) return showInfo("Не хватает данных", "Заполни название и выбери версию Minecraft.");
  if (lv === "loading") return;
  if (lv === "none") {
    const title = $("#plan-loader").selectedOptions[0].textContent;
    return showInfo("Нет такой версии", `${title} для Minecraft ${mc} не существует — выбери другую версию.`);
  }
  setCustomBusy(true);
  setProgress("custom", 0);
  setStatus("custom", "Готовлю установку…");
  try {
    const res = await api().install_custom(name, mc, loader, lv || null);
    setStatus("custom", `Готово: «${res.version_id}» добавлена в TLauncher.`);
    loadProfiles();
    showInfo("Сборка добавлена", res.text);
  } catch (e) {
    setStatus("custom", "Ошибка, попробуй ещё раз");
    showError("Не удалось добавить сборку:\n\n" + errText(e));
  }
  setCustomBusy(false);
}

async function loadProfiles() {
  state.profiles = await api().profile_list();
  $("#profiles").innerHTML = state.profiles.length ? state.profiles.map((p, i) => `
    <div class="item"><div class="grow"><div class="name">${esc(p.name)}</div><div class="meta">${esc(p.minecraft)} · ${esc(p.loader_name)}</div></div>
      <div class="tools">
        <button class="iconbtn go" data-act="play" data-i="${i}" title="Играть: выставит память и откроет TLauncher">${icon("player-play")}</button>
        <button class="iconbtn" data-act="memory" data-i="${i}" title="Память для игры">${icon("cpu")}</button>
        ${p.loader === "vanilla" ? "" : `<button class="iconbtn" data-act="updates" data-i="${i}" title="Обновить моды с Modrinth">${icon("refresh")}</button>`}
        <button class="iconbtn" data-act="share" data-i="${i}" title="Поделиться сборкой с друзьями">${icon("share")}</button>
        <button class="iconbtn" data-act="open" data-i="${i}" title="Открыть папку игры">${icon("folder")}</button>
        <button class="iconbtn del" data-act="remove" data-i="${i}" title="Убрать из TLauncher">${icon("trash")}</button>
      </div></div>`).join("")
    : `<div class="empty">Пока пусто. Добавь сборку слева — она появится здесь и в TLauncher.</div>`;
}

// ---------- Рецепты сборок ----------

// Поделиться: моды ищутся на Modrinth, конфиги пакуются в zip, сборка уходит автору на проверку
async function shareRecipe(p) {
  const done = dialog({title: `Поделиться — ${p.name}`, html: '<div class="muted">Ищу моды сборки на Modrinth…</div>', wide: true,
    buttons: [{label: "Отмена", value: null}, {label: "Отправить на проверку", value: "sent", primary: true, check: async () => {
      const err = $("#share-err");
      if (!err) return false; // ещё идёт разбор
      err.textContent = "Отправляю…";
      try {
        state.shareMessage = await api().share_submit($("#share-name").value, $("#share-desc").value, $("#share-nick").value);
        return true;
      } catch (e) {
        err.textContent = errText(e);
        return false;
      }
    }}]});
  try {
    const s = await api().share_scan(p.version_id);
    const missing = s.missing.length
      ? `<div class="warn">Нет на Modrinth — у друзей этих файлов не будет (${s.missing.length}): ${esc(s.missing.slice(0, 12).join(", "))}${s.missing.length > 12 ? " и др." : ""}</div>` : "";
    $("#modal-body").innerHTML = `<div class="grid-form share-form">
        <label class="field"><span>Название</span><input id="share-name" maxlength="60" value="${esc(s.name)}"></label>
        <label class="field"><span>Твой ник — автор сборки</span><input id="share-nick" maxlength="16" value="${esc(state.info.nick || "")}"></label>
      </div>
      <label class="field"><span>Описание для друзей</span><textarea id="share-desc" maxlength="500" rows="3" placeholder="Что в сборке, на чём играем"></textarea></label>
      <div class="kv">
        <span>С Modrinth</span><span>${s.mods} файлов: моды, ресурспаки, шейдеры — друзья скачают их оттуда</span>
        <span>Конфиги</span><span>${s.zip_files ? `${s.zip_files} файлов, ${s.zip_size < 2 ** 20 ? Math.ceil(s.zip_size / 1024) + " КБ" : (s.zip_size / 2 ** 20).toFixed(1) + " МБ"}` : "нет"}</span>
      </div>
      ${missing}
      ${s.skipped.length ? `<div class="hint">Пропущены большие файлы конфигов: ${esc(s.skipped.slice(0, 5).join(", "))}</div>` : ""}
      <div class="hint">Сборка уйдёт автору лаунчера на проверку и появится у всех в «Сборках друзей», когда он её одобрит.
        Поменять её потом можешь только ты — с этого ника на этом компьютере.</div>
      <div class="line" id="share-err"></div>`;
  } catch (e) {
    $("#modal-body").innerHTML = `<div class="hint">${esc(errText(e))}</div>`;
  }
  if (await done === "sent") showInfo("Сборка отправлена", state.shareMessage);
}

// Сборки друзей: одобренные рецепты, моды качаются с Modrinth
async function friendRecipes() {
  const done = dialog({title: "Сборки друзей", html: '<div class="muted">Загружаю список…</div>', wide: true,
                       buttons: [{label: "Закрыть", value: null, primary: true}]});
  let list = [];
  try {
    list = await api().recipe_list();
    $("#modal-body").innerHTML = list.length ? `<div class="list">${list.map((r, i) => `<div class="item">
        <div class="grow"><div class="name">${esc(r.name)} <span class="muted small">от ${esc(r.author)}</span></div>
          <div class="meta">${esc(r.minecraft)} · ${esc(r.loader_name)} · ${r.mods} модов с Modrinth${r.missing ? ` · ${r.missing} без Modrinth` : ""}</div>
          ${r.description ? `<div class="meta wrap">${esc(r.description)}</div>` : ""}</div>
        <button class="btn${r.installed && !r.outdated ? "" : " primary"}" data-recipe="${i}">${r.outdated ? "Обновить" : r.installed ? "Переставить" : "Установить"}</button>
      </div>`).join("")}</div>`
      : `<div class="empty">Пока никто не поделился сборкой. Поделиться своей — кнопка ${icon("share")} в «Моих сборках».</div>`;
  } catch (e) {
    $("#modal-body").innerHTML = `<div class="hint">${esc(errText(e))}</div>`;
  }
  $("#modal-body").onclick = e => {
    const b = e.target.closest("[data-recipe]");
    if (!b) return;
    $("#modal").classList.add("hidden");
    installRecipe(list[b.dataset.recipe]);
  };
}

async function installRecipe(r) {
  if (state.busy) return;
  state.busy = true;
  $("#recipes-btn").disabled = true;
  setStatus("custom", `Ставлю «${r.name}»…`);
  setProgress("custom", 0);
  try {
    const res = await api().install_recipe(r.id, r.sha);
    setStatus("custom", `Готово: «${res.version_id}»`);
    loadProfiles();
    showInfo("Сборка установлена", res.text);
  } catch (e) {
    setStatus("custom", "Ошибка, попробуй ещё раз");
    showError("Не удалось поставить сборку:\n\n" + errText(e));
  }
  state.busy = false;
  $("#recipes-btn").disabled = false;
}

// Память для игры: ползунок с советом лаунчера. key — id карточки или custom:<версия>
async function memoryDialog(key, name, size = 0) {
  const m = await api().memory_info(key, size || 0);
  const html = `<div class="mem-row"><input type="range" id="mem-range" min="1" max="${m.max}" value="${m.gb}">
      <b id="mem-val">${m.gb} ГБ</b></div>
    <label class="check"><input type="checkbox" id="mem-auto"${m.auto ? " checked" : ""}><span>Как советует лаунчер (${m.recommended} ГБ)</span></label>
    <div class="hint">У компьютера ${m.total} ГБ памяти${m.mods ? `, в сборке ${m.mods} модов` : ""}.
      Больше — не всегда лучше: Windows и браузеру тоже нужна память, а лишние гигабайты игре почти не помогают.
      Память выставляется в TLauncher, когда жмёшь «Играть» в лаунчере (TLauncher при этом должен быть закрыт).</div>`;
  const done = dialog({title: `Память — ${name}`, html, buttons: [
    {label: "Отмена", value: false},
    {label: "Сохранить", value: true, primary: true, check: async () => {
      try {
        await api().set_memory(key, $("#mem-auto").checked ? null : Number($("#mem-range").value));
        return true;
      } catch (e) {
        showError(errText(e));
        return false;
      }
    }}]});
  const range = $("#mem-range"), auto = $("#mem-auto");
  const sync = () => { $("#mem-val").textContent = `${range.value} ГБ`; };
  range.oninput = () => { auto.checked = false; sync(); };
  auto.onchange = () => { if (auto.checked) { range.value = m.recommended; sync(); } };
  return done;
}

async function profileAction(act, p) {
  if (act === "open") return api().open_folder(p.game_dir);
  if (act === "memory") return memoryDialog(`custom:${p.version_id}`, p.name);
  if (act === "share") return shareRecipe(p);
  if (act === "updates") return modUpdates(p);
  if (act === "play") {
    try {
      return showInfo("Готово", await api().play_custom(p.version_id));
    } catch (e) {
      return showError(errText(e));
    }
  }
  const ok = await confirmBox("Убрать сборку?", `Убрать версию «${p.version_id}» из TLauncher?\n\nПапка игры с мирами и настройками останется:\n${p.game_dir}`, "Убрать");
  if (!ok) return;
  try {
    await api().remove_profile(p.version_id);
  } catch (e) {
    showError("Не удалось удалить версию:\n\n" + errText(e));
  }
  loadProfiles();
}

// Обновление модов: список с галочками, бета и альфа по умолчанию не отмечены
async function modUpdates(p) {
  if (state.busy) return;
  let list = [];
  const done = dialog({title: `Обновления модов — ${p.name}`, html: '<div class="muted">Ищу обновления на Modrinth…</div>', wide: true,
    buttons: [{label: "Отмена", value: null}, {label: "Обновить выбранные", value: "go", primary: true, check: () => {
      const picked = $$("#modal-body input[data-u]:checked").map(x => +x.dataset.u);
      if (!list.length) return false;
      if (!picked.length) {
        $("#upd-err").textContent = "Отметь хотя бы один мод.";
        return false;
      }
      state.updatesPicked = picked;
      return true;
    }}]});
  try {
    const r = await api().mod_updates(p.version_id);
    list = r.updates;
    const kind = {beta: "бета", alpha: "альфа"};
    $("#modal-body").innerHTML = list.length ? `
      <div class="upd-head"><label class="check"><input type="checkbox" id="upd-all"><span>Все</span></label>
        <span class="muted small">Можно обновить ${list.length} из ${r.mods} модов${r.found < r.mods ? ` · ${r.mods - r.found} не с Modrinth, их не проверить` : ""}</span></div>
      <div class="list upd-list">${list.map((u, i) => `<label class="item upd">
          <input type="checkbox" data-u="${i}"${u.type === "release" ? " checked" : ""}>
          <div class="grow"><div class="name">${esc(u.title)}${kind[u.type] ? ` <span class="tag">${kind[u.type]}</span>` : ""}</div>
            <div class="meta">${esc(u.from)} → <b>${esc(u.to)}</b></div></div></label>`).join("")}</div>
      <div class="hint">Старые версии модов сохранятся — если что-то сломается, их можно вернуть. Бета и альфа — тестовые версии, они не отмечены.</div>
      <div class="line" id="upd-err"></div>`
      : `<div class="empty">Все моды свежие${r.found < r.mods ? `.<br><span class="muted small">${r.mods - r.found} из ${r.mods} модов не с Modrinth — их лаунчер проверить не может.</span>` : "."}</div>`;
    const all = $("#upd-all");
    if (all) {
      const sync = () => {
        const boxes = $$("#modal-body input[data-u]");
        all.checked = boxes.every(b => b.checked);
        all.indeterminate = !all.checked && boxes.some(b => b.checked);
      };
      all.onchange = () => $$("#modal-body input[data-u]").forEach(b => { b.checked = all.checked; });
      $$("#modal-body input[data-u]").forEach(b => b.onchange = sync);
      sync();
    }
  } catch (e) {
    $("#modal-body").innerHTML = `<div class="hint">${esc(errText(e))}</div>`;
  }
  if (await done !== "go") return;
  state.busy = true;
  setStatus("custom", "Обновляю моды…");
  setProgress("custom", 0);
  try {
    const text = await api().apply_mod_updates(p.version_id, state.updatesPicked);
    setStatus("custom", "Моды обновлены");
    showInfo("Моды обновлены", text);
  } catch (e) {
    setStatus("custom", "Ошибка, попробуй ещё раз");
    showError("Не удалось обновить моды:\n\n" + errText(e));
  }
  state.busy = false;
}

// ---------- Сервер ----------

async function loadServerTab() {
  const [profiles, servers] = await Promise.all([api().profile_list(), api().server_list()]);
  state.servers = servers;
  const sel = $("#host-pack"), keep = sel.value;
  sel.innerHTML = profiles.length ? profiles.map(p => `<option>${esc(p.name)}</option>`).join("")
    : `<option value="">сначала добавь сборку в «Свои сборки»</option>`;
  sel.value = profiles.some(p => p.name === keep) ? keep : (profiles.at(-1)?.name || "");
  $("#hosted").innerHTML = servers.length ? servers.map((s, i) => `
    <div class="item srv"><div class="grow"><div class="name">${esc(s.name)}</div>
      <div class="meta"><span class="state${s.online ? " online" : ""}">${s.online ? `● онлайн · ${esc(s.players)}` : "○ выключен"}</span>
        · ${esc(s.address)} · ${esc(s.minecraft)} ${esc(s.loader_name)}</div></div>
      <div class="tools">
        ${s.online
          ? `<button class="iconbtn del" data-act="stop" data-i="${i}" title="Остановить">${icon("player-stop")}</button>
             <button class="iconbtn" data-act="restart" data-i="${i}" title="Перезапустить">${icon("refresh")}</button>`
          : `<button class="iconbtn go" data-act="start" data-i="${i}" title="Запустить (перед запуском — бэкап мира)">${icon("player-play")}</button>`}
        <button class="iconbtn" data-act="console" data-i="${i}" title="Консоль: лог и команды">${icon("terminal-2")}</button>
        <button class="iconbtn" data-act="players" data-i="${i}" title="Игроки, белый список, операторы">${icon("users")}</button>
        <button class="iconbtn" data-act="stats" data-i="${i}" title="Статистика: кто сколько играл">${icon("chart-bar")}</button>
        <button class="iconbtn" data-act="load" data-i="${i}" title="Нагрузка: TPS, память, процессор">${icon("activity")}</button>
        <button class="iconbtn" data-act="backups" data-i="${i}" title="Бэкапы мира: сделать и восстановить">${icon("device-floppy")}</button>
        <button class="iconbtn" data-act="settings" data-i="${i}" title="Настройки">${icon("settings")}</button>
        <button class="iconbtn" data-act="folder" data-i="${i}" title="Папка сервера">${icon("folder")}</button>
        <button class="iconbtn del" data-act="forget" data-i="${i}" title="Убрать из списка">${icon("trash")}</button>
      </div></div>`).join("")
    : `<div class="empty">Тут появятся серверы, которые ты соберёшь.</div>`;
}

async function hostServer() {
  const pack = $("#host-pack").value;
  if (!pack) return showInfo("Нет сборки", "Сначала добавь сборку во вкладке «Свои сборки».");
  if (!$("#eula").checked) return showInfo("Нужно принять EULA", "Чтобы запустить сервер Minecraft, нужно принять EULA.");
  $("#host-btn").disabled = true;
  try {
    const rec = await api().host_server(pack);
    if (rec) {
      setStatus("host", `Сервер собран: ${rec.path}`);
      await loadServerTab();
      await serverSettings(rec.path);
    }
  } catch (e) {
    setStatus("host", "Ошибка, попробуй ещё раз");
    showError("Не удалось собрать сервер:\n\n" + errText(e));
  }
  $("#host-btn").disabled = false;
}

function settingControl(f) {
  if (f.kind === "choice")
    return `<select data-key="${f.key}">${f.choices.map(([k, v]) => `<option value="${esc(k)}"${k === f.value ? " selected" : ""}>${esc(v)}</option>`).join("")}</select>`;
  const type = f.kind === "int" ? "number" : "text";
  return `<input type="${type}" data-key="${f.key}" value="${esc(f.value)}">`;
}

async function serverSettings(path) {
  const s = await api().server_settings(path);
  const fields = s.fields.filter(f => f.kind !== "bool");
  const flags = s.fields.filter(f => f.kind === "bool");
  const html = `<div class="grid-form">
      ${fields.map(f => `<label class="field"><span>${esc(f.label)}</span>${settingControl(f)}</label>`).join("")}
      <label class="field"><span>Память сервера, ГБ</span><input type="number" min="1" max="64" data-key="ram" value="${s.ram}"></label>
      <label class="field" title="Пустой сервер сам выключится через столько минут. 0 — не выключать"><span>Выключать пустой через, мин</span><input type="number" min="0" data-key="auto_stop" value="${s.auto_stop}"></label>
    </div>
    <div class="grid-form" style="margin-top:14px">
      ${flags.map(f => `<label class="check"><input type="checkbox" data-key="${f.key}"${f.value ? " checked" : ""}><span>${esc(f.label)}</span></label>`).join("")}
    </div>
    <div class="hint">${esc(s.hint)}</div>
    <div class="warn" id="form-err"></div>`;
  const save = async body => {
    const values = {};
    for (const el of $$("[data-key]", body)) values[el.dataset.key] = el.type === "checkbox" ? el.checked : el.value;
    try {
      await api().save_server_settings(path, values);
      return true;
    } catch (e) {
      $("#form-err", body).textContent = errText(e);
      return false;
    }
  };
  await dialog({title: `Настройки сервера — ${s.name}`, html, wide: true,
    buttons: [{label: "Отмена", value: false}, {label: "Сохранить", value: true, primary: true, check: save}]});
  loadServerTab();
}

// Игроки онлайн, белый список и операторы. Пока сервер запущен — команды по RCON, иначе правка файлов
async function serverPlayers(s) {
  const render = async () => {
    const body = $("#modal-body");
    let d;
    try {
      d = await api().server_players(s.path);
    } catch (e) {
      body.innerHTML = `<div class="hint">${esc(errText(e))}</div>`;
      return;
    }
    const chips = (names, act) => names.length
      ? names.map(n => `<span class="chip-name">${esc(n)}<button data-act="${act}" data-name="${esc(n)}" title="Убрать">×</button></span>`).join("")
      : `<span class="muted">пусто</span>`;
    const isOp = n => d.ops.some(o => o.toLowerCase() === n.toLowerCase());
    // Кик и бан — со вторым нажатием: первое только спрашивает «Точно?»
    const onlineRow = n => `<div class="pl-row"><span class="grow">${esc(n)}${isOp(n) ? ` <span class="tag">оператор</span>` : ""}</span>
        <button class="iconbtn" data-act="${isOp(n) ? "deop" : "op"}" data-name="${esc(n)}" title="${isOp(n) ? "Снять оператора" : "Сделать оператором"}">${icon("crown")}</button>
        <button class="iconbtn" data-act="kick" data-confirm="1" data-name="${esc(n)}" title="Кикнуть (сможет зайти снова)">${icon("door-exit")}</button>
        <button class="iconbtn del" data-act="ban" data-confirm="1" data-name="${esc(n)}" title="Забанить">${icon("ban")}</button></div>`;
    body.innerHTML = `
      <div class="hint pl-err" id="pl-err">${d.running ? "" : "Сервер выключен: изменения применятся при следующем запуске."}</div>
      <div class="pl-sec"><h4>Сейчас на сервере${d.running && d.online.length ? ` (${d.online.length})` : ""}</h4>
        ${!d.running ? `<span class="muted">сервер выключен</span>` : d.online.length
          ? `<div class="pl-list">${d.online.map(onlineRow).join("")}</div>`
          : `<span class="muted">никого</span>`}</div>
      <div class="pl-sec"><h4>Белый список</h4>
        <label class="check"><input type="checkbox" id="wl-on" ${d.whitelist_on ? "checked" : ""}><span>Пускать только игроков из списка</span></label>
        <div class="chips">${chips(d.whitelist, "whitelist_remove")}</div>
        <div class="row2"><input id="wl-name" maxlength="16" placeholder="Ник"><button class="btn" data-act="whitelist_add">Добавить</button></div></div>
      <div class="pl-sec"><h4>Операторы (админы)</h4>
        <div class="chips">${chips(d.ops, "deop")}</div>
        <div class="row2"><input id="op-name" maxlength="16" placeholder="Ник"><button class="btn" data-act="op">Сделать оператором</button></div></div>
      <div class="pl-sec"><h4>Забаненные</h4>
        <div class="chips">${d.banned.length ? d.banned.map(n => `<span class="chip-name">${esc(n)}<button data-act="pardon" data-name="${esc(n)}" title="Разбанить">×</button></span>`).join("") : `<span class="muted">никого</span>`}</div>
        <div class="row2"><input id="ban-name" maxlength="16" placeholder="Ник"><button class="btn" data-act="ban" data-confirm="1">Забанить</button></div></div>
`;
    $("#wl-on", body).onchange = e => act(e.target.checked ? "whitelist_on" : "whitelist_off");
  };
  const act = async (action, name = "") => {
    try {
      await api().server_player_action(s.path, action, name);
      await render();
    } catch (e) {
      $("#pl-err").textContent = errText(e);
    }
  };
  const done = dialog({title: `Игроки — ${s.name}`, html: '<div class="muted">Загружаю…</div>', wide: true,
                       buttons: [{label: "Закрыть", value: null, primary: true}]});
  $("#modal-body").onclick = e => {
    const b = e.target.closest("button[data-act]");
    if (!b) return;
    const input = b.dataset.name ? null : {whitelist_add: "#wl-name", op: "#op-name", ban: "#ban-name"}[b.dataset.act];
    const name = input ? $(input).value.trim() : b.dataset.name;
    if (b.dataset.confirm && !b.classList.contains("sure")) {
      if (!name) return act(b.dataset.act, name); // пустой ник — пусть сервер лаунчера объяснит
      b.classList.add("sure");
      $("#pl-err").textContent = `${b.dataset.act === "ban" ? "Забанить" : "Кикнуть"} ${name}? Нажми ещё раз, чтобы подтвердить.`;
      setTimeout(() => b.classList.remove("sure"), 4000);
      return;
    }
    act(b.dataset.act, name);
  };
  await render();
  await done;
}

// Нагрузка: замер раз в 2 секунды, графики за последние 5 минут, пока окно открыто
const LOAD_POINTS = 150;
const gb = bytes => (bytes / 2 ** 30).toFixed(1);

function loadChart(values, max) {
  // Один ряд на график, шкала от нуля; линия 2px и лёгкая заливка под ней
  const W = 300, H = 64, n = values.length;
  if (n < 2) return `<svg class="ld-chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none"></svg>`;
  // Пока данных меньше минуты, график растянут на всю ширину, дальше — окно в 5 минут
  const span = Math.max(n, 30);
  const x = i => (W * (span - n + i) / (span - 1)).toFixed(1);
  const y = v => (H - 2 - (H - 4) * Math.min(1, (v ?? 0) / max)).toFixed(1);
  const pts = values.map((v, i) => `${x(i)},${y(v)}`).join(" ");
  return `<svg class="ld-chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" data-max="${max}">
      <line class="ld-grid" x1="0" x2="${W}" y1="${H / 2}" y2="${H / 2}"/>
      <polygon class="ld-area" points="${x(0)},${H} ${pts} ${x(n - 1)},${H}"/>
      <polyline class="ld-line" points="${pts}"/>
      <line class="ld-cross hidden" y1="0" y2="${H}"/></svg>`;
}

async function serverLoad(s) {
  const hist = {tps: [], players: [], memory: [], cpu: []};
  let open = true;
  const html = `<div class="ld-grid4" id="ld-tiles"><div class="muted">Замеряю…</div></div>
    <div class="hint" id="ld-note"></div>`;
  const done = dialog({title: `Нагрузка — ${s.name}`, html, wide: true, buttons: [{label: "Закрыть", value: null, primary: true}]});
  done.then(() => {
    open = false;
    $("#modal-body").onmousemove = $("#modal-body").onmouseleave = null;
  });

  const tile = (key, label, value, sub, max) => `<div class="ld-tile" data-key="${key}">
      <div class="ld-label">${label}</div><div class="ld-value">${value}</div><div class="ld-sub">${sub}</div>
      ${max ? loadChart(hist[key], max) : ""}</div>`;
  const tpsState = t => t >= 18 ? ["ok", "норма"] : t >= 15 ? ["warn", "подлагивает"] : ["bad", "лагает"];
  const render = d => {
    const memMax = d.memory_max || 1;
    // java.exe берёт сверх выделенного под мир ещё ~20–40% на себя, поэтому шкала с запасом
    const memScale = Math.max(memMax * 1.5, ...hist.memory.filter(v => v != null)) * 1.05;
    const maxPlayers = Math.max(4, ...hist.players.filter(v => v != null)) * 1.2;
    const [cls, word] = d.tps != null ? tpsState(d.tps) : ["", ""];
    $("#ld-tiles").innerHTML =
      (d.tps != null
        ? tile("tps", "TPS", `${d.tps.toFixed(1)}<span class="ld-state ${cls}">${word}</span>`,
               `тик ${d.mspt} мс · 20 TPS — идеально, ниже 15 заметны лаги`, 20)
        : tile("tps", "Лаги за 5 минут", `${d.lag}<span class="ld-state ${d.lag ? d.lag > 5 ? "bad" : "warn" : "ok"}">${
                 d.lag ? d.lag > 5 ? "лагает" : "бывают" : "норма"}</span>`, "сколько раз сервер не успевал", null)) +
      tile("players", "Игроки онлайн", d.players ?? "—", "сколько человек на сервере", maxPlayers) +
      tile("memory", "Память java.exe", d.memory != null ? `${gb(d.memory)} ГБ` : "—", `под мир ${gb(memMax)} ГБ + сама Java`, memScale) +
      tile("cpu", "Процессор", d.cpu != null ? `${d.cpu}%` : "—", "от всего процессора компьютера", 100);
    const notes = [];
    if (d.tps == null) notes.push("Точный TPS сервер сообщает на Forge, NeoForge и Minecraft 1.20.3+; здесь лаги считаются по предупреждениям сервера.");
    else if (d.lag) notes.push(`За последние 5 минут сервер ${d.lag} раз не успевал обрабатывать мир.`);
    $("#ld-note").textContent = notes.join(" ");
  };

  // Наведение: вертикальная линия и значение в этой точке
  $("#modal-body").onmousemove = e => {
    const svg = e.target.closest(".ld-chart");
    $$(".ld-cross").forEach(l => l.classList.add("hidden"));
    $$(".ld-tile .ld-sub").forEach(el => { if (el.dataset.text) { el.textContent = el.dataset.text; delete el.dataset.text; } });
    if (!svg) return;
    const key = svg.closest(".ld-tile").dataset.key, values = hist[key], n = values.length;
    if (n < 2) return;
    const r = svg.getBoundingClientRect();
    const span = Math.max(n, 30);
    const pos = (e.clientX - r.left) / r.width * (span - 1) - (span - n);
    const i = Math.max(0, Math.min(n - 1, Math.round(pos)));
    const v = values[i];
    const cross = $(".ld-cross", svg);
    const xx = (300 * (span - n + i) / (span - 1)).toFixed(1);
    cross.setAttribute("x1", xx); cross.setAttribute("x2", xx); cross.classList.remove("hidden");
    const sub = $(".ld-sub", svg.closest(".ld-tile"));
    sub.dataset.text = sub.textContent;
    const ago = (n - 1 - i) * 2;
    const fmt = {tps: x => `${x.toFixed(1)} TPS`, players: x => `${x} чел.`, memory: x => `${gb(x)} ГБ`, cpu: x => `${x}%`}[key];
    sub.textContent = `${v == null ? "нет данных" : fmt(v)} · ${ago ? `${ago} с назад` : "сейчас"}`;
  };
  $("#modal-body").onmouseleave = () => $("#modal-body").onmousemove({target: document.body});

  while (open) {
    try {
      const d = await api().server_load(s.path);
      if (!open) break;
      if (!d.running) {
        $("#ld-tiles").innerHTML = `<div class="muted">Сервер выключен. Запусти его — графики начнутся сами.</div>`;
        $("#ld-note").textContent = "";
        for (const k in hist) hist[k].length = 0;
      } else {
        for (const k in hist) {
          hist[k].push(d[k]);
          if (hist[k].length > LOAD_POINTS) hist[k].shift();
        }
        // Пока мышь над графиком, не перерисовываем — иначе подсказка мигает
        if (!$(".ld-cross:not(.hidden)")) render(d);
      }
    } catch (e) {
      if (!open) break;
      $("#ld-note").textContent = errText(e);
    }
    await wait(2000);
  }
}

// ---------- Друзья в сети ----------

state.friends = [];

function renderFriends(list) {
  state.friends = list;
  $("#friends-count").textContent = list.length;
  $("#friends-btn").classList.toggle("some", list.length > 0);
  if ($("#friends-list")) $("#friends-list").innerHTML = friendsHtml();
}

function friendStatus(f) {
  if (f.server) return `на сервере «${esc(f.server)}»`;
  if (f.playing) return `играет${f.pack ? ` в «${esc(f.pack)}»` : ""}${f.mode ? ` · ${esc(f.mode)}` : ""}`;
  return "в лаунчере";
}

function friendsHtml() {
  if (!state.friends.length) return `<div class="empty">Никого нет в сети.<br><span class="muted small">Друг виден, когда у него
    открыт лаунчер 0.9.5 или новее и включён Radmin VPN — или когда он играет на одном из серверов в списке.</span></div>`;
  return state.friends.map((f, i) => `<div class="item fr">
      <span class="dot${f.playing ? " on" : ""}"></span>
      <div class="grow"><div class="name">${esc(f.nick)}</div>
        <div class="meta">${friendStatus(f)}${f.hosting?.length ? ` · держит сервер «${esc(f.hosting[0])}»` : ""}${f.launcher ? "" : " · без лаунчера"}</div></div>
      ${f.card ? `<button class="btn primary" data-join="${i}">${icon("player-play")}К нему</button>` : ""}
      ${f.address || f.ip ? `<button class="iconbtn" data-fcheck="${i}" title="Проверить связь">${icon("wifi")}</button>` : ""}
    </div>`).join("");
}

async function friendsDialog() {
  const html = `<div class="list" id="friends-list">${friendsHtml()}</div>
    <form class="con-row fr-check" id="fr-check-form">
      <input id="fr-check-addr" placeholder="Проверить связь с адресом, например 26.12.34.56:25565">
      <button class="btn" type="submit">${icon("wifi")}Проверить</button></form>`;
  const done = dialog({title: "Друзья в сети", html, wide: true, buttons: [
    {label: "Обновить", value: null, check: async () => {
      renderFriends(await api().friends_online());
      return false;
    }},
    {label: "Закрыть", value: null, primary: true}]});
  $("#fr-check-form").onsubmit = e => {
    e.preventDefault();
    const addr = $("#fr-check-addr").value.trim();
    if (addr) checkDialog(addr, addr);
  };
  $("#modal-body").onclick = e => {
    const join = e.target.closest("[data-join]"), chk = e.target.closest("[data-fcheck]");
    if (join) {
      const f = state.friends[join.dataset.join];
      $("#modal").classList.add("hidden");
      joinFriend(f);
    } else if (chk) {
      const f = state.friends[chk.dataset.fcheck];
      checkDialog(f.address || f.ip, f.nick);
    }
  };
  await done;
}

// «К нему»: открываем «Наши серверы» и отмечаем его сервер — останется нажать ИГРАТЬ
async function joinFriend(f) {
  go("play");
  if (!state.selected.includes(f.card)) await toggleCard(f.card);
  const card = $$("#cards .card").find(c => c.dataset.id === f.card);
  if (card) {
    card.scrollIntoView({block: "nearest"});
    card.classList.add("flash");
    setTimeout(() => card.classList.remove("flash"), 1600);
  }
}

// Проверка связи по шагам: Radmin → компьютер друга → порт → Minecraft
async function checkDialog(address, name) {
  const marks = {ok: ["check", "ok"], warn: ["alert-triangle", "warn"], fail: ["x", "bad"], skip: ["minus", "skip"]};
  const run = async () => {
    const body = $("#modal-body");
    body.innerHTML = `<div class="muted">Проверяю ${esc(address)}… это до 10 секунд.</div>`;
    try {
      const r = await api().check_connection(address);
      body.innerHTML = `<div class="ck-list">${r.steps.map(s => `<div class="ck ${marks[s.state][1]}">
          <span class="ck-ico">${icon(marks[s.state][0])}</span>
          <div><div class="ck-title">${esc(s.title)}</div><div class="ck-text">${esc(s.text)}</div></div></div>`).join("")}</div>
        <div class="ck-summary ${r.steps.some(s => s.state === "fail") ? "bad" : "ok"}">${esc(r.summary)}</div>`;
    } catch (e) {
      body.innerHTML = `<div class="hint">${esc(errText(e))}</div>`;
    }
    return false;
  };
  const done = dialog({title: `Связь — ${name}`, html: "", wide: true, buttons: [
    {label: "Проверить снова", value: null, check: run},
    {label: "Закрыть", value: null, primary: true}]});
  run();
  await done;
}

const wait = ms => new Promise(r => setTimeout(r, ms));
const noColors = text => text.replace(/§./g, ""); // цветовые коды Minecraft

// Консоль: хвост logs/latest.log и команды по RCON
async function serverConsole(s) {
  let pos = -1, alive = true;
  const history = [];
  let back = 0;
  const html = `<pre class="console" id="con-log">Загружаю лог…</pre>
    <form class="con-row" id="con-form">
      <input id="con-cmd" autocomplete="off" placeholder="Команда без /, например: say Привет · time set day · weather clear">
      <button class="btn primary" type="submit">Отправить</button></form>
    <div class="hint" id="con-err">${s.online ? "" : "Сервер выключен — показан лог последнего запуска."}</div>`;
  const done = dialog({title: `Консоль — ${s.name}`, html, wide: true, buttons: [{label: "Закрыть", value: null}]});
  const log = $("#con-log"), input = $("#con-cmd");
  log.textContent = "";
  const append = text => {
    const atEnd = log.scrollTop + log.clientHeight >= log.scrollHeight - 40;
    log.textContent += noColors(text);
    if (log.textContent.length > 300000) log.textContent = log.textContent.slice(-200000);
    if (atEnd) log.scrollTop = log.scrollHeight;
  };
  (async () => {
    while (alive) {
      try {
        const r = await api().server_console(s.path, pos);
        if (pos >= 0 && r.pos < pos) append("\n——— сервер начал новый лог ———\n");
        pos = r.pos;
        if (r.text) append(r.text);
        else if (!log.textContent) log.textContent = "Лог пуст: сервер ещё не запускался.";
      } catch { /* сервер мог удалить лог при перезапуске — попробуем ещё раз */ }
      await wait(1500);
    }
  })();
  input.onkeydown = e => {
    // Стрелки — прошлые команды, как в консоли
    if (e.key !== "ArrowUp" && e.key !== "ArrowDown") return;
    e.preventDefault();
    back = Math.max(0, Math.min(history.length, back + (e.key === "ArrowUp" ? 1 : -1)));
    input.value = back ? history[history.length - back] : "";
  };
  $("#con-form").onsubmit = async e => {
    e.preventDefault();
    const cmd = input.value.trim();
    if (!cmd) return;
    input.value = "";
    history.push(cmd);
    back = 0;
    $("#con-err").textContent = "";
    log.scrollTop = log.scrollHeight;
    try {
      const answer = await api().server_command(s.path, cmd);
      append(`> ${cmd}\n${answer ? answer.trim() + "\n" : ""}`);
    } catch (err) {
      $("#con-err").textContent = errText(err);
    }
  };
  input.focus();
  await done;
  alive = false;
}

// Бэкапы: список, «сделать сейчас», восстановление (сервер должен быть выключен)
async function serverBackups(s) {
  const render = async (message = "") => {
    const body = $("#modal-body");
    let list;
    try {
      list = await api().server_backups(s.path);
    } catch (e) {
      body.innerHTML = `<div class="hint">${esc(errText(e))}</div>`;
      return;
    }
    body.innerHTML = `<div class="hint">Бэкап делается сам перед каждым запуском сервера, хранятся последние 7.
        Восстановить можно, когда сервер выключен; текущий мир перед этим тоже сохранится в бэкап.</div>
      <div class="list bk-list">${list.length ? list.map(b => `<div class="item">
          <div class="grow"><div class="name">${esc(b.time)}</div><div class="meta">${esc(b.name)} · ${(b.size / 2 ** 20).toFixed(1)} МБ</div></div>
          <button class="btn" data-restore="${esc(b.name)}">Восстановить</button></div>`).join("")
        : `<div class="empty">Бэкапов пока нет</div>`}</div>
      <div class="line" id="bk-msg">${esc(message)}</div>`;
  };
  const done = dialog({title: `Бэкапы мира — ${s.name}`, html: '<div class="muted">Загружаю…</div>', wide: true, buttons: [
    {label: "Сделать бэкап сейчас", value: null, check: async () => {
      $("#bk-msg").textContent = "Делаю бэкап…";
      try {
        await render(await api().backup_server(s.path));
      } catch (e) {
        $("#bk-msg").textContent = errText(e);
      }
      return false; // окно остаётся открытым
    }},
    {label: "Закрыть", value: null, primary: true}]});
  $("#modal-body").onclick = async e => {
    const b = e.target.closest("button[data-restore]");
    if (!b) return;
    // Второе нажатие — подтверждение: мир заменится целиком
    if (!b.classList.contains("confirm")) {
      for (const other of $$("button.confirm")) { other.classList.remove("confirm"); other.textContent = "Восстановить"; }
      b.classList.add("confirm");
      b.textContent = "Точно? Нажми ещё раз";
      return;
    }
    b.disabled = true;
    b.textContent = "Восстанавливаю…";
    try {
      await render(await api().restore_backup(s.path, b.dataset.restore));
    } catch (err) {
      b.disabled = false;
      b.classList.remove("confirm");
      b.textContent = "Восстановить";
      $("#bk-msg").textContent = errText(err);
    }
  };
  await render();
  await done;
}

function duration(seconds) {
  const h = Math.floor(seconds / 3600), m = Math.round((seconds % 3600) / 60);
  return h ? `${h} ч ${m} мин` : `${m} мин`;
}

// Статистика по логам сервера: кто сколько играл и когда заходил
async function serverStats(s) {
  const done = dialog({title: `Статистика — ${s.name}`, html: '<div class="muted">Считаю по логам сервера…</div>', wide: true,
                       buttons: [{label: "Закрыть", value: null, primary: true}]});
  try {
    const rows = await api().server_stats(s.path);
    const top = rows[0]?.seconds || 1;
    $("#modal-body").innerHTML = rows.length ? `<table class="stats">
        <tr><th>Игрок</th><th>Наиграно</th><th></th><th>Заходов</th><th>Последний раз</th></tr>
        ${rows.map(r => `<tr><td>${esc(r.name)}</td><td>${duration(r.seconds)}</td>
          <td class="bar"><i style="width:${Math.max(2, Math.round(r.seconds / top * 100))}%"></i></td>
          <td>${r.sessions}</td><td>${esc(r.last)}</td></tr>`).join("")}</table>
      <div class="hint">Считается по логам сервера (папка logs): входы и выходы игроков.</div>`
      : `<div class="empty">Пока никто не заходил — или логи сервера удалены.</div>`;
  } catch (e) {
    $("#modal-body").innerHTML = `<div class="hint">${esc(errText(e))}</div>`;
  }
  await done;
}

async function serverAction(act, s) {
  try {
    if (["start", "stop", "restart"].includes(act)) {
      setStatus("host", act === "stop" ? "Останавливаю сервер…" : "Готовлю запуск…");
      const call = {start: "start_server", stop: "stop_server", restart: "restart_server"}[act];
      setStatus("host", await api()[call](s.path));
      // Сервер запускается и выключается не мгновенно — обновим индикатор чуть позже
      setTimeout(loadServerTab, act === "stop" ? 8000 : 25000);
    }
    if (act === "console") await serverConsole(s);
    if (act === "stats") await serverStats(s);
    if (act === "load") await serverLoad(s);
    if (act === "backups") await serverBackups(s);
    if (act === "players") await serverPlayers(s);
    if (act === "settings") await serverSettings(s.path);
    if (act === "folder") await api().open_folder(s.path);
    if (act === "forget") {
      const ok = await confirmBox("Убрать сервер из списка?", `Убрать «${s.name}» из списка?\n\nПапка сервера останется:\n${s.path}`, "Убрать");
      if (ok) {
        await api().forget_server(s.path);
        loadServerTab();
      }
    }
  } catch (e) {
    showError(errText(e));
  }
}

// ---------- Скин ----------

// source: "server" — скин из облака, "local" — последняя загруженная копия, "picked" — выбран, но не загружен
// cape — плащ в base64 или null; capeChanged — плащ выбран или убран, но ещё не загружен
const skin = {viewer: null, b64: null, model: "default", source: null, animate: true, cape: null, capeChanged: false};

function skinViewer() {
  if (!skin.viewer && window.skinview3d) {
    try {
      const v = new skinview3d.SkinViewer({canvas: $("#skin-3d"), width: 300, height: 380});
      v.autoRotate = true;
      v.autoRotateSpeed = 0.6;
      v.animation = new skinview3d.WalkingAnimation();
      v.animation.speed = 0.6;
      v.controls.enableZoom = false;
      v.renderPaused = !$("#page-skin").classList.contains("active");
      skin.viewer = v;
    } catch (e) {
      console.warn("3D-предпросмотр недоступен", e); // без WebGL остаётся пиксельное лицо
    }
  }
  return skin.viewer;
}

function renderSkin() {
  const v = skinViewer();
  const in3d = Boolean(v && skin.b64);
  $("#skin-3d").classList.toggle("hidden", !in3d);
  $("#skin-face").classList.toggle("hidden", in3d);
  if (in3d) {
    // Битая картинка — просто не показываем её, а не роняем предпросмотр
    Promise.resolve(v.loadSkin("data:image/png;base64," + skin.b64, {model: skin.model}))
      .catch(() => console.warn("скин не читается"));
    Promise.resolve(v.loadCape(skin.cape ? "data:image/png;base64," + skin.cape : null))
      .catch(() => { console.warn("плащ не читается"); v.loadCape(null); });
  } else drawFace($("#skin-face"), skin.b64);
  $("#cape-remove").disabled = !skin.cape;
}

function setSkinTag(text, kind = "") {
  $("#skin-tag").textContent = text;
  $("#skin-tag").className = "skin-tag " + kind;
}

function setModel(model) {
  skin.model = model;
  $(`input[name=model][value=${model}]`).checked = true;
}

function showServerSkin(nick, b64, model, cape) {
  // У ника может быть только плащ — тогда скин показываем свой последний загруженный
  b64 = b64 || state.info.skin;
  Object.assign(skin, {b64, cape, source: "server"});
  setModel(model);
  setSkinTag(`Так ${nick} видят на сервере`, "server");
  drawFace($("#chip-head"), b64);
  renderSkin();
}

async function loadServerSkin() {
  // не перебиваем предпросмотр выбранного файла
  if (skin.source === "picked" || skin.capeChanged) return;
  const nick = $("#nick").value.trim();
  setSkinTag("Проверяю скин на сервере…");
  const res = await api().server_skin(nick);
  if (skin.source === "picked" || skin.capeChanged) return;
  if (res && res.found) return showServerSkin(nick, res.skin, res.model, res.cape);
  // Скина на сервере нет или облако недоступно — показываем последнюю загруженную копию
  Object.assign(skin, {b64: state.info.skin, cape: res ? null : state.info.cape, source: "local"});
  setModel(state.info.skin_model);
  setSkinTag(!res ? "Нет связи с сервером скинов — показан последний загруженный скин"
    : `На сервере у ${nick || "этого ника"} стандартный скин`);
  renderSkin();
}

async function chooseSkin() {
  try {
    const res = await api().pick_skin();
    if (!res) return;
    Object.assign(skin, {b64: res.skin, source: "picked"});
    setSkinTag(`Предпросмотр: ${res.name} — ещё не загружен`, "preview");
    $("#skin-status").textContent = "";
    $("#skin-upload").disabled = false;
    renderSkin();
  } catch (e) {
    showError(errText(e));
  }
}

function capePicked(cape, tag) {
  Object.assign(skin, {cape, capeChanged: true});
  setSkinTag(tag, "preview");
  $("#skin-status").textContent = "";
  $("#skin-upload").disabled = false;
  renderSkin();
}

async function chooseCape() {
  try {
    const res = await api().pick_cape();
    if (res) capePicked(res.cape, `Предпросмотр плаща: ${res.name} — ещё не загружен`);
  } catch (e) {
    showError(errText(e));
  }
}

async function removeCape() {
  await api().remove_cape();
  capePicked(null, "Без плаща — нажми «Загрузить на сервер», чтобы убрать его");
}

async function uploadSkin() {
  const nick = $("#nick").value.trim();
  if (!nick) return showInfo("Нужен ник", "Впиши ник, под которым играешь в TLauncher.");
  const status = $("#skin-status");
  $("#skin-upload").disabled = true;
  try {
    const res = await api().upload_skin(nick, skin.model === "slim");
    status.className = "line";
    status.textContent = res.message;
    state.info.skin = res.skin;
    state.info.cape = res.cape;
    state.info.skin_model = skin.model;
    skin.capeChanged = false;
    setNick(nick);
    showServerSkin(nick, res.skin, skin.model, res.cape);
  } catch (e) {
    status.className = "line err";
    status.textContent = "Скин не загружен";
    $("#skin-upload").disabled = false;
    showError(errText(e));
  }
}

function toggleSkinAnimation() {
  skin.animate = !skin.animate;
  const v = skinViewer();
  if (v) {
    v.autoRotate = skin.animate;
    v.animation.paused = !skin.animate;
  }
  const btn = $("#skin-anim");
  btn.title = skin.animate ? "Пауза" : "Анимация";
  btn.innerHTML = icon(skin.animate ? "player-pause" : "player-play");
}

// ---------- Скриншоты ----------

// list — все найденные, view — после фильтра по сборке (по нему индексы плиток)
const shots = {list: [], view: [], loaded: false, observer: null};
const thumbs = {queue: [], busy: 0};

async function loadShots(force = false) {
  if (shots.loaded && !force) return;
  shots.loaded = true;
  $("#shots").innerHTML = '<div class="muted">Ищу скриншоты…</div>';
  try {
    shots.list = await api().screenshot_list();
  } catch (e) {
    shots.loaded = false;
    $("#shots").innerHTML = `<div class="hint">${esc(errText(e))}</div>`;
    return;
  }
  const sel = $("#shots-pack"), keep = sel.value;
  const packs = [...new Set(shots.list.map(s => s.pack))];
  sel.innerHTML = `<option value="">Все сборки</option>` + packs.map(p => `<option>${esc(p)}</option>`).join("");
  sel.value = packs.includes(keep) ? keep : "";
  renderShots();
}

function pumpThumbs() {
  // Не больше трёх миниатюр разом: каждая — чтение и сжатие картинки в Python
  while (thumbs.busy < 3 && thumbs.queue.length) {
    const img = thumbs.queue.shift();
    if (!img.isConnected) continue;
    thumbs.busy++;
    api().screenshot_thumb(img.dataset.path)
      .then(b64 => { if (b64) img.src = "data:image/jpeg;base64," + b64; else img.classList.add("broken"); })
      .catch(() => img.classList.add("broken"))
      .finally(() => { thumbs.busy--; pumpThumbs(); });
  }
}

function renderShots() {
  const pack = $("#shots-pack").value;
  shots.view = shots.list.filter(s => !pack || s.pack === pack);
  $("#shots-info").textContent = shots.list.length
    ? `${shots.view.length} шт. · в игре скриншот — клавиша F2` : "";
  const grid = $("#shots");
  thumbs.queue = [];
  grid.innerHTML = shots.view.length ? shots.view.map((s, i) => `
    <button class="shot" data-i="${i}" title="${esc(s.pack)} · ${esc(s.name)}">
      <img alt="" data-path="${esc(s.path)}"><span><b>${esc(s.pack)}</b>${esc(s.time)}</span></button>`).join("")
    : `<div class="empty">Скриншотов пока нет. В игре нажми F2 — снимок появится здесь.</div>`;
  shots.observer?.disconnect();
  shots.observer = new IntersectionObserver(entries => {
    for (const e of entries) {
      if (!e.isIntersecting) continue;
      shots.observer.unobserve(e.target);
      thumbs.queue.push(e.target);
    }
    pumpThumbs();
  }, {root: grid, rootMargin: "300px"});
  for (const img of $$("img[data-path]", grid)) shots.observer.observe(img);
}

async function viewShot(s) {
  if (!s) return;
  const done = dialog({title: `${s.pack} · ${s.time}`, wide: true,
    html: `<div class="shot-view"><img id="shot-big" alt=""></div><div class="line" id="shot-msg"></div>`,
    buttons: [
      {label: "Копировать", value: null, check: async () => {
        try {
          await api().screenshot_copy(s.path);
          $("#shot-msg").textContent = "Скопировано — вставь через Ctrl+V в Telegram или Discord";
        } catch (e) {
          $("#shot-msg").textContent = errText(e);
        }
        return false;
      }},
      {label: "Показать в папке", value: "folder"},
      {label: "Закрыть", value: null, primary: true}]});
  api().screenshot_full(s.path)
    .then(b64 => { $("#shot-big").src = "data:image/jpeg;base64," + b64; })
    .catch(e => { $("#shot-msg").textContent = errText(e); });
  if (await done === "folder") api().screenshot_show(s.path);
}

// ---------- О лаунчере ----------

const CREDITS = [
  ["Telegram", "@Tekatokita", "https://t.me/Tekatokita"],
  ["Discord", "tekarare.", null],
  ["GitHub", "TejkaFan", "https://github.com/TejkaFan"],
];

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

function about() {
  const i = state.info;
  const credits = CREDITS.map(([where, name, url]) => `<span>${where}</span><span>${url
    ? `<a data-url="${esc(url)}">${esc(name)}</a>`
    : `<a data-copy="${esc(name)}" title="Нажми, чтобы скопировать">${esc(name)}</a>`}</span>`).join("");
  const html = `<div class="kv">
      <span>Версия</span><span>${esc(i.version)} (сборка ${esc(i.build)})</span>
      <span>Облако</span><span>${esc(i.cloud_url)}</span>
      <span>Папка Minecraft</span><span>${esc(i.mc_dir)}</span>
      <span>Настройки</span><span>${esc(i.settings_dir)}</span>
      <span>Отчёты</span><span class="text">Сбои лаунчера сами уходят автору: описание ошибки и лог, без имени пользователя Windows</span>
    </div>
    <div class="pl-sec opts">
      <label class="check"><input type="checkbox" data-opt="tray"${i.options.tray ? " checked" : ""}><span>Крестик сворачивает лаунчер в трей (выход — правой кнопкой по значку)</span></label>
      <label class="check"><input type="checkbox" data-opt="notify"${i.options.notify ? " checked" : ""}><span>Уведомлять, когда друг запустил сервер или зашёл на него</span></label>
      <label class="check"><input type="checkbox" data-opt="presence"${i.options.presence ? " checked" : ""}><span>Показывать друзьям по Radmin, что я в сети и во что играю</span></label>
    </div>
    <div class="credits"><h4 class="px">Credits</h4><div class="kv">${credits}</div></div>`;
  dialog({title: "MineForFriends by TEJKA", html, buttons: [
    {label: "Отправить лог", value: "report"}, {label: "Папка Minecraft", value: "mc"},
    {label: "Закрыть", value: null, primary: true},
  ]}).then(v => {
    if (v === "mc") api().open_folder(i.mc_dir);
    if (v === "report") sendReport("Отчёт игрока из окна «О лаунчере»");
  });
  $("#modal-body").onchange = e => {
    const opt = e.target.dataset.opt;
    if (!opt) return;
    i.options[opt] = e.target.checked;
    api().set_option(opt, e.target.checked).catch(err => showError(errText(err)));
  };
  $("#modal-body").onclick = async e => {
    const a = e.target.closest("a");
    if (a?.dataset.url) api().open_url(a.dataset.url);
    if (a?.dataset.copy && await copyText(a.dataset.copy)) {
      a.textContent = `${a.dataset.copy} — скопировано`;
      setTimeout(() => (a.textContent = a.dataset.copy), 1500);
    }
  };
}

// ---------- Что нового ----------

// Описание релиза на GitHub (Markdown) → HTML. Строки про скачивание, установку и Credits
// нужны только на странице релиза, в лаунчере их пропускаем.
function notesHtml(md) {
  const inline = s => esc(s)
    .replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
    .replace(/`(.+?)`/g, "<code>$1</code>")
    .replace(/\[(.+?)\]\((https?:[^)\s]+)\)/g, '<a data-url="$2">$1</a>');
  const out = [];
  let list = null, skip = false;
  const closeList = () => { if (list) out.push(`<ul>${list.join("")}</ul>`); list = null; };
  for (const raw of md.split(/\r?\n/)) {
    const line = raw.trim();
    const head = line.match(/^(?:\*\*(.+?)\*\*|#+\s*(.+))$/);
    if (head) {
      closeList();
      const title = head[1] || head[2];
      skip = /установить/i.test(title);
      if (!skip && !/что нового/i.test(title)) out.push(`<h5>${inline(title)}</h5>`);
      continue;
    }
    if (!line) { closeList(); skip = false; continue; }
    if (skip || /^(\*\*)?(Скачать|Credits)/i.test(line)) continue;
    const item = line.match(/^(?:[-*]|\d+\.)\s+(.*)$/);
    if (item) (list ??= []).push(`<li>${inline(item[1])}</li>`);
    else { closeList(); out.push(`<p>${inline(line)}</p>`); }
  }
  closeList();
  return out.join("");
}

function formatDate(iso) {
  const [y, m, d] = iso.split("-");
  return d ? `${d}.${m}.${y}` : "";
}

async function changelog(title = "Что нового") {
  dialog({title, html: '<div class="muted">Загружаю список обновлений…</div>', wide: true,
          buttons: [{label: "Закрыть", value: null, primary: true}]});
  const body = $("#modal-body");
  body.onclick = e => {
    const a = e.target.closest("a[data-url]");
    if (a) api().open_url(a.dataset.url);
  };
  try {
    const releases = await api().releases();
    body.innerHTML = releases.length ? releases.map(r => `<div class="release">
        <div class="rhead"><span class="px">${esc(r.version)}</span>${r.version === state.info.version
          ? '<span class="rtag">у тебя</span>' : ""}<span class="rdate">${formatDate(r.date)}</span></div>
        ${notesHtml(r.notes)}</div>`).join("") : '<div class="muted">Обновлений пока нет.</div>';
  } catch (e) {
    body.innerHTML = `<div class="muted">${esc(errText(e))}</div>`;
  }
}

// ---------- Вылеты Minecraft ----------

async function showCrash(c) {
  const html = `<div class="kv">
      <span>Сборка</span><span class="text">${esc(c.pack)}</span>
      <span>Когда</span><span class="text">${esc(c.time)}</span>
      ${c.where ? `<span>Что делала игра</span><span class="text">${esc(c.where)}</span>` : ""}
      <span>Ошибка</span><span class="text">${esc(c.title)}</span>
    </div>
    <ul class="hints">${c.hints.map(h => `<li>${esc(h)}</li>`).join("")}</ul>`;
  const choice = await dialog({title: "Minecraft вылетел", html, buttons: [
    {label: "Открыть отчёт", value: "open"}, {label: "Отправить автору", value: "send"},
    {label: "Понятно", value: null, primary: true},
  ]});
  if (choice === "open") api().open_crash();
  if (choice === "send") {
    let message;
    try {
      message = await api().send_crash();
    } catch (e) {
      message = errText(e);
    }
    showInfo("Отчёт", message);
  }
}

// ---------- Старт ----------

// События из Python
window.app = {
  emit(name, data) {
    if (name === "status") data.scope === "boot" ? ($("#play-hint").textContent = data.text) : setStatus(data.scope, data.text);
    if (name === "progress") setProgress(data.scope, data.value);
    if (name === "servers") renderServers(data);
    if (name === "friends") renderFriends(data);
    if (name === "tlauncher") showTlauncher(data.installed);
    if (name === "ready") {
      state.ready = true;
      updatePlay();
    }
  },
};

function bind() {
  paintIcons();
  for (const b of $$(".nav[data-page]")) b.onclick = () => go(b.dataset.page);
  $("#news-btn").onclick = () => changelog();
  $("#about-btn").onclick = about;
  $("#player-chip").onclick = () => go("skin");
  $("#friends-btn").onclick = friendsDialog;
  $("#win-min").onclick = () => api().minimize();
  $("#win-close").onclick = () => api().close();
  $("#cards").onclick = e => {
    const mem = e.target.closest("[data-mem]");
    if (mem) {
      const p = state.packs.find(x => x.id === mem.dataset.mem);
      return p && memoryDialog(p.id, p.name, p.size).then(changed => changed && api().refresh_servers());
    }
    const check = e.target.closest("[data-check]");
    if (check) {
      const p = state.packs.find(x => x.id === check.dataset.check);
      return p && checkDialog(p.address, p.name);
    }
    const card = e.target.closest(".card");
    if (card) toggleCard(card.dataset.id);
  };
  $("#play-btn").onclick = play;
  $("#tl-install").onclick = installTlauncher;
  $("#friend-add").onclick = friendHosts;
  $("#radmin-btn").onclick = () => {
    api().open_radmin();
    $("#radmin-text").textContent = "Когда Radmin VPN подключится, плашка пропадёт сама (проверка раз в 30 секунд).";
  };
  $("#pick-dir").onclick = () => pickPack("dir");
  $("#pick-zip").onclick = () => pickPack("zip");
  for (const r of $$("input[name=mc-mode]")) r.onchange = () => { setCustomBusy(false); refreshLoader(); };
  $("#plan-mc").onchange = refreshLoader;
  $("#plan-loader").onchange = () => { setCustomBusy(false); refreshLoader(); };
  $("#plan-add").onclick = addPack;
  $("#recipes-btn").onclick = friendRecipes;
  $("#profiles").onclick = e => {
    const b = e.target.closest("[data-act]");
    if (b) profileAction(b.dataset.act, state.profiles[b.dataset.i]);
  };
  $("#host-btn").onclick = hostServer;
  $("#eula-link").onclick = e => { e.preventDefault(); api().open_url(state.info.eula_url); };
  $("#hosted").onclick = e => {
    const b = e.target.closest("[data-act]");
    if (b) serverAction(b.dataset.act, state.servers[b.dataset.i]);
  };
  $("#skin-choose").onclick = chooseSkin;
  $("#skin-upload").onclick = uploadSkin;
  $("#shots-pack").onchange = renderShots;
  $("#shots-refresh").onclick = () => loadShots(true);
  $("#shots").onclick = e => { const b = e.target.closest(".shot"); if (b) viewShot(shots.view[b.dataset.i]); };
  $("#cape-choose").onclick = chooseCape;
  $("#cape-remove").onclick = removeCape;
  $("#skin-anim").onclick = toggleSkinAnimation;
  for (const r of $$("input[name=model]")) r.onchange = () => { skin.model = r.value; renderSkin(); };
  $("#nick").onchange = loadServerSkin;
  $("#skin-site").onclick = () => api().open_url(state.info.skins_site);
}

let started = false;

async function start() {
  if (started) return;
  started = true;
  bind();
  const info = await api().init();
  state.info = info;
  $("#version").textContent = "v" + info.version;
  showTlauncher(info.tlauncher);
  $("#plan-loader").innerHTML = info.loaders.map(([id, name]) => `<option value="${id}">${esc(name)}</option>`).join("");
  $("#nick").value = info.nick;
  setNick(info.nick);
  drawFace($("#chip-head"), info.skin);
  $("#skin-anim").innerHTML = icon("player-pause");
  loadServerSkin();
  loadProfiles();
  if (info.updated) changelog(`Лаунчер обновлён до ${info.version}`);
  else api().last_crash().then(c => c && showCrash(c)).catch(() => {});
  api().boot();
}

// pywebview может подключить api раньше, чем загрузится этот скрипт
if (window.pywebview?.api?.init) start();
else window.addEventListener("pywebviewready", start);
