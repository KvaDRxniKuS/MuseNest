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
