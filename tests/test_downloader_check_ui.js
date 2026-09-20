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
];

function makeContext(lang) {
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
  const sandbox = {
    document,
    console,
    setTimeout: () => 0,
    clearTimeout: () => {},
    setInterval: () => 0,
    clearInterval: () => {},
    fetch: () => Promise.reject(new Error("no network in UI test")),
    alert: () => {},
    localStorage: {
      getItem: () => lang,
      setItem: () => {},
    },
    location: { href: "http://localhost/" },
    __els: elements,
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  // Test-only hooks so we can drive the module-level `let` bindings.
  vm.runInContext(
    src + "\n;globalThis.__setLang = (l) => { currentLang = l; };" +
          "globalThis.__setDict = (d) => { loadedTranslations = d; };",
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

console.log("\n%d passed, %d failed\n", passed, failures);
process.exit(failures ? 1 : 0);
