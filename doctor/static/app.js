"use strict";
// Single-page UI. User data is only ever inserted with textContent / value, never as HTML.

const $ = id => document.getElementById(id);
let token = localStorage.getItem("doctor_token") || "";
let samples = [], current = null, cur = null, pollTimer = null;

// ---------- texts ----------
const METRICS = [
  ["dur_s", "Тривалість файлу, с"], ["speech_s", "Мовлення, с"], ["lead_sil_s", "Тиша на початку, с"],
  ["trail_sil_s", "Тиша в кінці, с"], ["pause_max_s", "Найдовша пауза, с"], ["rms_dbfs", "Гучність, dBFS"],
  ["peak_dbfs", "Пік, dBFS"], ["clip_frac", "Перевантаження, частка"], ["snr_db", "Сигнал / шум, дБ"],
  ["rolloff_hz", "Смуга частот, Гц"], ["rate", "Темп"], ["text_wer", "Розбіжність транскрипту"],
];
// [what is wrong, why it matters / what repair does]
const ISSUES = {
  speech_s_min: (v, w) => [`Замало мовлення: ${v} с, потрібно від ${w} с`, "Короткий семпл гірше передає голос. Потрібен довший запис."],
  speech_s_max: (v, w) => [`Задовгий семпл: ${v} с мовлення, рекомендовано до ${w} с`, "Виправлення обріже його до 6–12 с по межі речення."],
  snr_db: (v, w) => [`Чути шум: сигнал / шум ${v} дБ, бажано від ${w}`, "Шум переходить у синтез. Виправлення з шумоочищенням його прибирає."],
  clip_frac: v => [`Перевантаження: обрізано ${(v * 100).toFixed(2)}% відліків`, "Цього не виправити; запишіть тихіше."],
  rolloff_hz: (v, w) => [`Вузька смуга частот: ${v} Гц, бажано від ${w}`, "Схоже на телефонний або дуже стиснений запис."],
  pause_max_s: v => [`Довга пауза всередині: ${v} с`, "Виправлення скоротить паузи до 0,3 с."],
  rate_min: v => [`Повільний темп: ${v}`, "Синтез буде таким самим повільним."],
  rate_max: v => [`Швидкий темп: ${v}`, "Модель може ковтати слова: їй забракне часу."],
  trail_sil_min: v => [`Запис обірвано одразу після слова: ${v} с тиші`, "Це дає зайвий звук на початку синтезу. Виправлення додає 0,2 с тиші."],
  text_wer: v => [`Транскрипт розходиться з розпізнаванням: WER ${v}`, "Неточний транскрипт — головна причина ковтання фраз і «відсебеньок». Звірте на слух."],
  asr_loop: () => ["Розпізнавання зациклилось", "Whisper застряг на протяжному звуці й загубив решту тексту. Транскрипт з ним не порівнюється; обрізання по словах вимкнено. Перевірте транскрипт на слух."],
  no_text: () => ["Транскрипт не задано", "Використано автоматичне розпізнавання. Перевірте його й збережіть."],
  no_speech: () => ["Мовлення не знайдено", "Файл порожній або містить лише шум."],
};
const NOTES = {
  text_from_asr: () => "Транскрипт узято з розпізнавання: перевірте його.",
  asr_loop: () => "Розпізнавання зациклилось: залишено ваш транскрипт, семпл не обрізано по словах.",
  text_replaced: v => `Транскрипт замінено розпізнаним: розбіжність ${v}, схоже, це інший текст.`,
  text_kept_check: v => `Транскрипт залишено, хоча розпізнавання розходиться (${v}). Whisper помиляється на сленгу та іменах; звірте на слух.`,
  cropped: v => `Обрізано на ${v} с по межі слова.`,
  not_cropped_no_asr: () => "Не обрізано: немає розпізнавання слів.",
  denoised: v => `Прибрано шум (Sidon); сигнал / шум був ${v} дБ.`,
  denoise_skipped: v => `Шумоочищення пропущено: ${tr(v)}.`,
  edges_pauses: v => `Краї підрізано: 0,1 с тиші на початку, 0,2 с у кінці; паузи скорочено до ${v} с.`,
  loudness: v => `Гучність вирівняно до ${v} dBFS.`,
};
const ERRORS = {  // server error strings -> Ukrainian
  "unsupported file type": "непідтримуваний тип файлу", "cannot decode audio": "не вдалося прочитати аудіо",
  "audio too short": "аудіо закоротке", "too many samples": "забагато семплів", "no files": "немає файлів",
  "not found": "не знайдено", "bad id": "неправильний ідентифікатор",
  "no transcript: give one or run transcription first": "немає транскрипту: введіть його або запустіть розпізнавання",
  "speech recognition failed on this sample: enter the transcript by hand": "розпізнавання на цьому семплі зациклилось: введіть транскрипт вручну",
};
const tr = msg => !msg ? msg : msg.startsWith("memory:") ? "бракує пам'яті (" + msg.slice(7).trim() + ")" : ERRORS[msg] || msg;
const VERDICT = { OK: "Придатний", WARN: "Є зауваження", BAD: "Потребує виправлення" };
const SHORT = { OK: "добре", WARN: "зауваження", BAD: "погано" };
const ACTION = { transcribe: "Розпізнавання", analyse: "Аналіз", repair: "Виправлення" };
const STATE = { queued: "у черзі", running: "триває", done: "готово", error: "помилка" };

