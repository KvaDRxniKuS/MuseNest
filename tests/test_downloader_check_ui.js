/* UI test for the downloader-check progress bar.
 *
 * Loads the *real* static/app.js into a Node vm sandbox with a minimal DOM stub
 * and exercises ytCheckRender / ytCheckStageLabel / ytCheckSetButton /
 * ytCheckSummary — the functions that draw the progress bar under the
 * "Проверить загрузчик" button.
 *
 * Run:  node tests/test_downloader_check_ui.js
 */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const APP_JS = path.join(__dirname, "..", "static", "app.js");
const src = fs.readFileSync(APP_JS, "utf8");

let failures = 0;
let passed = 0;

function check(name, fn) {
  try {
    fn();
    passed++;
    console.log("  ok   " + name);
  } catch (e) {
    failures++;
    console.log("  FAIL " + name + "\n       " + e.message);
  }
}

/* ------------------------- minimal DOM stub ------------------------- */
const IDS = [
  "ytCheckProgress", "ytCheckBar", "ytCheckStageText", "ytCheckPct",
  "ytCheckDetail", "ytCheckResult", "ytCheckStatus", "ytCheckBtn",
  "langSelect", "artistSearch", "artistInput", "blackInput", "proxyUrl",
  "saveFolder",
  // source / downloader picker (applySourceVisibility)
  "musicSource", "downloader", "sourceHint", "downloaderHint", "sourceHomeBtn",
  "field_clientId", "field_clientSecret", "field_yandexToken",
  "field_zvukToken", "field_ytCookieBrowser",
  "yandexToken", "zvukToken", "clientId", "clientSecret", "ytCookieBrowser",
  // the rest of populateForm()
  "appVersion", "audioQuality", "interval", "tolerance", "maxAlbums", "threads",
  "monitorEnabled", "fallback", "artistCount", "blackList",
  // YouTube request router tiles
  "dlParallel", "dlDelay", "txt_dlParallel", "txt_dlDelay",
  "txt_interval", "txt_tolerance", "txt_maxAlbums", "txt_threads",
  // target folder for newly added artists
  "targetFolder", "targetFolderLabel", "libraryContainer", "libraryStatusMsg",
  "artistSuggest", "saveMsg",
];

