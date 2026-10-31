// Интерфейс лаунчера. Логика живёт в Python (класс Api в launcher.py):
// сюда приходят события через app.emit(), отсюда — вызовы pywebview.api.*
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const api = () => window.pywebview.api;

const TITLES = {play: "Наши серверы", custom: "Свои сборки", server: "Сервер", skin: "Скин"};
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
const showError = text => dialog({title: "Что-то пошло не так", text});
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

function showSkin(b64) {
  drawFace($("#chip-head"), b64);
  drawFace($("#skin-face"), b64);
}

function setNick(nick) {
  $("#chip-nick").textContent = nick || "Без ника";
}

// ---------- Наши серверы ----------

function renderServers(d) {
  Object.assign(state, {packs: d.packs, cloud: d.cloud, selected: d.selected});
  const line = $("#cloud-line");
  if (!d.cloud) {
    line.className = "line err";
    line.textContent = "Облако недоступно — сборки сейчас не загрузить. Проверь Radmin VPN или подожди, пока хост включит сервер.";
    $("#cards").innerHTML = "";
    updatePlay();
    return;
  }
  line.className = "line muted";
  line.textContent = "Нажми на карточку, чтобы ставить и обновлять эту сборку. Индикатор обновляется сам.";
  $("#cards").innerHTML = d.packs.map(p => {
    const st = p.status;
    const on = state.selected.includes(p.id);
    const size = p.size ? `${Math.round(p.size / 2 ** 20)} МБ` : "";
    const fill = st && st.max ? Math.min(100, Math.round(st.online / st.max * 100)) : 0;
    const who = st
      ? `<div class="who online">${icon("users")}Игроков: ${st.online} / ${st.max}</div>`
      : `<div class="who off">${icon("moon")}Хост сейчас не играет</div>`;
    return `<div class="card${on ? " on" : ""}" data-id="${esc(p.id)}">
      <div class="top"><span class="name">${esc(p.name)}</span><span class="badge${st ? " online" : ""}">${st ? "онлайн" : "выключен"}</span></div>
      <div class="desc">${esc([p.description, size].filter(Boolean).join(" · "))}</div>
      ${who}
      <div class="fill"><div style="width:${fill}%"></div></div>
      <div class="pick"><span class="tick">${icon("check")}</span>${on ? "Ставится и обновляется" : "Не ставится"}</div>
    </div>`;
  }).join("");
  updatePlay();
}

function updatePlay() {
  const chosen = state.packs.filter(p => state.selected.includes(p.id)).map(p => `«${p.name}»`);
  $("#play-btn").disabled = !state.ready || !state.cloud || !chosen.length || state.busy;
  if (!state.ready || state.busy) return;
  $("#play-hint").textContent = !state.cloud ? "Нет связи с облаком"
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
        <button class="iconbtn" data-act="open" data-i="${i}" title="Открыть папку игры">${icon("folder")}</button>
        <button class="iconbtn del" data-act="remove" data-i="${i}" title="Убрать из TLauncher">${icon("trash")}</button>
      </div></div>`).join("")
    : `<div class="empty">Пока пусто. Добавь сборку слева — она появится здесь и в TLauncher.</div>`;
}

async function profileAction(act, p) {
  if (act === "open") return api().open_folder(p.game_dir);
  const ok = await confirmBox("Убрать сборку?", `Убрать версию «${p.version_id}» из TLauncher?\n\nПапка игры с мирами и настройками останется:\n${p.game_dir}`, "Убрать");
  if (!ok) return;
  try {
    await api().remove_profile(p.version_id);
  } catch (e) {
    showError("Не удалось удалить версию:\n\n" + errText(e));
  }
  loadProfiles();
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
    <div class="item"><div class="grow"><div class="name">${esc(s.name)}</div>
      <div class="meta">${esc(s.address)} · ${esc(s.minecraft)} ${esc(s.loader_name)}</div></div>
      <div class="tools">
        <button class="iconbtn go" data-act="start" data-i="${i}" title="Запустить">${icon("player-play")}</button>
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

async function serverAction(act, s) {
  try {
    if (act === "start") setStatus("host", await api().start_server(s.path));
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

async function uploadSkin() {
  const nick = $("#nick").value.trim();
  if (!nick) return showInfo("Нужен ник", "Впиши ник, под которым играешь в TLauncher.");
  const status = $("#skin-status");
  $("#skin-pick").disabled = true;
  try {
    const res = await api().upload_skin(nick, $("input[name=model]:checked").value === "slim");
    if (res) {
      status.className = "line";
      status.textContent = res.message;
      showSkin(res.skin);
      setNick(nick);
      $("#skin-file").textContent = "Этот скин сейчас на сервере";
    }
  } catch (e) {
    status.className = "line err";
    status.textContent = "Скин не загружен";
    showError(errText(e));
  }
  $("#skin-pick").disabled = false;
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
    </div>
    <div class="credits"><h4 class="px">Credits</h4><div class="kv">${credits}</div></div>`;
  dialog({title: "MineForFriends by TEJKA", html, buttons: [
    {label: "Папка Minecraft", value: "mc"}, {label: "Закрыть", value: null, primary: true},
  ]}).then(v => {
    if (v === "mc") api().open_folder(i.mc_dir);
  });
  $("#modal-body").onclick = async e => {
    const a = e.target.closest("a");
    if (a?.dataset.url) api().open_url(a.dataset.url);
    if (a?.dataset.copy && await copyText(a.dataset.copy)) {
      a.textContent = `${a.dataset.copy} — скопировано`;
      setTimeout(() => (a.textContent = a.dataset.copy), 1500);
    }
  };
}

// ---------- Старт ----------

// События из Python
window.app = {
  emit(name, data) {
    if (name === "status") data.scope === "boot" ? ($("#play-hint").textContent = data.text) : setStatus(data.scope, data.text);
    if (name === "progress") setProgress(data.scope, data.value);
    if (name === "servers") renderServers(data);
    if (name === "ready") {
      state.ready = true;
      updatePlay();
    }
  },
};

function bind() {
  paintIcons();
  for (const b of $$(".nav[data-page]")) b.onclick = () => go(b.dataset.page);
  $("#about-btn").onclick = about;
  $("#player-chip").onclick = () => go("skin");
  $("#win-min").onclick = () => api().minimize();
  $("#win-close").onclick = () => api().close();
  $("#cards").onclick = e => {
    const card = e.target.closest(".card");
    if (card) toggleCard(card.dataset.id);
  };
  $("#play-btn").onclick = play;
  $("#pick-dir").onclick = () => pickPack("dir");
  $("#pick-zip").onclick = () => pickPack("zip");
  for (const r of $$("input[name=mc-mode]")) r.onchange = () => { setCustomBusy(false); refreshLoader(); };
  $("#plan-mc").onchange = refreshLoader;
  $("#plan-loader").onchange = () => { setCustomBusy(false); refreshLoader(); };
  $("#plan-add").onclick = addPack;
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
  $("#skin-pick").onclick = uploadSkin;
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
  $("#plan-loader").innerHTML = info.loaders.map(([id, name]) => `<option value="${id}">${esc(name)}</option>`).join("");
  $("#nick").value = info.nick;
  setNick(info.nick);
  showSkin(info.skin);
  if (info.skin) $("#skin-file").textContent = "Этот скин сейчас на сервере";
  loadProfiles();
  api().boot();
}

// pywebview может подключить api раньше, чем загрузится этот скрипт
if (window.pywebview?.api?.init) start();
else window.addEventListener("pywebviewready", start);