// ---------- helpers ----------
function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null && v !== false) e.setAttribute(k, v === true ? "" : v);
  }
  for (const k of kids.flat()) if (k !== null && k !== undefined && k !== false) e.append(k instanceof Node ? k : String(k));
  return e;
}
const svg = d => { const s = document.createElementNS("http://www.w3.org/2000/svg", "svg"); s.setAttribute("viewBox", "0 0 16 16");
  const p = document.createElementNS("http://www.w3.org/2000/svg", "path"); p.setAttribute("d", d); s.append(p); return s; };
const ICON_PLAY = "M4 2.5v11l9-5.5z", ICON_PAUSE = "M4 2.5h3v11H4zM9 2.5h3v11H9z";
const withToken = url => token ? url + (url.includes("?") ? "&" : "?") + "t=" + encodeURIComponent(token) : url;
const ACUTE = new RegExp(String.fromCharCode(0x301), "g");
const words = t => t.normalize("NFC").replace(ACUTE, "").toLowerCase().replace(/[^\p{L}\p{N}'’ʼ]+/gu, " ").trim();
const fmt = v => v === null || v === undefined ? "—" : typeof v === "number" ? String(Math.round(v * 1000) / 1000).replace(".", ",") : String(v);
const sec = t => `${Math.floor(t / 60)}:${String(Math.floor(t % 60)).padStart(2, "0")}`;
const busyJob = m => m.job && (m.job.state === "queued" || m.job.state === "running");

async function api(path, opts = {}) {
  const headers = Object.assign({}, opts.headers || {});
  if (token) headers["X-Token"] = token;
  if (opts.json !== undefined) { headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(opts.json); }
  const r = await fetch(path, Object.assign({}, opts, { headers }));
  if (r.status === 401) {
    const t = prompt("Введіть токен доступу до сервісу:");
    if (t !== null) { token = t.trim(); localStorage.setItem("doctor_token", token); return api(path, opts); }
    throw new Error("unauthorized");
  }
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.error || r.statusText);
  return d;
}

// ---------- status / settings ----------
let settingsFilled = false;
async function loadStatus() {
  const s = await api("/api/status");
  const dev = s.device === "cuda" ? "GPU" : "CPU";
  const names = { asr: "розпізнавання", sidon: "шумоочищення" };
  const loaded = s.loaded.length ? s.loaded.map(m => `${names[m.name] || m.name}${m.busy ? " (працює)" : ""}`).join(", ") : "жодної";
  $("status").replaceChildren(
    el("div", {}, `${dev}${s.cuda ? "" : ", GPU недоступний"}; вільно ${(s.free_mb / 1024).toFixed(1).replace(".", ",")} ГБ`),
    el("div", {}, `Завантажені моделі: ${loaded}${s.queue ? `; у черзі ${s.queue}` : ""}`));
  if (!settingsFilled) {
    settingsFilled = true;
    for (const m of s.asr_models) $("s-asr").append(el("option", { value: m }, m.replace("openai/", "")));
    $("s-device").value = s.settings.device; $("s-asr").value = s.settings.asr_model;
    $("s-idle").value = s.settings.idle_unload_s; $("s-reserve").value = s.settings.reserve_mb;
  }
}
$("s-save").onclick = async () => {
  const r = await api("/api/settings", { method: "POST", json: { device: $("s-device").value, asr_model: $("s-asr").value,
    idle_unload_s: $("s-idle").value, reserve_mb: $("s-reserve").value } });
  $("s-idle").value = r.idle_unload_s; $("s-reserve").value = r.reserve_mb;
  loadStatus();
};
$("s-unload").onclick = async () => { await api("/api/unload", { method: "POST" }); loadStatus(); };

// ---------- upload ----------
const drop = $("drop");
drop.onclick = () => $("file").click();
drop.ondragover = e => { e.preventDefault(); drop.classList.add("over"); };
drop.ondragleave = () => drop.classList.remove("over");
drop.ondrop = e => { e.preventDefault(); drop.classList.remove("over"); upload(e.dataTransfer.files); };
document.body.ondragover = e => e.preventDefault();
document.body.ondrop = e => { e.preventDefault(); if (e.target !== drop) upload(e.dataTransfer.files); };
$("file").onchange = e => { upload(e.target.files); e.target.value = ""; };

async function upload(files) {
  if (!files || !files.length) return;
  const fd = new FormData();
  for (const f of files) fd.append("files", f);
  $("upmsg").textContent = "Завантажую…";
  try {
    const d = await api("/api/samples", { method: "POST", body: fd });
    const errs = d.errors.map(e => `${e.name}: ${tr(e.error)}`).join("; ");
    $("upmsg").textContent = (d.created.length ? `Додано ${d.created.length}. ` : "") + (errs ? `Не прийнято: ${errs}` : "");
    await loadList();
    if (d.created.length) select(d.created[0].id);
  } catch (e) { $("upmsg").textContent = "Не вдалося завантажити: " + tr(e.message); }
}

// ---------- list ----------
async function loadList() {
  samples = await api("/api/samples");
  $("list").replaceChildren(...samples.map(s => {
    const busy = s.job && (s.job.state === "queued" || s.job.state === "running");
    const v = s.fixed_verdict || s.verdict;
    return el("li", {}, el("button", { type: "button", "aria-current": s.id === current ? "true" : "false",
        title: `${s.name}${s.verdict ? ": " + SHORT[s.verdict] : ""}${s.fixed_verdict ? ", після виправлення " + SHORT[s.fixed_verdict] : ""}`,
        onclick: () => select(s.id) },
      el("span", { class: "dot " + (busy ? "busy" : v || "") }),
      el("span", { class: "nm" }, s.name),
      el("span", { class: "aft" }, busy ? STATE[s.job.state] : s.fixed_verdict ? "виправлено" : "")));
  }));
  if (!current) showEmpty();
  schedule();
}
function schedule() {
  clearTimeout(pollTimer);
  const busy = samples.some(s => s.job && (s.job.state === "queued" || s.job.state === "running"));
  pollTimer = setTimeout(async () => {
    try { await loadList(); await loadStatus(); if (current) await refresh(); } catch { schedule(); }
  }, busy ? 1500 : 8000);
}

// ---------- ECG strip ----------
// One strip per (sample, kind, version); it survives re-renders, so playback is never cut off.
function strip(id, kind, version, caption) {
  const q = kind === "fixed" ? "?v=" + Math.round(version) : "";
  const audio = el("audio", { preload: "none", src: withToken(`/api/samples/${id}/audio/${kind}${q}`) });
  const btn = el("button", { class: "play", type: "button", "aria-label": "Відтворити" }, svg(ICON_PLAY));
  const canvas = el("canvas", { class: "ecg", tabindex: "0", role: "slider", "aria-label": `${caption}: позиція відтворення`,
    "aria-valuemin": "0", "aria-valuenow": "0" });
  const time = el("span", {}, "0:00");
  const note = el("span", { class: "fine" });
  const box = el("div", { class: "strip" },
    el("div", { class: "strip-cap" }, note, time), btn, canvas, audio);
  let data = null, raf = 0, cut = null;
  const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  function draw() {
    const dpr = window.devicePixelRatio || 1, w = canvas.clientWidth, h = canvas.clientHeight;
    if (!w) return;
    canvas.width = w * dpr; canvas.height = h * dpr;
    const g = canvas.getContext("2d"); g.scale(dpr, dpr);
    // ECG paper: fine 0.04 s grid, bold every 5 cells
    const dur = data ? data.dur : 1, px = w / dur, cell = Math.max(px * 0.04, 4);
    for (let i = 0, x = 0; x <= w; i++, x = i * cell) {
      g.fillStyle = css(i % 5 ? "--grid2" : "--grid"); g.fillRect(Math.round(x), 0, 1, h);
    }
    for (let i = 0, y = h / 2; y <= h; i++, y = h / 2 + i * cell) {
      g.fillStyle = css(i % 5 ? "--grid2" : "--grid"); g.fillRect(0, Math.round(y), w, 1); g.fillRect(0, Math.round(h - y), w, 1);
    }
    if (!data) return;
    // speech zones, silence at the edges, long pauses
    g.fillStyle = css("--speech");
    for (const [a, b] of data.speech) g.fillRect(a * px, 0, (b - a) * px, h);
    const hatch = (a, b) => {
      g.save(); g.beginPath(); g.rect(a * px, 0, (b - a) * px, h); g.clip();
      g.strokeStyle = css("--warn"); g.globalAlpha = .35; g.lineWidth = 1;
      for (let x = a * px - h; x < b * px + h; x += 6) { g.beginPath(); g.moveTo(x, h); g.lineTo(x + h, 0); g.stroke(); }
      g.restore();
    };
    if (data.speech.length) {
      const first = data.speech[0][0], last = data.speech[data.speech.length - 1][1];
      if (first > 0.4) hatch(0, first);
      if (dur - last > 0.6) hatch(last, dur);
      for (let i = 0; i + 1 < data.speech.length; i++) {
        const a = data.speech[i][1], b = data.speech[i + 1][0];
        if (b - a > 1.0) hatch(a, b);
      }
    }
    const n = data.peaks.length, mid = h / 2, amp = h * 0.46;
    g.fillStyle = css("--trace");
    for (let i = 0; i < n; i++) {
      const [lo, hi] = data.peaks[i], x = i / n * w, top = mid - hi * amp, bot = mid - lo * amp;
      g.fillRect(x, top, Math.max(w / n - 0.3, 0.7), Math.max(bot - top, 1));
    }
    if (cut) {  // where the repair cropped the original
      g.strokeStyle = css("--bad"); g.setLineDash([4, 3]); g.lineWidth = 2;
      g.beginPath(); g.moveTo(cut * px, 0); g.lineTo(cut * px, h); g.stroke(); g.setLineDash([]);
    }
    if (audio.currentTime > 0) { g.fillStyle = css("--act"); g.fillRect(audio.currentTime * px - 1, 0, 2, h); }
  }
  function tick() {
    draw(); time.textContent = `${sec(audio.currentTime)} / ${sec(data ? data.dur : 0)}`;
    canvas.setAttribute("aria-valuenow", Math.round(audio.currentTime));
    if (!audio.paused) raf = requestAnimationFrame(tick);
  }
  const seek = t => { audio.currentTime = Math.max(0, Math.min(t, data ? data.dur : 0)); tick(); };
  btn.onclick = () => audio.paused ? audio.play() : audio.pause();
  audio.onplay = () => { btn.replaceChildren(svg(ICON_PAUSE)); btn.setAttribute("aria-label", "Пауза"); raf = requestAnimationFrame(tick); };
  audio.onpause = audio.onended = () => { btn.replaceChildren(svg(ICON_PLAY)); btn.setAttribute("aria-label", "Відтворити"); cancelAnimationFrame(raf); tick(); };
  canvas.onclick = e => {
    if (!data) return;
    const r = canvas.getBoundingClientRect(); seek((e.clientX - r.left) / r.width * data.dur);
    if (audio.paused) audio.play();
  };
  canvas.onkeydown = e => {
    if (e.key === " " || e.key === "Enter") { e.preventDefault(); btn.click(); }
    else if (e.key === "ArrowRight") { e.preventDefault(); seek(audio.currentTime + 1); }
    else if (e.key === "ArrowLeft") { e.preventDefault(); seek(audio.currentTime - 1); }
  };
  api(`/api/samples/${id}/peaks/${kind}`).then(d => { data = d; canvas.setAttribute("aria-valuemax", Math.round(d.dur)); tick(); });
  new ResizeObserver(() => draw()).observe(canvas);
  box.setCut = c => { if (c !== cut) { cut = c; note.textContent = c ? "пунктир — місце обрізання" : ""; draw(); } };
  return box;
}

// ---------- detail: five steps, each re-rendered only when its own data changes ----------
let view = null;  // {id, els: {head, s1..s5}, keys: {...}, strips: {...}}

async function select(id) {
  current = id; history.replaceState(null, "", "#" + id);
  await refresh(); loadList();
  $("detail").scrollTop = 0;
}

async function refresh() {
  let m;
  try { m = await api("/api/samples/" + current); } catch { current = null; showEmpty(); return; }
  cur = m;
  render(m);
}

function diagnosis(issues) {
  if (!issues || !issues.length) return el("p", { class: "clean" }, "Зауважень немає: семпл придатний для клонування.");
  return el("ul", { class: "dx" }, issues.map(([lvl, key, v, w]) => {
    const [what, why] = (ISSUES[key] || (() => [key, ""]))(fmt(v), fmt(w));
    return el("li", { class: lvl }, what, el("span", { class: "fine" }, why));
  }));
}

function metrics(before, after) {
  const rows = METRICS.filter(([k]) => (before && before[k] !== undefined) || (after && after[k] !== undefined));
  return el("table", {},
    el("tr", {}, el("th", {}, ""), el("th", {}, after ? "До" : "Значення"), after ? el("th", {}, "Після") : null),
    rows.map(([k, label]) => {
      const a = before ? before[k] : undefined, b = after ? after[k] : undefined;
      const changed = after && fmt(a) !== fmt(b);
      return el("tr", {}, el("td", {}, label), el("td", {}, fmt(a)), after ? el("td", { class: changed ? "chg" : "" }, fmt(b)) : null);
    }));
}

// step frame: number, title, state ("done" | "todo" | "wait" | "") and body
function step(n, title, state, stateText, ...body) {
  return [el("div", { class: "step-h" }, el("span", { class: "num " + state }, String(n)), el("h2", {}, title),
    stateText ? el("span", { class: "st " + state }, stateText) : null), ...body.flat()];
}

function render(m) {
  if (!view || view.id !== m.id) {
    view = { id: m.id, keys: {}, strips: {}, els: {} };
    for (const k of ["head", "s1", "s2", "s3", "s4", "s5"]) view.els[k] = el("section", { class: k === "head" ? "top" : "step" });
    $("detail").replaceChildren(el("div", { class: "wrap" }, view.els.head, view.els.s1,
      el("div", { class: "pair" }, view.els.s2, view.els.s3), view.els.s4, view.els.s5));
  }
  const busy = busyJob(m), a = m.analysis, f = m.fixed;
  const job = m.job && m.job.state !== "done" ? m.job : null;
  const running = act => busy && m.job.action === act;
  // update a section only when its key changes; never rebuild the one the user is typing in
  const put = (name, key, build) => {
    if (view.keys[name] === key) return;
    const box = view.els[name];
    if (box.contains(document.activeElement) && /INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) return;
    view.keys[name] = key;
    box.replaceChildren(...[build()].flat().filter(Boolean));
  };
  const J = x => JSON.stringify(x);

  put("head", J([m.name, m.verdict, f && f.verdict, job, a && [a.dur_s, a.speech_s]]), () => [
    el("div", { class: "head" }, el("h1", {}, m.name),
      el("button", { class: "btn quiet", type: "button", onclick: async () => {
        if (!confirm(`Видалити «${m.name}» разом із виправленою версією?`)) return;
        await api("/api/samples/" + m.id, { method: "DELETE" }); current = null; history.replaceState(null, "", "#");
        showEmpty(); loadList(); } }, "Видалити")),
    el("p", { class: "meta" },
      f ? el("span", { class: "verdict " + f.verdict }, "Після виправлення: " + VERDICT[f.verdict].toLowerCase())
        : m.verdict ? el("span", { class: "verdict " + m.verdict }, VERDICT[m.verdict]) : el("span", {}, "Ще не перевірено"),
      a ? el("span", {}, `${fmt(a.dur_s)} с, з них мовлення ${fmt(a.speech_s)} с`) : null,
      job ? el("span", { class: "job" + (job.state === "error" ? " err" : "") },
        `${ACTION[job.action] || job.action}: ${STATE[job.state] || job.state}${job.msg ? ", " + tr(job.msg) : ""}`) : null)]);

  // 1. original: the strip element is created once per sample
  if (!view.strips.source) {
    view.strips.source = strip(m.id, "source", 0, "Оригінал");
    view.els.s1.replaceChildren(...step(1, "Оригінал", "done", "",
      el("div", { class: "strips" }, view.strips.source,
        el("div", { class: "legend" }, el("span", {}, el("i", { class: "lg-speech" }), "мовлення"),
          el("span", {}, el("i", { class: "lg-sil" }), "зайва тиша або довга пауза"))),
      el("div", { class: "row" }, el("a", { class: "btn", href: withToken(`/api/samples/${m.id}/download/source`) }, "Завантажити оригінал і .info"))));
  }
  view.strips.source.setCut(f ? (f.notes.find(([c]) => c === "cropped") || [])[1] || null : null);

  // 2. info
  put("s2", J([m.text, m.asr_text, m.info_extra, running("transcribe") || running("analyse")]), () => {
    const info = m.info_extra || {};
    const fText = el("textarea", { id: "f-text", rows: "4" }); fText.value = m.text || "";
    // defaults match the server (INFO_DEFAULTS): file name, OmniVoice guidance 2.0, speed 1.0
    const fName = el("input", { value: info.name || m.name.replace(/\.[^.]+$/, "") });
    const fCfg = el("input", { type: "number", step: "0.1", min: "0", value: info.cfg_strength ?? 2.0 });
    const fSpeed = el("input", { type: "number", step: "0.05", min: "0.1", value: info.speed ?? 1.0 });
    const saved = el("span", { class: "fine", role: "status" });
    const save = async () => {
      await api("/api/samples/" + m.id, { method: "PATCH", json: { text: fText.value,
        info: { name: fName.value, cfg_strength: fCfg.value, speed: fSpeed.value } } });
      saved.textContent = "Збережено"; document.activeElement.blur(); await refresh(); loadList();
    };
    const asrBusy = running("transcribe") || (running("analyse") && !m.asr_text);
    const banner = !m.text ? el("div", { class: "banner" },
      m.asr_text ? [el("p", {}, "Транскрипт не задано. Розпізнано автоматично:"), el("q", {}, m.asr_text),
        el("button", { class: "btn pri", type: "button", onclick: () => { fText.value = m.asr_text; fText.focus(); } }, "Вставити в транскрипт")]
        : asrBusy ? el("p", {}, "Транскрипт не задано. Йде автоматичне розпізнавання…")
          : [el("p", {}, "Транскрипт не задано."), el("button", { class: "btn", type: "button", onclick: () => run("transcribe") }, "Розпізнати автоматично")]) : null;
    return step(2, "Info", m.text ? "done" : "todo", m.text ? "" : "потрібен транскрипт", banner,
      el("label", { for: "f-text" }, "Транскрипт: дослівно те, що звучить у семплі", fText),
      m.text && m.asr_text && words(m.text) !== words(m.asr_text) ? el("p", { class: "asr" }, "Розпізнано: ", m.asr_text) : null,
      el("div", { class: "fields" }, el("label", {}, "Ім'я голосу", fName), el("label", {}, "cfg_strength", fCfg), el("label", {}, "speed", fSpeed)),
      el("div", { class: "row" }, el("button", { class: "btn pri", type: "button", onclick: save }, "Зберегти info"),
        el("button", { class: "btn", type: "button", disabled: busy, onclick: () => run("transcribe") }, "Розпізнати знову"), saved));
  });

  // 3. diagnosis
  put("s3", J([m.verdict, m.issues, a, m.analysis_note, busy]), () =>
    step(3, "Діагноз", !a ? "wait" : m.verdict === "OK" ? "done" : "todo",
      !a ? (busy ? "аналіз триває" : "") : VERDICT[m.verdict].toLowerCase(),
      a ? diagnosis(m.issues) : el("p", { class: "fine" }, busy ? "Аналіз триває…" : "Семпл ще не аналізувався."),
      m.analysis_note ? el("p", { class: "fine" }, "Розпізнавання пропущено: ", tr(m.analysis_note.replace("asr skipped: ", ""))) : null,
      a ? el("details", { class: "all" }, el("summary", {}, "Усі виміри"), metrics(a)) : null,
      el("div", { class: "row mt" }, el("button", { class: "btn", type: "button", disabled: busy, onclick: () => run("analyse") }, "Перевірити знову"))));

  // 4. repair
  put("s4", J([busy, running("repair"), !!(m.text || m.asr_text), f && f.notes, f && f.denoise]), () => {
    let mode = (f && f.denoise) || "auto";
    const seg = el("div", { class: "seg", role: "group", "aria-label": "Шумоочищення" });
    const modes = [["auto", "Лише для шумних"], ["on", "Завжди"], ["off", "Ніколи"]];
    const paintSeg = () => seg.replaceChildren(...modes.map(([k, l]) => el("button", { type: "button", "aria-pressed": String(k === mode),
      onclick: () => { mode = k; paintSeg(); } }, l)));
    paintSeg();
    const canFix = !!(m.text || m.asr_text);
    return step(4, "Виправлення", running("repair") ? "wait" : f ? "done" : "todo", running("repair") ? "триває" : f ? "виконано" : "",
      el("p", { class: "fine" }, "Обрізає задовгий запис, прибирає шум, вирівнює краї, паузи й гучність. Шумоочищення трохи змінює тембр, тож для чистих записів воно не потрібне."),
      el("div", { class: "row mt" }, el("span", { class: "fine" }, "Шумоочищення:"), seg,
        el("button", { class: "btn pri", type: "button", disabled: busy || !canFix, onclick: () => run("repair", { denoise: mode }) },
          running("repair") ? "Виправляю…" : f ? "Виправити ще раз" : "Виправити"),
        !canFix ? el("span", { class: "fine" }, "Спершу заповніть транскрипт у кроці 2.") : null),
      f ? [el("h3", {}, "Що зроблено"), el("ol", { class: "steps" }, f.notes.map(([c, v]) => el("li", {}, (NOTES[c] || (() => c))(fmt(v)))))] : null);
  });

  // 5. result
  put("s5", J([f && f.t, f && f.verdict]), () => {
    if (!f) return step(5, "Після виправлення", "", "", el("p", { class: "fine" }, "Тут з'явиться виправлений семпл, його діагноз і файли для завантаження."));
    view.strips.fixed = strip(m.id, "fixed", f.t, "Виправлений");
    return step(5, "Після виправлення", f.verdict === "BAD" ? "todo" : "done", VERDICT[f.verdict].toLowerCase(),
      el("div", { class: "strips" }, view.strips.fixed),
      el("div", { class: "pair" },
        el("div", {}, el("h3", {}, "Транскрипт"), el("p", { class: "newtext" }, f.text),
          el("h3", {}, "Діагноз"), diagnosis(f.issues),
          el("div", { class: "row mt" },
            el("a", { class: "btn pri", href: withToken(`/api/samples/${m.id}/download/fixed`) }, "Завантажити .mp3 і .info"))),
        el("div", {}, el("h3", {}, "Виміри до й після"), metrics(a || {}, f.analysis))));
  });
}

function showEmpty() {
  view = null;
  const req = [["6–12 секунд", "чистого мовлення; коротший семпл гірше передає голос"],
    ["Точний транскрипт", "дослівно те, що звучить; помилки в ньому — головна причина ковтання фраз"],
    ["Тиха кімната", "шум і луна переходять у синтез"],
    ["Тиша в кінці", "запис, обірваний на слові, дає зайвий звук на початку синтезу"],
    ["Звичайний темп", "синтез повторює темп семпла"]];
  $("detail").replaceChildren(el("div", { class: "empty" },
    el("h1", {}, samples.length ? "Оберіть семпл ліворуч" : "Додайте перший семпл"),
    el("p", {}, "Сервіс перевіряє голосові семпли для OmniVoice і виправляє те, що можна виправити. Хороший семпл:"),
    el("ul", { class: "req" }, req.map(([a, b]) => el("li", {}, el("b", {}, a), el("span", {}, b))))));
}

async function run(action, body) {
  await api(`/api/samples/${current}/${action}`, { method: "POST", json: body || {} });
  await refresh(); loadList();
}

showEmpty();
loadStatus().catch(() => {});
loadList().then(() => { const h = location.hash.slice(1); if (/^[0-9a-f]{32}$/.test(h)) select(h); });