function makeContext(lang, opts) {
  opts = opts || {};
  const elements = {};
  for (const id of IDS) {
    elements[id] = {
      id,
      tagName: id === "langSelect" ? "SELECT" : "DIV",
      textContent: "",
      innerHTML: "",
      className: "",
      placeholder: "",
      disabled: false,
      value: "",
      style: {},
      dataset: {},
      options: [],
      appendChild() {},
    };
  }
  const document = {
    getElementById: (id) => elements[id] || null,
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener: () => {},
    createElement: () => ({ style: {}, dataset: {}, appendChild() {} }),
    title: "",
  };
  // Timers are queued instead of scheduled so the polling loop can be driven
  // deterministically from the test.
  const timers = [];
  const sandbox = {
    document,
    console,
    setTimeout: (fn) => { timers.push(fn); return timers.length; },
    clearTimeout: () => {},
    setInterval: () => 0,
    clearInterval: () => {},
    fetch: opts.fetch || (() => Promise.reject(new Error("no network in UI test"))),
    alert: () => {},
    localStorage: {
      getItem: () => lang,
      setItem: () => {},
    },
    location: { href: "http://localhost/" },
    __els: elements,
    __timers: timers,
    __calls: [],
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  // Test-only hooks so we can drive the module-level `let` bindings.
  vm.runInContext(
    src + "\n;globalThis.__setLang = (l) => { currentLang = l; };" +
          "globalThis.__setDict = (d) => { loadedTranslations = d; };" +
          "globalThis.__setConfig = (c) => { config = c; };" +
          "globalThis.__setArtists = (a) => { allArtists = a; };" +
          "globalThis.__getArtists = () => allArtists;" +
          "globalThis.__setLibrary = (l) => { libraryData = l; };" +
          "globalThis.__getLibrary = () => libraryData;" +
          "globalThis.__getExpanded = () => expandedNodes;",
    sandbox,
    { filename: "app.js" }
  );
  return sandbox;
}

console.log("\n— downloader check UI (static/app.js in a DOM stub) —");

const ru = makeContext("RU-Russian");
const en = makeContext("EN-English");

/* ------------------------- progress rendering ------------------------- */
check("bar width follows percent, no indeterminate class", () => {
  ru.ytCheckRender({ status: "running", stage_key: "download", percent: 42,
                     elapsed_s: 3.2, message: "1.2 МБ / 3.0 МБ · 256 КБ/с" });
  const els = ru.__els;
  assert.strictEqual(els.ytCheckBar.style.width, "42%");
  assert.strictEqual(els.ytCheckBar.className, "tbar-fill");
  assert.strictEqual(els.ytCheckDetail.textContent, "1.2 МБ / 3.0 МБ · 256 КБ/с");
  assert.ok(els.ytCheckPct.textContent.includes("42%"), els.ytCheckPct.textContent);
  assert.ok(els.ytCheckPct.textContent.includes("3.2 с"), els.ytCheckPct.textContent);
  assert.strictEqual(els.ytCheckProgress.style.display, "");
});

check("percent 0 while running -> indeterminate sliding bar, no '0%'", () => {
  ru.ytCheckRender({ status: "running", stage_key: "prepare", percent: 0, elapsed_s: 0.4 });
  const els = ru.__els;
  assert.ok(els.ytCheckBar.className.includes("ind"), els.ytCheckBar.className);
  assert.strictEqual(els.ytCheckBar.style.width, "40%");
  assert.ok(!els.ytCheckPct.textContent.includes("%"),
            "must not show a frozen 0%, got: " + els.ytCheckPct.textContent);
});

check("percent above 100 is clamped to 100%", () => {
  ru.ytCheckRender({ status: "running", stage_key: "download", percent: 150 });
  assert.strictEqual(ru.__els.ytCheckBar.style.width, "100%");
});

check("running + percent <= 0 means 'unknown' -> indeterminate, not a 2% sliver", () => {
  // A non-positive percent while running can only mean "nothing measurable yet",
  // so the sliding bar wins over the clamp floor.
  ru.ytCheckRender({ status: "running", stage_key: "download", percent: -5 });
  assert.ok(ru.__els.ytCheckBar.className.includes("ind"), ru.__els.ytCheckBar.className);
  assert.strictEqual(ru.__els.ytCheckBar.style.width, "40%");
});

check("finished state clamps the 2% floor (bar never collapses to nothing)", () => {
  ru.ytCheckRender({ status: "done", stage_key: "failed", percent: 0 });
  assert.ok(!ru.__els.ytCheckBar.className.includes("ind"));
  assert.strictEqual(ru.__els.ytCheckBar.style.width, "2%");
});

check("finished state shows full bar", () => {
  ru.ytCheckRender({ status: "done", stage_key: "done", percent: 100, elapsed_s: 12.7 });
  assert.strictEqual(ru.__els.ytCheckBar.style.width, "100%");
  assert.ok(!ru.__els.ytCheckBar.className.includes("ind"));
});

/* ------------------------- stage labels / i18n ------------------------- */
check("RU stage labels", () => {
  assert.strictEqual(ru.ytCheckStageLabel("download"), "Скачивание аудио");
  assert.strictEqual(ru.ytCheckStageLabel("convert"), "Конвертация в mp3");
  assert.strictEqual(ru.ytCheckStageLabel("timeout"), "Таймаут проверки");
});

check("EN stage labels", () => {
  assert.strictEqual(en.ytCheckStageLabel("download"), "Downloading audio");
  assert.strictEqual(en.ytCheckStageLabel("prepare"), "Preparing");
});

check("translation file overrides built-in label", () => {
  ru.__setDict({ ytCheckStage_download: "ЗАГРУЗКА АУДИО" });
  assert.strictEqual(ru.ytCheckStageLabel("download"), "ЗАГРУЗКА АУДИО");
  ru.__setDict({});
  assert.strictEqual(ru.ytCheckStageLabel("download"), "Скачивание аудио");
});

check("unknown stage key falls back to backend text", () => {
  assert.strictEqual(ru.ytCheckStageLabel("brand_new_stage", "Что-то новое"), "Что-то новое");
});

/* ------------------------- button state ------------------------- */
check("button switches to busy and restores its label", () => {
  const btn = ru.__els.ytCheckBtn;
  btn.textContent = "▶️ Проверить загрузчик";
  ru.ytCheckSetButton(true);
  assert.strictEqual(btn.disabled, true);
  assert.ok(btn.textContent.includes("Проверка"), btn.textContent);
  ru.ytCheckSetButton(false);
  assert.strictEqual(btn.disabled, false);
  assert.strictEqual(btn.textContent, "▶️ Проверить загрузчик");
});

/* ------------------------- result summary ------------------------- */
check("youtube summary lists ffmpeg/yt-dlp/cookies", () => {
  const txt = ru.ytCheckSummary({ ffmpeg: "/usr/bin/ffmpeg", mode: "youtube",
                                  yt_dlp_version: "2026.08.19", cookie_source: "firefox" },
                                true, "Загрузчик работает");
  assert.ok(txt.startsWith("✅"), txt);
  assert.ok(txt.includes("✅ ffmpeg"), txt);
  assert.ok(txt.includes("yt-dlp 2026.08.19"), txt);
  assert.ok(txt.includes("cookies: firefox"), txt);
});

check("zvuk summary lists token/quality/API", () => {
  const txt = ru.ytCheckSummary({ ffmpeg: null, mode: "zvuk", zvuk_token: false,
                                  quality: "mid", api_reachable: true },
                                false, "Zvuk API доступен");
  assert.ok(txt.startsWith("⚠️"), txt);
  assert.ok(txt.includes("ffmpeg не найден"), txt);
  assert.ok(txt.includes("Zvuk токен: нет"), txt);
  assert.ok(txt.includes("качество: mid"), txt);
});

/* ------------------------- status is shown next to the button ------------------------- */
check("ytCheckShow writes to both the activity column and next to the button", () => {
  ru.ytCheckShow("✅ Загрузчик работает", true);
  assert.strictEqual(ru.__els.ytCheckStatus.innerHTML, "✅ Загрузчик работает");
  assert.strictEqual(ru.__els.ytCheckResult.innerHTML, "✅ Загрузчик работает");
  assert.strictEqual(ru.__els.ytCheckResult.style.color, "#10b981");
  ru.ytCheckShow("⚠️ ошибка", false);
  assert.strictEqual(ru.__els.ytCheckResult.style.color, "#ef4444");
});

/* =================== orchestration: the real checkDownloader() ===================
 * Drives start -> poll -> finish against a scripted fake server, using queued
 * timers so the polling loop runs deterministically.
 */

function jsonRes(obj, status) {
  status = status || 200;
  return { status: status, ok: status < 400, json: async () => obj };
}

function fakeServer(states) {
  let i = 0;
  const calls = [];
  const fn = async (url, init) => {
    const u = String(url);
    calls.push({ url: u, method: (init && init.method) || "GET" });
    if (u.includes("/check/start")) return jsonRes({ ok: true, run_id: "abc123", timeout: 45 });
    if (u.includes("/check/state/")) {
      const st = states[Math.min(i, states.length - 1)];
      i++;
      if (st === 404) return jsonRes({ ok: false, message: "not found" }, 404);
      return jsonRes({ ok: true, state: st });
    }
    return jsonRes({}, 404);
  };
  fn.calls = calls;
  return fn;
}

async function drainTimers(sandbox, maxSteps) {
  const widths = [];
  for (let i = 0; i < (maxSteps || 40); i++) {
    const fn = sandbox.__timers.shift();
    if (!fn) break;
    await fn();
    await new Promise((r) => setImmediate(r));
    widths.push(sandbox.__els.ytCheckBar.style.width);
  }
  return widths;
}

function checkAsync(name, fn) {
  return fn().then(
    () => { passed++; console.log("  ok   " + name); },
    (e) => { failures++; console.log("  FAIL " + name + "\n       " + e.message); }
  );
}

const FAIL_RESULT = {
  ok: false, mode: "youtube", ffmpeg: null, yt_dlp_version: "2026.08.19",
  cookie_source: "none",
  test: { ok: false, message: "ERROR: [youtube] jNQXAC9IVRw: Unable to download API page: TLS/SSL",
          detail: "network" },
};
const OK_RESULT = {
  ok: true, mode: "youtube", ffmpeg: "/usr/bin/ffmpeg", yt_dlp_version: "2026.08.19",
  cookie_source: "firefox",
  test: { ok: true, message: "Загрузчик работает (тест успешно скачал аудио)", detail: "probe.mp3" },
};

/* ------------------- source / downloader picker ------------------- */
/* The source is picked first; only its own credential fields may show. */
console.log("\n— applySourceVisibility() —");

function pickerContext(src, dl) {
  const sb = makeContext("RU-Russian");
  sb.__els.musicSource.value = src;
  sb.__els.downloader.value = dl;
  sb.applySourceVisibility();
  return sb.__els;
}

function disp(els, id) {
  return els["field_" + id].style.display;
}

check("spotify shows only its own key fields", () => {
  const els = pickerContext("spotify", "youtube");
  assert.strictEqual(disp(els, "clientId"), "");
  assert.strictEqual(disp(els, "clientSecret"), "");
  assert.strictEqual(disp(els, "yandexToken"), "none");
  assert.strictEqual(disp(els, "zvukToken"), "none");
  // youtube downloader contributes the cookie picker
  assert.strictEqual(disp(els, "ytCookieBrowser"), "");
});

check("deezer needs no keys at all", () => {
  const els = pickerContext("deezer", "youtube");
  assert.strictEqual(disp(els, "clientId"), "none");
  assert.strictEqual(disp(els, "clientSecret"), "none");
  assert.strictEqual(disp(els, "yandexToken"), "none");
  assert.strictEqual(disp(els, "zvukToken"), "none");
});

check("yandex shows its OAuth token field", () => {
  const els = pickerContext("yandex", "youtube");
  assert.strictEqual(disp(els, "yandexToken"), "");
  assert.strictEqual(disp(els, "clientId"), "none");
  assert.strictEqual(disp(els, "zvukToken"), "none");
});

check("zvuk source shows the zvuk token", () => {
  const els = pickerContext("zvuk", "youtube");
  assert.strictEqual(disp(els, "zvukToken"), "");
  assert.strictEqual(disp(els, "yandexToken"), "none");
});

check("musicbrainz needs no keys", () => {
  const els = pickerContext("musicbrainz", "youtube");
  assert.strictEqual(disp(els, "clientId"), "none");
  assert.strictEqual(disp(els, "zvukToken"), "none");
  assert.strictEqual(disp(els, "yandexToken"), "none");
});

check("zvuk downloader needs the token even when the source is deezer", () => {
  // Regression: the token used to belong to the source block only, so picking
  // deezer + zvuk downloader left the user with no place to enter it.
  const els = pickerContext("deezer", "zvuk");
  assert.strictEqual(disp(els, "zvukToken"), "");
  // and the youtube-only cookie picker must be hidden
  assert.strictEqual(disp(els, "ytCookieBrowser"), "none");
});

check("youtube downloader shows cookies, zvuk downloader does not", () => {
  assert.strictEqual(disp(pickerContext("deezer", "youtube"), "ytCookieBrowser"), "");
  assert.strictEqual(disp(pickerContext("deezer", "zvuk"), "ytCookieBrowser"), "none");
});

check("'open source' button points at the selected source's site", () => {
  const cases = {
    spotify: "https://developer.spotify.com/dashboard",
    deezer: "https://www.deezer.com",
    yandex: "https://music.yandex.ru",
    zvuk: "https://zvuk.com",
    musicbrainz: "https://musicbrainz.org",
  };
  for (const src in cases) {
    const els = pickerContext(src, "youtube");
    assert.strictEqual(els.sourceHomeBtn.href, cases[src],
                       src + " -> " + els.sourceHomeBtn.href);
    assert.strictEqual(els.sourceHomeBtn.style.display, "");
  }
});

check("every source gets a non-empty hint", () => {
  ["spotify", "deezer", "yandex", "zvuk", "musicbrainz"].forEach((src) => {
    const els = pickerContext(src, "youtube");
    assert.ok((els.sourceHint.textContent || "").length > 10,
              src + " hint is empty");
  });
});

check("downloader hint explains where the file comes from", () => {
  const y = pickerContext("deezer", "youtube");
  assert.ok(y.downloaderHint.textContent.includes("YouTube"), y.downloaderHint.textContent);
  const z = pickerContext("deezer", "zvuk");
  assert.ok(z.downloaderHint.textContent.includes("Zvuk"), z.downloaderHint.textContent);
});

check("zvuk source + youtube downloader does not promise YouTube outright", () => {
  // core/monitor.py::_downloader_for() forces "zvuk" for any zvuk- track id,
  // so the picker does not decide: Zvuk is tried first and YouTube is only the
  // fallback. The hint must not claim the file comes from YouTube.
  const els = pickerContext("zvuk", "youtube");
  const t = els.downloaderHint.textContent;
  assert.ok(t.includes("zvuk-"), t);
  assert.ok(t.includes("запасной") || t.includes("если Zvuk не отдаст"), t);
  assert.ok(!t.startsWith("Файл берётся с YouTube"), t);
});

check("other sources keep the plain youtube hint", () => {
  ["deezer", "spotify", "yandex", "musicbrainz"].forEach((src) => {
    const t = pickerContext(src, "youtube").downloaderHint.textContent;
    assert.ok(t.includes("Файл берётся с YouTube"), src + " -> " + t);
  });
});

/* populateForm() now ends with applySourceVisibility(). If that call throws,
 * the whole settings panel silently stops rendering — worth pinning. */
/* The credential fields ship hidden in the markup and are revealed by
 * applySourceVisibility(). Both halves matter: if the markup stops hiding them
 * every key field flashes before the config loads, and if init stops calling
 * the helper a failed /api/settings leaves them hidden with no way to type a
 * key. Pin the pair rather than either half alone. */
check("index.html ships every credential field hidden", () => {
  const html = fs.readFileSync(path.join(__dirname, "..", "templates", "index.html"), "utf8");
  ["field_clientId", "field_clientSecret", "field_yandexToken",
   "field_zvukToken", "field_ytCookieBrowser"].forEach((id) => {
    const m = html.match(new RegExp('<div class="field" id="' + id + '"[^>]*>'));
    assert.ok(m, id + " wrapper not found in index.html");
    assert.ok(/display:\s*none/.test(m[0]), id + " is not hidden by default: " + m[0]);
  });
});

check("init reveals fields before the async config load", () => {
  const initAt = src.indexOf('document.addEventListener("DOMContentLoaded"');
  assert.ok(initAt > 0, "DOMContentLoaded handler not found");
  const body = src.slice(initAt, initAt + 600);
  const revealAt = body.indexOf("applySourceVisibility()");
  const loadAt = body.indexOf("loadSettings()");
  assert.ok(revealAt > 0, "init does not call applySourceVisibility()");
  assert.ok(loadAt > 0, "init does not call loadSettings()");
  assert.ok(revealAt < loadAt,
            "applySourceVisibility() must run before loadSettings(), otherwise a " +
            "failed /api/settings leaves every field hidden");
});

check("populateForm() runs end to end and applies source visibility", () => {
  const sb = makeContext("RU-Russian");
  sb.__setConfig({
    app_version: "0.4.0",
    spotify_client_id: "cid-1",
    spotify_client_secret: "sec-1",
    yandex_token: "ya-1",
    zvuk_token: "zv-1",
    save_folder: "/tmp/dl",
    audio_quality: "256",
    monitor_interval_minutes: 30,
    duration_tolerance_sec: 10,
    max_albums_per_artist: 50,
    download_threads: 2,
    monitor_enabled: true,
    fallback_to_closest: true,
    youtube_cookie_browser: "firefox",
    proxy: "socks5://127.0.0.1:1080",
    music_source: "yandex",
    downloader: "youtube",
    artists: [],
    blacklist: ["remix"],
  });
  sb.populateForm();  // must not throw
  const els = sb.__els;

  // values landed in the right inputs
  assert.strictEqual(els.yandexToken.value, "ya-1");
  assert.strictEqual(els.zvukToken.value, "zv-1");
  assert.strictEqual(els.clientId.value, "cid-1");
  assert.strictEqual(els.musicSource.value, "yandex");
  assert.strictEqual(els.downloader.value, "youtube");
  assert.strictEqual(els.appVersion.textContent, "v0.4.0");

  // and the picker state follows the config: yandex source -> yandex field only
  assert.strictEqual(els.field_yandexToken.style.display, "");
  assert.strictEqual(els.field_clientId.style.display, "none");
  assert.strictEqual(els.field_zvukToken.style.display, "none");
  assert.strictEqual(els.sourceHomeBtn.href, "https://music.yandex.ru");
});

check("populateForm() with zvuk downloader reveals the zvuk token", () => {
  const sb = makeContext("RU-Russian");
  sb.__setConfig({
    music_source: "deezer", downloader: "zvuk", zvuk_token: "zv-2",
    artists: [], blacklist: [],
  });
  sb.populateForm();
  assert.strictEqual(sb.__els.field_zvukToken.style.display, "");
  assert.strictEqual(sb.__els.field_ytCookieBrowser.style.display, "none");
});

/* ---------------- YouTube request router tiles ---------------- */
console.log("\n— YouTube request router tiles —");

check("index.html ships the router tiles with the anti-rate-limit options", () => {
  const html = fs.readFileSync(path.join(__dirname, "..", "templates", "index.html"), "utf8");
  assert.ok(/<select id="dlParallel"/.test(html), "#dlParallel tile missing");
  assert.ok(/<select id="dlDelay"/.test(html), "#dlDelay tile missing");
  assert.ok(/<option value="1" id="dlp_1">1 — по очереди/.test(html),
            "dlParallel must offer the safe one-by-one mode");
  assert.ok(/<option value="3.5" id="dld_3_5">3.5 секунды \(рекомендуется\)/.test(html),
            "dlDelay must offer the recommended 3.5 s spacing");
});

/* ---------------- YouTube channel search zone (📺 pill) ---------------- */
console.log("\n— per-artist YouTube channel search zone —");

const MIRAJ_CH = "https://www.youtube.com/channel/UCp61iuJMfqfYTomVA50RlrQ";
/* saveSettings() repopulates the form from the server response — the stub must
   echo the POSTed body back, exactly like the real backend does. */
const settingsFetch = (url, opts) => Promise.resolve({
  json: () => Promise.resolve(JSON.parse((opts && opts.body) || "{}")),
});

check("the artist card renders a channel pill with the channel count", () => {
  const sb = makeContext("RU-Russian");
  sb.__setConfig({ music_source: "zvuk", artists: [
    { name: "Miraj", youtube_channels: [MIRAJ_CH] },
    { name: "NoChannel" },
  ] });
  sb.__setArtists([{ name: "Miraj", youtube_channels: [MIRAJ_CH] },
                   { name: "NoChannel" }]);
  sb.__setLibrary({ artists: [ { name: "Miraj", albums: [] },
                               { name: "NoChannel", albums: [] } ] });
  sb.renderLibraryTree("");
  const out = sb.__els.libraryContainer.innerHTML;
  assert.ok(out.includes("channel-pill"), "the channel pill must be rendered");
  assert.ok(out.includes("onChannelEdit('Miraj'"),
            "the pill must open the channel editor for the artist");
  const withChan = out.slice(out.indexOf("Miraj"), out.indexOf("NoChannel"));
  assert.ok(withChan.includes(">1<"), "the count of set channels is shown");
});

check("onChannelEdit() saves the pasted channels onto the artist entry", async () => {
  const sb = makeContext("RU-Russian", { fetch: settingsFetch });
  let asked = null;
  sb.prompt = (msg, def) => { asked = def; return MIRAJ_CH; };
  sb.__setConfig({ music_source: "zvuk", artists: [{ name: "Miraj" }] });
  sb.__setArtists([{ name: "Miraj" }]);
  sb.__setLibrary({ artists: [] });

  await sb.onChannelEdit("Miraj");

  assert.strictEqual(asked, "", "the prompt starts empty for an artist without channels");
  const arts = sb.__getArtists();
  assert.deepStrictEqual(arts[0].youtube_channels, [MIRAJ_CH],
                         "the pasted channel must land on the artist entry");
});

check("an empty prompt clears the channels, cancel keeps them", async () => {
  const sb = makeContext("RU-Russian", { fetch: settingsFetch });
  sb.prompt = () => "";
  sb.__setConfig({ artists: [{ name: "Miraj", youtube_channels: [MIRAJ_CH] }] });
  sb.__setArtists([{ name: "Miraj", youtube_channels: [MIRAJ_CH] }]);
  sb.__setLibrary({ artists: [] });
  await sb.onChannelEdit("Miraj");
  assert.deepStrictEqual(sb.__getArtists()[0].youtube_channels, [],
                         "empty input must reset to the global search");

  const sb2 = makeContext("RU-Russian", { fetch: settingsFetch });
  sb2.prompt = () => null;
  sb2.__setConfig({ artists: [{ name: "Miraj", youtube_channels: [MIRAJ_CH] }] });
  sb2.__setArtists([{ name: "Miraj", youtube_channels: [MIRAJ_CH] }]);
  sb2.__setLibrary({ artists: [] });
  await sb2.onChannelEdit("Miraj");
  assert.deepStrictEqual(sb2.__getArtists()[0].youtube_channels, [MIRAJ_CH],
                         "cancel must not touch the saved channels");
});

/* ---------------- the 🎧 platform link ---------------- */
console.log("\n— platform link follows the artist's source —");
check("the listen link is built per monitoring platform, zvuk and yandex included", () => {
  assert.ok(src.includes("PLATFORM_LINKS"),
            "renderLibrary must define a per-platform link table");
  assert.ok(src.includes("https://zvuk.com/artist/' + id"),
            "zvuk artists must link to zvuk.com/artist/{id}");
  assert.ok(src.includes("https://music.yandex.ru/artist/' + id"),
            "yandex artists must link to music.yandex.ru/artist/{id}");
  assert.ok(src.includes("https://open.spotify.com/artist/' + id"),
            "spotify artists must link to open.spotify.com/artist/{id}");
  assert.ok(src.includes("https://www.deezer.com/artist/' + id"),
            "deezer artists must link to deezer.com/artist/{id}");
  // The search order must start from the artist's own platform...
  assert.ok(/\[selSource\]\.concat\(knownSources\.filter/.test(src),
            "the artist's own platform must be tried first for the link");
  // ...and the fallback must key off the source-specific id fields.
  assert.ok(/idKey:\s*"zvuk_id"/.test(src) && /idKey:\s*"yandex_id"/.test(src),
            "links must be built from source-specific ids");
});

check("populateForm() applies the router settings with sane defaults", () => {
  const sb = makeContext("RU-Russian");
  sb.__setConfig({ artists: [], blacklist: [],
                   download_max_parallel: 2, download_delay_sec: 5 });
  sb.populateForm();
  assert.strictEqual(sb.__els.dlParallel.value, "2");
  assert.strictEqual(sb.__els.dlDelay.value, "5");

  const sb2 = makeContext("RU-Russian");
  sb2.__setConfig({ artists: [], blacklist: [] });
  sb2.populateForm();
  assert.strictEqual(sb2.__els.dlParallel.value, "1", "default is one-by-one");
  assert.strictEqual(sb2.__els.dlDelay.value, "3.5", "default spacing is 3.5 s");
});

check("saveSettings() sends the router keys and syncTileTexts() knows the tiles", () => {
  assert.ok(src.includes("'dlParallel', 'dlDelay'"),
            "syncTileTexts must refresh the router tile labels");
  assert.ok(/download_max_parallel:\s*parseInt/.test(src),
            "saveSettings must send download_max_parallel");
  assert.ok(/download_delay_sec:\s*isNaN\(dldVal\)/.test(src),
            "saveSettings must send download_delay_sec (0 must survive, not fall back)");
});

/* ---------------- target folder for newly added artists ---------------- */
console.log("\n— target folder for new artists —");

/* The picker itself: without it there is no way to choose where an artist goes. */
check("index.html offers a folder picker wired to setTargetFolder()", () => {
  const html = fs.readFileSync(path.join(__dirname, "..", "templates", "index.html"), "utf8");
  assert.ok(/<select id="targetFolder"[^>]*onchange="setTargetFolder\(this\.value\)"/.test(html),
            "#targetFolder must exist and drive setTargetFolder()");
  assert.ok(/id="targetFolderLabel"/.test(html), "the picker needs a translatable label");
});

check("getKnownFolders() unions config.folders with the artists' genre_path", () => {
  const sb = makeContext("RU-Russian");
  sb.__setConfig({ folders: ["Rock/Metal", "  Jazz  "], artists: [] });
  sb.__setArtists([{ name: "A", genre_path: "Jazz" }, { name: "B", genre_path: "" },
                   { name: "C", genre_path: "Rock/Metal" }, { name: "D", genre_path: "Blues" }]);
  // spread: the array comes from the vm realm, deepStrictEqual compares prototypes
  assert.deepStrictEqual([...sb.getKnownFolders()], ["Blues", "Jazz", "Rock/Metal"]);
});

check("with no folder picked the target stays empty", () => {
  const sb = makeContext("RU-Russian");
  sb.__setConfig({ folders: ["Rock"], artists: [] });
  assert.strictEqual(sb.getTargetFolder(), "");
});

check("setTargetFolder() selects the folder and fills the dropdown", () => {
  const sb = makeContext("RU-Russian");
  sb.__setConfig({ folders: ["Rock/Metal", "Jazz"], artists: [] });
  sb.setTargetFolder("Rock/Metal");
  assert.strictEqual(sb.getTargetFolder(), "Rock/Metal");
  assert.strictEqual(sb.__els.targetFolder.value, "Rock/Metal");
  const html = sb.__els.targetFolder.innerHTML;
  assert.ok(html.includes('value=""'), "needs a 'no folder' option: " + html);
  assert.ok(html.includes("Jazz"), html);
  assert.ok(/value="Rock\/Metal" selected/.test(html), "active folder must be selected: " + html);
});

check("normalises backslashes and stray slashes in the chosen path", () => {
  const sb = makeContext("RU-Russian");
  sb.__setConfig({ folders: ["Rock/Metal"], artists: [] });
  assert.strictEqual(sb.setTargetFolder("\\Rock\\Metal\\"), "Rock/Metal");
});

check("a folder deleted afterwards stops capturing new artists", () => {
  const sb = makeContext("RU-Russian");
  sb.__setConfig({ folders: ["Rock/Metal"], artists: [] });
  sb.setTargetFolder("Rock/Metal");
  sb.__setConfig({ folders: [], artists: [] });
  assert.strictEqual(sb.getTargetFolder(), "");
  sb.renderTargetFolder();
  assert.strictEqual(sb.__els.targetFolder.value, "");
});

/* The actual requirement: a new artist lands in the folder that is selected. */
const addFetch = () => Promise.resolve({
  json: () => Promise.resolve({ ok: true, job_id: "job-1", folders: ["Rock/Metal"] }),
});

/* Regression: the folder header's onclick used to force the node open after
 * toggleNodeExpand() had just flipped it, leaving the state out of sync with the
 * DOM - so a folder that was opened and closed would not open again. */
check("clicking a folder header twice collapses it and it reopens on the third", () => {
  const sb = makeContext("RU-Russian");
  sb.__setConfig({ folders: ["Rock"], artists: [] });
  const key = "genre_Rock";
  const click = () => { sb.toggleNodeExpand(key); sb.setTargetFolder("Rock"); };

  click();
  assert.strictEqual(sb.__getExpanded()[key], true, "first click opens");
  click();
  assert.strictEqual(sb.__getExpanded()[key], false,
                     "second click must collapse - the target-folder handler must not force it open");
  click();
  assert.strictEqual(sb.__getExpanded()[key], true, "third click opens again");
});

check("the folder header passes no expand flag to setTargetFolder()", () => {
  const m = src.match(/onclick="toggleNodeExpand\('\$\{catKey\}'\);([^"]*)"/);
  assert.ok(m, "folder header onclick not found");
  assert.ok(!/,\s*true/.test(m[1]),
            "a second argument re-introduces the stuck-folder bug: " + m[1]);
});

check("addArtist() files the new artist into the selected folder", () => {
  const sb = makeContext("RU-Russian", { fetch: addFetch });
  sb.__setConfig({ folders: ["Rock/Metal"], artists: [] });
  sb.__setArtists([]);
  sb.__setLibrary({ artists: [] });
  sb.setTargetFolder("Rock/Metal");
  sb.__els.artistInput.value = "Starset";

  sb.addArtist();  // both pushes happen before the first await

  const arts = sb.__getArtists();
  assert.strictEqual(arts.length, 1);
  assert.strictEqual(arts[0].name, "Starset");
  assert.strictEqual(arts[0].genre_path, "Rock/Metal",
                     "the artist must be filed into the selected folder");
  const lib = sb.__getLibrary().artists;
  assert.strictEqual(lib.length, 1);
  assert.strictEqual(lib[0].genre_path, "Rock/Metal");
});

check("selectArtist() from the suggestion list does the same", () => {
  const sb = makeContext("RU-Russian", { fetch: addFetch });
  sb.__setConfig({ folders: ["Rock/Metal"], artists: [] });
  sb.__setArtists([]);
  sb.__setLibrary({ artists: [] });
  sb.setTargetFolder("Rock/Metal");

  sb.selectArtist({ name: "Linkin Park", id: "123" });

  const arts = sb.__getArtists();
  assert.strictEqual(arts.length, 1);
  assert.strictEqual(arts[0].genre_path, "Rock/Metal");
  assert.strictEqual(sb.__getLibrary().artists[0].genre_path, "Rock/Metal");
});

check("with no folder selected the artist still goes to the root", () => {
  const sb = makeContext("RU-Russian", { fetch: addFetch });
  sb.__setConfig({ folders: ["Rock/Metal"], artists: [] });
  sb.__setArtists([]);
  sb.__setLibrary({ artists: [] });
  sb.__els.artistInput.value = "Пикник";

  sb.addArtist();

  assert.strictEqual(sb.__getArtists()[0].genre_path, "");
  assert.strictEqual(sb.__getLibrary().artists[0].genre_path, "");
});

(async () => {
  console.log("\n— checkDownloader() orchestration —");

  await checkAsync("start is POSTed, button goes busy, first paint is indeterminate", async () => {
    const sb = makeContext("RU-Russian", { fetch: fakeServer([
      { run_id: "abc123", status: "running", stage_key: "prepare", percent: 0, elapsed_s: 0.1, message: "" },
    ]) });
    await sb.checkDownloader();
    const els = sb.__els;
    assert.strictEqual(els.ytCheckBtn.disabled, true, "button must be disabled while checking");
    assert.ok(els.ytCheckBtn.textContent.includes("Проверка"), els.ytCheckBtn.textContent);
    assert.strictEqual(els.ytCheckProgress.style.display, "");
    assert.ok(els.ytCheckBar.className.includes("ind"), els.ytCheckBar.className);
  });

  await checkAsync("polls through stages and renders the outcome of a FAILED check", async () => {
    const sb = makeContext("RU-Russian", { fetch: fakeServer([
      { run_id: "abc123", status: "running", stage_key: "prepare",  percent: 5,  elapsed_s: 0.2, message: "yt-dlp 2026.08.19" },
      { run_id: "abc123", status: "running", stage_key: "extract",  percent: 12, elapsed_s: 1.1, message: "" },
      { run_id: "abc123", status: "running", stage_key: "download", percent: 47, elapsed_s: 2.4, message: "1.2 МБ / 3.0 МБ" },
      { run_id: "abc123", status: "running", stage_key: "convert",  percent: 90, elapsed_s: 3.0, message: "FFmpegExtractAudio" },
      { run_id: "abc123", status: "done",    stage_key: "failed",   percent: 100, elapsed_s: 3.4, message: "TLS/SSL", result: FAIL_RESULT },
    ]) });
    await sb.checkDownloader();
    const widths = await drainTimers(sb, 12);

    const els = sb.__els;
    assert.strictEqual(els.ytCheckBtn.disabled, false, "button must be re-enabled when finished");
    assert.strictEqual(els.ytCheckBar.style.width, "100%", widths.join(","));
    assert.ok(els.ytCheckResult.innerHTML.startsWith("⚠️"), els.ytCheckResult.innerHTML);
    assert.ok(els.ytCheckResult.innerHTML.includes("ffmpeg не найден"), els.ytCheckResult.innerHTML);
    assert.strictEqual(els.ytCheckResult.style.color, "#ef4444");
    // the blocking alert() must not be used for failures
    assert.strictEqual(els.ytCheckDetail.textContent, "network");

    // progress must have advanced monotonically through the stages
    const nums = widths.map((w) => parseFloat(w)).filter((n) => !isNaN(n));
    assert.ok(nums.length >= 4, "expected several polls, got " + widths.join(","));
    for (let i = 1; i < nums.length; i++) {
      assert.ok(nums[i] >= nums[i - 1], "bar went backwards: " + widths.join(","));
    }
  });

  await checkAsync("renders the outcome of a SUCCESSFUL check", async () => {
    const sb = makeContext("RU-Russian", { fetch: fakeServer([
      { run_id: "abc123", status: "running", stage_key: "download", percent: 60, elapsed_s: 1.0, message: "" },
      { run_id: "abc123", status: "done", stage_key: "done", percent: 100, elapsed_s: 4.2,
        message: "ok", result: OK_RESULT },
    ]) });
    await sb.checkDownloader();
    await drainTimers(sb, 8);
    const els = sb.__els;
    assert.strictEqual(els.ytCheckBtn.disabled, false);
    assert.ok(els.ytCheckResult.innerHTML.startsWith("✅"), els.ytCheckResult.innerHTML);
    assert.ok(els.ytCheckResult.innerHTML.includes("✅ ffmpeg"), els.ytCheckResult.innerHTML);
    assert.ok(els.ytCheckResult.innerHTML.includes("cookies: firefox"), els.ytCheckResult.innerHTML);
    assert.strictEqual(els.ytCheckResult.style.color, "#10b981");
  });

  await checkAsync("a transient state-poll failure does not abort the check", async () => {
    const sb = makeContext("RU-Russian", { fetch: fakeServer([
      404,
      { run_id: "abc123", status: "running", stage_key: "download", percent: 30, elapsed_s: 1.0, message: "" },
      { run_id: "abc123", status: "done", stage_key: "done", percent: 100, elapsed_s: 2.0,
        message: "ok", result: OK_RESULT },
    ]) });
    await sb.checkDownloader();
    await drainTimers(sb, 10);
    const els = sb.__els;
    assert.ok(els.ytCheckResult.innerHTML.startsWith("✅"),
              "must recover and finish, got: " + els.ytCheckResult.innerHTML);
    assert.strictEqual(els.ytCheckBtn.disabled, false);
  });

  await checkAsync("start failure restores the button and hides the bar", async () => {
    const sb = makeContext("RU-Russian", {
      fetch: async () => { throw new Error("server unreachable"); },
    });
    await sb.checkDownloader();
    const els = sb.__els;
    assert.strictEqual(els.ytCheckBtn.disabled, false, "button must not stay stuck");
    assert.strictEqual(els.ytCheckProgress.style.display, "none");
    assert.ok(els.ytCheckResult.innerHTML.startsWith("⚠️"), els.ytCheckResult.innerHTML);
    assert.ok(els.ytCheckResult.innerHTML.includes("server unreachable"), els.ytCheckResult.innerHTML);
  });

  await checkAsync("zvuk check outcome is summarized with token/quality", async () => {
    const zvuk = { ok: true, mode: "zvuk", ffmpeg: "/usr/bin/ffmpeg", zvuk_token: true,
                   quality: "high", api_reachable: true,
                   test: { ok: true, message: "Zvuk API доступен, токен активен", detail: "" } };
    const sb = makeContext("RU-Russian", { fetch: fakeServer([
      { run_id: "abc123", status: "running", stage_key: "api", percent: 30, elapsed_s: 0.5, message: "" },
      { run_id: "abc123", status: "done", stage_key: "done", percent: 100, elapsed_s: 1.2,
        message: "ok", result: zvuk },
    ]) });
    await sb.checkDownloader();
    await drainTimers(sb, 8);
    const txt = sb.__els.ytCheckResult.innerHTML;
    assert.ok(txt.startsWith("✅"), txt);
    assert.ok(txt.includes("Zvuk токен: есть"), txt);
    assert.ok(txt.includes("качество: high"), txt);
  });

  await checkAsync("zvuk expired token is shown as a token problem, not an outage", async () => {
    // Backend (v0.3.3): the API answered anonymously, so api_reachable stays
    // true and only token_valid is false. The summary must not flip the API to
    // "недоступно" — that was the original misleading diagnosis.
    const zvuk = { ok: false, mode: "zvuk", ffmpeg: "/usr/bin/ffmpeg", zvuk_token: true,
                   quality: "high", api_reachable: true, api_status: 200,
                   token_valid: false, token_status: 401,
                   test: { ok: false,
                           message: "Токен Zvuk истёк или неверен (HTTP 401). Скачивание продолжит работать только в качестве mid.",
                           detail: "api_reachable=True; token_status=401" } };
    const sb = makeContext("RU-Russian", { fetch: fakeServer([
      { run_id: "abc124", status: "running", stage_key: "token", percent: 60, elapsed_s: 0.6, message: "токен задан" },
      { run_id: "abc124", status: "failed", stage_key: "failed", percent: 100, elapsed_s: 1.4,
        message: "Токен Zvuk истёк или неверен (HTTP 401).", result: zvuk },
    ]) });
    await sb.checkDownloader();
    await drainTimers(sb, 8);
    const txt = sb.__els.ytCheckResult.innerHTML;

    assert.ok(txt.startsWith("⚠️"), txt);
    assert.ok(txt.includes("истёк"), txt);
    assert.ok(txt.includes("401"), txt);
    // the API is up — the summary must say so
    assert.ok(txt.includes("Zvuk API: доступно"), txt);
    assert.ok(!txt.includes("Zvuk API: недоступно"), txt);
    // and the token is set, so "нет" would be wrong
    assert.ok(txt.includes("Zvuk токен: есть"), txt);
    assert.ok(!txt.includes("Zvuk токен: нет"), txt);
    assert.strictEqual(sb.__els.ytCheckBtn.disabled, false, "button must not stay stuck");
  });

  console.log("\n%d passed, %d failed\n", passed, failures);
  process.exit(failures ? 1 : 0);
})();
