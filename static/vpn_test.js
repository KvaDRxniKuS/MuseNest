/* MuseNest — yt-dlp / VPN tester UI (templates/vpn_test.html) */
(function () {
  "use strict";

  var pollTimer = null;
  var currentRun = null;
  var configProxy = "";

  function $(id) { return document.getElementById(id); }

  function esc(s) {
    return String(s === undefined || s === null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function setStatus(text, cls) {
    var el = $("vpnStatus");
    el.textContent = text || "";
    el.className = "msg" + (cls ? " " + cls : "");
  }

  function verdictPill(verdict, text) {
    var cls = "muted";
    if (verdict === "ok") cls = "ok";
    else if (verdict === "throttled" || verdict === "extract_only" || verdict === "degraded") cls = "warn";
    else if (verdict) cls = "bad";
    return '<span class="pill ' + cls + '">' + esc(text || verdict || "—") + "</span>";
  }

  function okPill(block, okText, noneText) {
    if (!block) return '<span class="pill muted">' + esc(noneText || "—") + "</span>";
    if (block.ok) return '<span class="pill ok">' + esc(okText || "ok") + "</span>";
    return '<span class="pill bad">' + esc(block.code || "нет") + "</span>";
  }

  /* ---------------- environment ---------------- */
  function loadEnv() {
    fetch("/api/vpn-test/env")
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d || !d.ok) { $("envLine").textContent = "окружение недоступно"; return; }
        var e = d.env || {};
        configProxy = d.config_proxy || "";
        $("envLine").innerHTML =
          "yt-dlp <b>" + esc(e.yt_dlp) + "</b> · ffmpeg " +
          (e.ffmpeg ? '<span class="pill ok">есть</span>' : '<span class="pill warn">нет</span>') +
          " · PySocks " + (e.pysocks ? "есть" : "нет") + " · python " + esc(e.python);
        var hints = (d.hints || []).slice();
        if (configProxy) hints.push("В настройках MuseNest задан прокси: <span class='vpn-kv'>" + esc(configProxy) + "</span>");
        if (hints.length) {
          var box = $("envHints");
          box.style.display = "";
          box.innerHTML = "<h2>Окружение</h2><ul style='margin:.2rem 0 0 1.1rem;'>" +
            hints.map(function (h) { return "<li>" + h + "</li>"; }).join("") + "</ul>";
        }
      })
      .catch(function () { $("envLine").textContent = "сервер недоступен"; });
  }

  /* ---------------- form helpers ---------------- */
  function appendLine(line) {
    var ta = $("profilesText");
    var cur = ta.value.replace(/\s+$/, "");
    ta.value = (cur ? cur + "\n" : "") + line;
    ta.focus();
  }

  window.vpnAddConfigProxy = function () {
    if (!configProxy) { alert("В настройках MuseNest прокси не задан."); return; }
    appendLine("Прокси из настроек = " + configProxy);
  };
  window.vpnAddDirect = function () { appendLine("direct"); };
  window.vpnFillExample = function () {
    $("profilesText").value =
      "Нидерланды SOCKS5 = socks5://127.0.0.1:1080\n" +
      "Германия HTTP, http://user:pass@1.2.3.4:8080\n" +
      "direct";
  };

  /* ---------------- run ---------------- */
  function collect() {
    var targets = ($("optTarget").value || "").split(/[\n,]+/).map(function (s) { return s.trim(); })
      .filter(Boolean);
    return {
      profiles_text: $("profilesText").value || "",
      add_direct: false,
      add_config_proxy: false,
      label: $("optLabel").value || "",
      force: $("optForce").checked,
      targets: targets,
      options: {
        ip: $("optIp").checked,
        reach: $("optReach").checked,
        extract: $("optExtract").checked,
        search: $("optSearch").checked,
        download: $("optDownload").checked,
        mp3: $("optMp3").checked,
        clients: ($("optClients").value || "").trim(),
        cookies: $("optCookies").value,
        timeout: parseFloat($("optTimeout").value) || 25,
        throttle_kbps: parseFloat($("optThrottle").value) || 60,
        search_query: ($("optSearchQuery").value || "").trim()
      }
    };
  }

  window.vpnStart = function () {
    var payload = collect();
    if (!payload.profiles_text.trim()) {
      setStatus("Укажите хотя бы один профиль (или нажмите «пример»).");
      return;
    }
    $("runBtn").disabled = true;
    $("stopBtn").disabled = false;
    setStatus("Запуск…");
    $("vpnBody").innerHTML = "";
    $("vpnDetails").innerHTML = "";
    $("vpnLog").innerHTML = "<div>запуск…</div>";

    fetch("/api/vpn-test/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    })
      .then(function (r) { return r.json().then(function (d) { return { status: r.status, body: d }; }); })
      .then(function (res) {
        if (!res.body || !res.body.ok) {
          $("runBtn").disabled = false;
          $("stopBtn").disabled = true;
          setStatus((res.body && res.body.message) || ("Ошибка " + res.status));
          return;
        }
        currentRun = res.body.run_id;
        setStatus("Проверяю: " + (res.body.profiles || []).join(", "));
        poll();
      })
      .catch(function (e) {
        $("runBtn").disabled = false;
        $("stopBtn").disabled = true;
        setStatus("Сеть: " + e);
      });
  };

  window.vpnStopPolling = function () {
    if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
    $("runBtn").disabled = false;
    $("stopBtn").disabled = true;
    setStatus("Слежение остановлено (тест на сервере продолжается).");
  };

  function poll() {
    if (!currentRun) return;
    fetch("/api/vpn-test/state/" + currentRun)
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d || !d.ok) { setStatus("Запуск не найден"); finish(); return; }
        render(d.state);
        if (d.state.status === "running") {
          pollTimer = setTimeout(poll, 1500);
        } else {
          finish();
          setStatus(d.state.status === "done"
            ? "Готово за " + (d.state.elapsed_s || 0) + " c"
            : "Ошибка: " + (d.state.error || "неизвестно"));
        }
      })
      .catch(function (e) { setStatus("Опрос: " + e); pollTimer = setTimeout(poll, 3000); });
  }

  function finish() {
    if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
    $("runBtn").disabled = false;
    $("stopBtn").disabled = true;
  }

  /* ---------------- rendering ---------------- */
  function speedText(dl) {
    if (!dl) return '<span class="pill muted">—</span>';
    if (!dl.ok) return '<span class="pill bad">' + esc(dl.code || "нет") + "</span>";
    var txt = Math.round(dl.kbps || 0) + " кбит/с";
    return '<span class="pill ' + (dl.throttled ? "warn" : "ok") + '">' + esc(txt) +
      (dl.throttled ? " ⚠резка" : "") + "</span>";
  }

  function render(st) {
    if (!st) return;
    var results = st.results || [];
    $("vpnSummary").innerHTML =
      "Профилей: <b>" + esc(st.total) + "</b> · проверено: <b>" + esc(st.done) + "</b>" +
      (st.current ? " · сейчас: <b>" + esc(st.current) + "</b>" : "") +
      " · " + esc(st.elapsed_s || 0) + " c" +
      (st.label ? " · метка: " + esc(st.label) : "");

    var table = $("vpnTable");
    if (results.length) table.style.display = "";
    $("vpnBody").innerHTML = results.map(function (r) {
      var eg = r.egress || {}, reach = r.reach || {}, srch = r.search || {};
      return "<tr>" +
        "<td title='" + esc(r.proxy_label) + "'><b>" + esc(r.name) + "</b><br><span class='vpn-kv'>" +
          esc(r.proxy_label) + "</span></td>" +
        "<td>" + esc(eg.ip || "—") + "</td>" +
        "<td>" + esc(eg.country || "—") + "</td>" +
        "<td title='" + esc(eg.org) + "'>" + esc(eg.org || "—") + "</td>" +
        "<td>" + okPill(r.reach ? reach : null, reach.status ? ("HTTP " + reach.status) : "ok", "—") +
          (reach.ms ? "<br><span class='vpn-kv'>" + esc(reach.ms) + " ms</span>" : "") + "</td>" +
        "<td>" + okPill(r.search ? srch : null, srch.count + " рез.", "—") + "</td>" +
        "<td>" + speedText(r.download) + "</td>" +
        "<td>" + verdictPill(r.verdict, r.verdict_text) + "</td>" +
        "</tr>";
    }).join("");

    $("vpnDetails").innerHTML = results.map(function (r) {
      var rows = [];
      var eg = r.egress;
      if (eg) {
        rows.push(eg.ok
          ? "IP: <b>" + esc(eg.ip) + "</b> — " + esc(eg.country) + (eg.city ? ", " + esc(eg.city) : "") +
            " · " + esc(eg.org || "—") + " · ASN " + esc(eg.asn || "—") + " · " + esc(eg.ms) + " ms"
          : "IP не определён: " + esc(eg.error));
      }
      if (r.reach) {
        rows.push("youtube.com: " + (r.reach.ok
          ? "доступен (" + esc(r.reach.ms) + " ms)"
          : "<b>недоступен</b> — " + esc(r.reach.error)));
      }
      var ex = r.extract;
      if (ex) {
        rows.push(ex.ok
          ? "extract: «" + esc(ex.title) + "» · " + esc(ex.format_count) + " форматов (аудио " +
            esc(ex.audio_formats) + ") · макс. " + esc(ex.max_height) + "р · " + esc(ex.ms) + " ms"
          : "extract: <b>ошибка</b> [" + esc(ex.code) + "] " + esc(ex.error));
      }
      if (r.clients && r.clients.length) {
        rows.push("player clients: " + r.clients.map(function (c) {
          return esc(c.client) + " " + (c.ok ? "✅" : "❌ " + esc(c.code || ""));
        }).join(", "));
      }
      if (r.search) {
        rows.push(r.search.ok
          ? "поиск: " + esc(r.search.count) + " результатов" + (r.search.first ? " («" + esc(r.search.first) + "»)" : "")
          : "поиск: <b>ошибка</b> [" + esc(r.search.code) + "] " + esc(r.search.error));
      }
      if (r.download) {
        rows.push(r.download.ok
          ? "скачивание: " + esc(r.download.bytes) + " байт за " + esc(r.download.seconds) +
            " c → <b>" + esc(r.download.kbps) + " кбит/с</b>" + (r.download.throttled ? " ⚠ YouTube режет скорость" : "")
          : "скачивание: <b>ошибка</b> [" + esc(r.download.code) + "] " + esc(r.download.error));
      }
      if (r.mp3) {
        rows.push(r.mp3.ok
          ? "путь MuseNest (mp3): ok (" + esc((r.mp3.files || []).join(", ")) + ")"
          : "путь MuseNest (mp3): <b>ошибка</b> [" + esc(r.mp3.code) + "] " + esc(r.mp3.error));
      }
      var hints = (r.hints || []).map(function (h) { return "<li>" + esc(h) + "</li>"; }).join("");
      return '<div class="vpn-detail">' +
        "<h3>" + esc(r.name) + " — " + verdictPill(r.verdict, r.verdict_text) +
        " <span class='vpn-kv'>" + esc(r.elapsed_s || 0) + " c</span></h3>" +
        '<div class="vpn-kv">' + rows.join("<br>") + "</div>" +
        (hints ? "<ul>" + hints + "</ul>" : "") +
        "</div>";
    }).join("");

    var log = (st.log || []).slice(-120).map(function (l) {
      return "<div>[" + esc(l.ts) + "] " + esc(l.profile) + " — " + esc(l.message) + "</div>";
    }).join("");
    $("vpnLog").innerHTML = log || "<div>—</div>";
    var box = $("vpnLog");
    box.scrollTop = box.scrollHeight;

    if (st.status !== "running") {
      $("vpnExport").style.display = "flex";
      $("expMd").href = "/api/vpn-test/report/" + st.run_id + "?format=md";
      $("expJson").href = "/api/vpn-test/report/" + st.run_id + "?format=json";
      $("vpnSavedTo").textContent = st.saved_to ? ("Сохранено: " + st.saved_to) : "";
    }
  }

  document.addEventListener("DOMContentLoaded", loadEnv);
})();
