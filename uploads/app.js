let config = {};
let allArtists = [];
let allBlack = [];
let searchTimeout = null;
let searchReqId = 0;

let libraryData = { artists: [] };
let expandedNodes = {}; // persistent expanded states: "art_Name", "alb_Name_ID"

let currentLang = localStorage.getItem("tracker_lang") || "RU-Russian";
let loadedTranslations = {}; // holds current loaded language keys with fallback

/* ------------------------------- helpers ------------------------------ */

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function fmtBytes(b) {
  if (b === null || b === undefined || isNaN(Number(b))) return "";
  const u = ["B", "KB", "MB", "GB"];
  let i = 0, v = Number(b);
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return v.toFixed(v >= 100 || i === 0 ? 0 : 1) + " " + u[i];
}

function fmtSpeed(s) { return s ? fmtBytes(s) + "/s" : ""; }

function fmtEta(sec) {
  if (sec === null || sec === undefined || isNaN(sec)) return "";
  sec = Math.max(0, Math.round(sec));
  const m = Math.floor(sec / 60), s = sec % 60;
  return m + ":" + String(s).padStart(2, "0");
}

function showLibStatus(msg) {
  const el = document.getElementById("libraryStatusMsg");
  if (el) {
    el.textContent = msg;
    el.style.display = "block";
  }
}

function hideLibStatus() {
  const el = document.getElementById("libraryStatusMsg");
  if (el) {
    el.style.display = "none";
  }
}

/* ------------------------- Localization Extensible System ------------------------ */

async function loadAvailableLanguages() {
  try {
    const res = await fetch("/api/translations");
    const list = await res.json();
    const select = document.getElementById("langSelect");
    if (!select) return;
    
    select.innerHTML = "";
    list.forEach(lang => {
      // e.g. "RU-Russian" -> split to "RU" and "Russian"
      const parts = lang.split("-");
      const label = parts[1] || parts[0];
      const opt = document.createElement("option");
      opt.value = lang;
      opt.textContent = label;
      select.appendChild(opt);
    });
    
    // Default fallback if stored language is missing
    if (!list.includes(currentLang)) {
      currentLang = list[0] || "RU-Russian";
    }
    select.value = currentLang;
  } catch (err) {
    console.error("Failed to load available languages:", err);
  }
}

async function loadLanguage(langName) {
  try {
    // 1. Always fetch English as the baseline fallback first
    const baselineRes = await fetch("/api/translations/EN-English");
    const baseline = await baselineRes.json();
    
    if (langName !== "EN-English") {
      // 2. Fetch target language and merge over baseline (overwriting keys)
      const res = await fetch(`/api/translations/${langName}`);
      const target = await res.json();
      loadedTranslations = Object.assign({}, baseline, target);
    } else {
      loadedTranslations = baseline;
    }
    
    applyLanguage();
  } catch (err) {
    console.error(`Failed to load translation ${langName}:`, err);
  }
}

async function changeLanguage() {
  const select = document.getElementById("langSelect");
  if (!select) return;
  currentLang = select.value;
  localStorage.setItem("tracker_lang", currentLang);
  await loadLanguage(currentLang);
}

function applyLanguage() {
  const dict = loadedTranslations || {};
  
  // 1. Translate elements by ID (excluding input placeholder helper keys)
  for (const id in dict) {
    const el = document.getElementById(id);
    if (el) {
      if (el.tagName === "INPUT") {
        el.placeholder = dict[id];
      } else {
        el.textContent = dict[id];
      }
    }
  }

  // 2. Explicitly handle input placeholders and select options
  const searchEl = document.getElementById("artistSearch");
  if (searchEl) searchEl.placeholder = dict.search_placeholder || "Search...";
  
  const artistInputEl = document.getElementById("artistInput");
  if (artistInputEl) artistInputEl.placeholder = dict.artist_input_placeholder || "Artist...";
  
  const blackInputEl = document.getElementById("blackInput");
  if (blackInputEl) blackInputEl.placeholder = dict.black_input_placeholder || "Exclude...";

  const proxyUrlEl = document.getElementById("proxyUrl");
  if (proxyUrlEl) proxyUrlEl.placeholder = dict.proxyUrlPlaceholder || "socks5://127.0.0.1:1080";

  const saveFolderEl = document.getElementById("saveFolder");
  if (saveFolderEl) saveFolderEl.placeholder = dict.saveFolderPlaceholder || "downloads";

  // 3. Handle specific hints and warnings
  const artsHint = document.querySelector(".add-wrap + .hint");
  if (artsHint) artsHint.textContent = dict.artists_hint || "";

  const blHint = document.querySelector("#blackList + .hint");
  if (blHint) blHint.textContent = dict.blacklist_hint || "";

  // 4. Re-render dynamic elements so they use new terms
  renderLibraryTree(document.getElementById("artistSearch").value);
  renderBlack();
}

/* ------------------------- Settings & Controls ------------------------ */

async function loadSettings() {
  try {
    const res = await fetch("/api/settings");
    config = await res.json();
    populateForm();
  } catch (err) {
    console.error("Failed to load settings:", err);
  }
}

function populateForm() {
  document.getElementById("clientId").value = config.spotify_client_id || "";
  document.getElementById("clientSecret").value = config.spotify_client_secret || "";
  document.getElementById("musicSource").value = config.music_source || "deezer";
  document.getElementById("saveFolder").value = config.save_folder || "";
  document.getElementById("audioQuality").value = config.audio_quality || "320";
  document.getElementById("interval").value = config.monitor_interval_minutes || 60;
  document.getElementById("tolerance").value = config.duration_tolerance_sec || 15;
  document.getElementById("maxAlbums").value = config.max_albums_per_artist || 99999;
  document.getElementById("threads").value = config.download_threads || 4;
  document.getElementById("monitorEnabled").checked = !!config.monitor_enabled;
  document.getElementById("fallback").checked = !!config.fallback_to_closest;
  const ytcEl = document.getElementById("ytCookieBrowser");
  if (ytcEl) ytcEl.value = config.youtube_cookie_browser || "";
  const prxEl = document.getElementById("proxyUrl");
  if (prxEl) prxEl.value = config.proxy || "";

  allArtists = config.artists || [];
  allBlack = config.blacklist || [];
  renderBlack();
  
  // Update badge
  const artistCountEl = document.getElementById("artistCount");
  if (artistCountEl) {
    artistCountEl.textContent = `(${allArtists.length})`;
  }
}

async function saveSettings() {
  const data = {
    spotify_client_id: document.getElementById("clientId").value.trim(),
    spotify_client_secret: document.getElementById("clientSecret").value.trim(),
    music_source: document.getElementById("musicSource").value,
    save_folder: document.getElementById("saveFolder").value.trim(),
    audio_quality: document.getElementById("audioQuality").value,
    monitor_interval_minutes: parseInt(document.getElementById("interval").value) || 60,
    duration_tolerance_sec: parseInt(document.getElementById("tolerance").value) || 15,
    max_albums_per_artist: parseInt(document.getElementById("maxAlbums").value) || 99999,
    download_threads: parseInt(document.getElementById("threads").value) || 4,
    monitor_enabled: document.getElementById("monitorEnabled").checked,
    fallback_to_closest: document.getElementById("fallback").checked,
    youtube_cookie_browser: (document.getElementById("ytCookieBrowser") || {}).value || "",
    proxy: (document.getElementById("proxyUrl") || {}).value || "",
    artists: allArtists,
    blacklist: allBlack,
  };

  try {
    const res = await fetch("/api/settings", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(data),
    });
    config = await res.json();
    populateForm();
    const msg = document.getElementById("saveMsg");
    if (msg) {
      msg.textContent = loadedTranslations.save_ok || "Saved!";
      setTimeout(() => msg.textContent = "", 2500);
    }
  } catch (err) {
    alert((currentLang.startsWith("RU") ? "Ошибка сохранения: " : "Save error: ") + err);
  }
}

async function testSpotify() {
  await saveSettings();
  const btn = document.getElementById("testBtn");
  if (btn) btn.textContent = currentLang.startsWith("RU") ? "Проверка..." : "Testing...";
  try {
    const res = await fetch("/api/test_spotify", {method: "POST"});
    const data = await res.json();
    if (data.ok) {
      alert(loadedTranslations.api_ok || "Spotify connected!");
    } else {
      alert((loadedTranslations.api_err || "Spotify connection failed:") + " " + (data.message || "Invalid keys"));
    }
  } catch (err) {
    alert("Request error: " + err);
  } finally {
    if (btn) btn.textContent = loadedTranslations.testBtn || "Test Spotify API";
  }
}

async function pickFolder() {
  try {
    const res = await fetch("/api/pick_folder", {method: "POST"});
    const data = await res.json();
    if (data.ok && data.path) {
      document.getElementById("saveFolder").value = data.path;
    } else if (!data.canceled && data.message) {
      alert((currentLang.startsWith("RU") ? "Не удалось выбрать папку: " : "Failed to select folder: ") + data.message);
    }
  } catch (err) {
    alert(currentLang.startsWith("RU") ? "Ошибка связи с сервером" : "Server communication error");
  }
}

async function triggerScan() {
  await saveSettings();
  try {
    const res = await fetch("/api/scan", {method: "POST"});
    const data = await res.json();
    if (!data.ok && data.message) {
      alert(data.message);
    }
  } catch (err) {
    alert(currentLang.startsWith("RU") ? "Ошибка запуска сканирования" : "Failed to trigger scan");
  }
}

async function triggerStop() {
  try {
    await fetch("/api/stop", {method: "POST"});
  } catch (err) {
    console.error("Stop error:", err);
  }
}

/* ------------------------- Collapsible Library Tree ------------------------ */

async function loadLibrary() {
  try {
    const res = await fetch(`/api/library?_t=${Date.now()}`);
    libraryData = await res.json();
    renderLibraryTree(document.getElementById("artistSearch").value);
  } catch (err) {
    console.error("Failed to load library:", err);
  }
}

async function checkLibraryFiles() {
  await saveSettings(); // Save any newly typed save folder path first!
  const btn = document.getElementById("libCheckBtn");
  const oldText = btn.textContent;
  btn.textContent = currentLang.startsWith("RU") ? "⏳ Сверка..." : "⏳ Checking...";
  btn.disabled = true;
  showLibStatus(loadedTranslations.status_checking_files || "Checking files...");
  try {
    const res = await fetch("/api/library/check", { method: "POST" });
    const data = await res.json();
    if (data.ok) {
      libraryData = data.library;
      renderLibraryTree(document.getElementById("artistSearch").value);
    } else {
      alert("Error: " + data.message);
    }
  } catch (err) {
    alert("Error: " + err);
  } finally {
    btn.textContent = oldText;
    btn.disabled = false;
    hideLibStatus();
  }
}

async function resetFilterErrors() {
  const btn = document.getElementById("libResetFilterBtn");
  const oldText = btn.textContent;
  btn.textContent = currentLang.startsWith("RU") ? "⏳ Сброс..." : "⏳ Resetting...";
  btn.disabled = true;
  try {
    const res = await fetch("/api/library/reset_filter_errors", { method: "POST" });
    const data = await res.json();
    if (data.ok) {
      libraryData = data.library;
      renderLibraryTree(document.getElementById("artistSearch").value);
    } else {
      alert("Error: " + data.message);
    }
  } catch (err) {
    alert("Error: " + err);
  } finally {
    btn.textContent = oldText;
    btn.disabled = false;
  }
}

async function updateLibraryMetadata() {
  await saveSettings();
  const btn = document.getElementById("libUpdateBtn");
  const oldText = btn.textContent;
  btn.textContent = currentLang.startsWith("RU") ? "⏳ Обновление..." : "⏳ Updating...";
  btn.disabled = true;
  showLibStatus(loadedTranslations.status_updating_net || "Updating metadata...");
  try {
    const res = await fetch("/api/library/update", { method: "POST" });
    const data = await res.json();
    if (data.ok) {
      libraryData = data.library;
      renderLibraryTree(document.getElementById("artistSearch").value);
    } else {
      alert("Error: " + data.message);
    }
  } catch (err) {
    alert("Error: " + err);
  } finally {
    btn.textContent = oldText;
    btn.disabled = false;
    hideLibStatus();
  }
}

async function toggleIgnore(artistId, albumId, trackId, event) {
  event.stopPropagation();
  try {
    const res = await fetch("/api/library/toggle_ignore", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ artist_id: artistId, album_id: albumId, track_id: trackId })
    });
    const data = await res.json();
    if (data.ok) {
      libraryData = data.library;
      renderLibraryTree(document.getElementById("artistSearch").value);
    }
  } catch (err) {
    console.error("Toggle ignore error:", err);
  }
}

function toggleNodeExpand(nodeKey) {
  expandedNodes[nodeKey] = !expandedNodes[nodeKey];
  const children = document.getElementById("children_" + nodeKey);
  const header = children?.previousElementSibling;
  const arrow = header?.querySelector(".tree-node-arrow");
  if (children && arrow) {
    if (expandedNodes[nodeKey]) {
      children.classList.add("expanded");
      arrow.classList.add("expanded");
    } else {
      children.classList.remove("expanded");
      arrow.classList.remove("expanded");
    }
  }
}

function renderLibraryTree(filter = "") {
  const container = document.getElementById("libraryContainer");
  if (!container) return;
  
  const f = filter.toLowerCase().trim();
  const artists = libraryData.artists || [];
  
  if (artists.length === 0) {
    container.innerHTML = `
      <div style="color:var(--text-muted);font-style:italic;text-align:center;padding:1.5rem 0.5rem;font-size:0.85rem;">
        ${loadedTranslations.empty_list_hint || "List is empty."}
      </div>`;
    return;
  }
  
  let html = "";
  
  artists.forEach(art => {
    const artName = art.name || "";
    const artId = art.id || artName;
    const isArtIgnored = !!art.ignored;
    
    // Check if artist is fully downloaded
    const realAlbums = art.albums || [];
    const isArtCompleted = realAlbums.length > 0 && realAlbums.every(alb => {
      const realTracks = alb.tracks || [];
      return realTracks.length > 0 && realTracks.every(t => t.downloaded);
    });
    
    let artMatches = artName.toLowerCase().includes(f);
    let matchedAlbums = [];
    
    realAlbums.forEach(alb => {
      const albName = alb.name || "";
      const albId = alb.id;
      const isAlbIgnored = !!alb.ignored;
      
      let albMatches = albName.toLowerCase().includes(f);
      let matchedTracks = [];
      
      (alb.tracks || []).forEach(trk => {
        const trkName = trk.name || "";
        const trkMatches = trkName.toLowerCase().includes(f);
        
        if (!f || artMatches || albMatches || trkMatches) {
          matchedTracks.push(trk);
        }
      });
      
      if (!f || artMatches || albMatches || matchedTracks.length > 0) {
        matchedAlbums.push({
          ...alb,
          tracks: matchedTracks,
          forceExpand: f && (albMatches || matchedTracks.length > 0)
        });
      }
    });
    
    if (!f || artMatches || matchedAlbums.length > 0) {
      const forceExpandArt = f && (artMatches || matchedAlbums.length > 0);
      const artKey = "art_" + artName;
      const isArtExpanded = forceExpandArt || !!expandedNodes[artKey];
      
      const artHeaderClass = isArtCompleted ? "completed-green" : "not-completed-gray";
      
      html += `
        <div class="tree-node" style="margin-left:0;">
          <div class="tree-node-header ${artHeaderClass}" onclick="toggleNodeExpand('${artKey}')">
            <span class="tree-node-title" title="${esc(artName)}">
              <span class="tree-node-arrow ${isArtExpanded ? 'expanded' : ''}">▶</span>
              <span style="font-weight:600;" class="${isArtIgnored ? 'ignored-text' : ''}">${esc(artName)}</span>
            </span>
            <div class="tree-actions">
              <a href="${esc((art.spotify_id ? 'https://open.spotify.com/artist/' + art.spotify_id : (art.id ? 'https://open.spotify.com/artist/' + art.id : '#')))}" target="_blank" style="margin-right:6px; font-size:0.8rem; text-decoration:none;" title="Open Spotify">🎧</a>
              <span style="font-size:0.75rem; color:var(--text-muted); margin-right:6px;">${esc(art.followers != null ? art.followers.toLocaleString() + ' 👥' : '')}</span>
              <label class="ignore-check" onclick="event.stopPropagation()">
                <input type="checkbox" ${isArtIgnored ? 'checked' : ''} onchange="toggleIgnore('${esc(artId)}', null, null, event)">
                ${loadedTranslations.ignore_label || "Ignore"}
              </label>
              <button class="del-btn" onclick="event.stopPropagation(); removeArtistByName('${esc(artName)}')">✕</button>
            </div>
          </div>
          <div class="tree-node-children ${isArtExpanded ? 'expanded' : ''}" id="children_${artKey}">
      `;
      
      matchedAlbums.forEach(alb => {
        const albName = alb.name || "";
        const albId = alb.id;
        const isAlbIgnored = !!alb.ignored;
        const albKey = "alb_" + artName + "_" + albId;
        const isAlbExpanded = alb.forceExpand || !!expandedNodes[albKey];
        
        const realTracks = alb.tracks || [];
        const isAlbCompleted = realTracks.length > 0 && realTracks.every(t => t.downloaded);
        const albHeaderClass = isAlbCompleted ? "completed-green" : "not-completed-gray";
        
        html += `
          <div class="tree-node">
            <div class="tree-node-header ${albHeaderClass}" onclick="toggleNodeExpand('${albKey}')">
              <span class="tree-node-title" title="${esc(albName)}">
                <span class="tree-node-arrow ${isAlbExpanded ? 'expanded' : ''}">▶</span>
                <span style="font-weight:500;" class="${isArtIgnored || isAlbIgnored ? 'ignored-text' : ''}">${esc(albName)}</span>
              </span>
              <div class="tree-actions">
                <label class="ignore-check" onclick="event.stopPropagation()">
                  <input type="checkbox" ${isAlbIgnored ? 'checked' : ''} onchange="toggleIgnore('${esc(artId)}', '${esc(albId)}', null, event)">
                  ${loadedTranslations.ignore_label || "Ignore"}
                </label>
              </div>
            </div>
            <div class="tree-node-children ${isAlbExpanded ? 'expanded' : ''}" id="children_${albKey}">
        `;
        
        alb.tracks.forEach(trk => {
          const trkName = trk.name || "";
          const trkId = trk.id;
          const isTrkIgnored = !!trk.ignored;
          const isDownloaded = !!trk.downloaded;
          const isNoMatch = !!trk.no_match;
          
          let trkHeaderClass = "not-completed-gray";
          if (isDownloaded) {
            trkHeaderClass = "completed-green";
          } else if (isNoMatch) {
            trkHeaderClass = "failed-red";
          }
          
          const errCode = trk.error_code || "ERR-1";
          const labelKey = `err_${errCode.split("-")[1]}_label`;
          const titleKey = `err_${errCode.split("-")[1]}_title`;
          const badgeLabel = loadedTranslations[labelKey] || errCode;
          const badgeTitle = loadedTranslations[titleKey] || "Error occurred";
          
          const errBadge = (!isDownloaded && isNoMatch) ? `<span class="failed-badge" style="margin-left: 6px; flex: none;" title="${esc(badgeTitle)}">${esc(badgeLabel)}</span>` : "";
          
          html += `
            <div class="tree-node tree-track">
              <div class="tree-node-header ${trkHeaderClass}" style="cursor:default;" onclick="event.stopPropagation()">
                <span class="tree-node-title" title="${esc(trkName)}">
                  <span class="${isArtIgnored || isAlbIgnored || isTrkIgnored ? 'ignored-text' : ''}">${esc(trkName)}</span>
                  ${errBadge}
                </span>
                <div class="tree-actions">
                  <label class="ignore-check">
                    <input type="checkbox" ${isTrkIgnored ? 'checked' : ''} onchange="toggleIgnore('${esc(artId)}', '${esc(albId)}', '${esc(trkId)}', event)">
                    ${loadedTranslations.ignore_label || "Ignore"}
                  </label>
                </div>
              </div>
            </div>
          `;
        });
        
        if (alb.tracks.length === 0) {
          html += `
            <div style="color:var(--text-muted);font-style:italic;font-size:0.75rem;padding:0.3rem 1rem;">
              ${loadedTranslations.no_tracks_hint || "No tracks."}
            </div>`;
        }
        
        html += `
            </div>
          </div>
        `;
      });
      
      if (matchedAlbums.length === 0) {
        html += `
          <div style="color:var(--text-muted);font-style:italic;font-size:0.75rem;padding:0.3rem 1rem;">
            ${loadedTranslations.no_albums_hint || "No albums."}
          </div>`;
      }
      
      html += `
          </div>
        </div>
      `;
    }
  });
  
  container.innerHTML = html;
}

async function removeArtistByName(artName) {
  if (!confirm((loadedTranslations.confirm_remove_artist || "Are you sure you want to remove ") + `${artName}?`)) return;
  const idx = allArtists.findIndex(a => {
    const name = (typeof a === "object" ? a.name : a) || "";
    return name.toLowerCase() === artName.toLowerCase();
  });
  if (idx !== -1) {
    allArtists.splice(idx, 1);
    await saveSettings();
    showLibStatus(loadedTranslations.status_removing_artist || "Removing...");
    await loadLibrary();
    hideLibStatus();
  }
}

/* ------------------------- Artist Inputs & Suggestions ------------------------ */

function onArtistInput() {
  clearTimeout(searchTimeout);
  const q = document.getElementById("artistInput").value.trim();
  const suggest = document.getElementById("artistSuggest");
  if (q.length < 2) {
    suggest.style.display = "none";
    return;
  }
  searchTimeout = setTimeout(async () => {
    const reqId = ++searchReqId;
    suggest.innerHTML = `<div class="suggest-state">${currentLang.startsWith("RU") ? "🔍 Поиск…" : "🔍 Searching..."}</div>`;
    suggest.style.display = "block";
    try {
      const res = await fetch("/api/artist_search", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({query: q}),
      });
      const items = await res.json().catch(() => null);
      if (reqId !== searchReqId) return; // пришёл ответ на устаревший запрос
      if (document.getElementById("artistInput").value.trim() !== q) return;
      if (!res.ok) {
        suggest.innerHTML = `<div class="suggest-state error">⚠️ ${esc((items && items.error) || ("Server error (" + res.status + ")"))}</div>`;
        return;
      }
      if (!Array.isArray(items) || items.length === 0) {
        suggest.innerHTML = `<div class="suggest-state">${currentLang.startsWith("RU") ? "Ничего не найдено — Enter добавит как есть" : "No results found — Enter will add as is"}</div>`;
        return;
      }
      suggest.innerHTML = "";
      items.forEach(item => {
        const div = document.createElement("div");
        div.className = "suggest-item";
        div.innerHTML = `<span>${esc(item.name)}</span> <small style="color:var(--text-muted)">${esc(item.followers ? item.followers.toLocaleString() + " followers · " : "")}${esc(item.spotify_name ? "Spotify: " + item.spotify_name : "ID: " + (item.id || "—"))}</small> ${item.link ? `<a href="${esc(item.link)}" target="_blank" style="font-size:0.75rem; margin-left:6px;">🎧</a>` : ""}`;
        div.onmousedown = () => {
          selectArtist(item);
        };
        suggest.appendChild(div);
      });
      suggest.style.display = "block";
    } catch (err) {
      console.error(err);
      if (reqId === searchReqId) {
        suggest.innerHTML = `<div class="suggest-state error">⚠️ Network error</div>`;
      }
    }
  }, 300);
}

async function selectArtist(item) {
  document.getElementById("artistSuggest").style.display = "none";
  document.getElementById("artistInput").value = "";
  if (!allArtists.some(a => (a.id && a.id === item.id) || a.name.toLowerCase() === item.name.toLowerCase())) {
    allArtists.push({
      id: item.id || null,
      name: item.name,
      source: config.music_source || "deezer",
      spotify_name: item.spotify_name || null,
    });
    showLibStatus((currentLang.startsWith("RU") ? `⏳ Добавление ${item.name}: получение альбомов и треков из сети...` : `⏳ Adding ${item.name}: fetching albums and tracks from net...`));
    await saveSettings();
    await updateLibraryMetadata();
    hideLibStatus();
  }
}

async function addArtist() {
  const input = document.getElementById("artistInput");
  const val = input.value.trim();
  if (!val) return;
  document.getElementById("artistSuggest").style.display = "none";
  input.value = "";
  if (!allArtists.some(a => (typeof a === "object" ? a.name : a).toLowerCase() === val.toLowerCase())) {
    allArtists.push({
      id: null,
      name: val,
      source: config.music_source || "deezer",
      spotify_name: null,
    });
    showLibStatus((currentLang.startsWith("RU") ? `⏳ Добавление ${val}: загрузка структуры альбомов и треков...` : `⏳ Adding ${val}: fetching album and track structure...`));
    await saveSettings();
    await updateLibraryMetadata();
    hideLibStatus();
  }
}

/* ------------------------- Blacklist ------------------------ */

function renderBlack() {
  const list = document.getElementById("blackList");
  list.innerHTML = "";
  allBlack.forEach((b, idx) => {
    const li = document.createElement("li");
    li.innerHTML = `
      <span class="name" title="${esc(b)}">${esc(b)}</span>
      <button class="del-btn" onclick="removeBlack(${idx})">✕</button>
    `;
    list.appendChild(li);
  });
}

function addBlack() {
  const input = document.getElementById("blackInput");
  const val = input.value.trim().toLowerCase();
  if (!val) return;
  input.value = "";
  if (!allBlack.includes(val)) {
    allBlack.push(val);
    saveSettings();
  }
}

function removeBlack(idx) {
  allBlack.splice(idx, 1);
  saveSettings();
}

/* ------------------------- thread activity box ------------------------ */

const THREAD_STATES = {
  idle:        { icon: "💤", cls: "idle" },
  searching:   { icon: "🔍", cls: "searching" },
  downloading: { icon: "⬇️", cls: "downloading" },
  processing:  { icon: "⚙️", cls: "processing" },
  error:       { icon: "❌", cls: "error" },
};

function renderThreadTasks(st) {
  const tasksBox = document.getElementById("threadTasksBox");
  const threads = st.threads_info || st.thread_tasks || {};
  const keys = Object.keys(threads).sort((a, b) => Number(a) - Number(b));
  if (keys.length === 0) {
    tasksBox.innerHTML = `<div style="color:var(--text-muted);font-style:italic;">${loadedTranslations.threads_idle || "Threads idling..."}</div>`;
    return;
  }
  tasksBox.innerHTML = keys.map(k => {
    const info = threads[k];
    const isObj = info && typeof info === "object";
    const state = isObj ? (info.state || "idle") : "idle";
    const task  = isObj ? (info.task || "") : String(info);
    const p     = isObj ? info.progress : null;
    const meta  = THREAD_STATES[state] || THREAD_STATES.idle;

    let progressHtml = "";
    if (state === "downloading" && p) {
      const pct = (p.percent !== null && p.percent !== undefined) ? p.percent : null;
      const size = p.total_bytes
        ? fmtBytes(p.downloaded_bytes) + " / " + fmtBytes(p.total_bytes)
        : (p.downloaded_bytes ? fmtBytes(p.downloaded_bytes) : "");
      const parts = [pct !== null ? pct + "%" : "…"];
      if (size) parts.push(size);
      const spd = fmtSpeed(p.speed);
      if (spd) parts.push(spd);
      const eta = fmtEta(p.eta);
      if (eta) parts.push("ETA " + eta);
      progressHtml = `
        <div class="tbar"><div class="tbar-fill${pct === null ? " ind" : ""}" style="width:${pct !== null ? pct : 100}%"></div></div>
        <div class="tmeta">${parts.map(esc).join(" · ")}</div>`;
    }

    const threadLabel = currentLang.startsWith("RU") ? "Поток" : "Thread";

    return `<div class="thread-row">
      <div class="thread-head"><span class="tstate-dot ${meta.cls}"></span><b>[${threadLabel} ${esc(k)}]</b>&nbsp;${esc(task)}</div>
      ${progressHtml}
    </div>`;
  }).join("");
}

let wasRunning = false;

async function pollStatus() {
  try {
    const res = await fetch(`/api/status?_t=${Date.now()}`);
    const data = await res.json();
    const st = data.status;

    const dot = document.getElementById("runDot");
    const txt = document.getElementById("runText");
    if (st.running) {
      dot.className = "dot running";
      txt.textContent = st.current_artist ? (currentLang.startsWith("RU") ? `Сканирование: ${st.current_artist}` : `Scanning: ${st.current_artist}`) : (currentLang.startsWith("RU") ? "Проверка..." : "Checking...");
      
      // Real-time library tree reloads as files download or fail during scan
      loadLibrary();
      wasRunning = true;
    } else {
      dot.className = "dot idle";
      txt.textContent = loadedTranslations.stage_idle || "Idling";
      if (wasRunning) {
        // Just transitioned from running to idling, refresh one final time!
        loadLibrary();
        wasRunning = false;
      }
    }

    let currentStage = st.current_stage || "Ожидание";
    
    // Live translation of dynamic status strings
    if (currentStage === "Ожидание") {
      currentStage = loadedTranslations.stage_idle || "Idling";
    } else if (currentStage === "✅ Завершено") {
      currentStage = loadedTranslations.stage_completed || "✅ Completed";
    } else if (currentStage === "⏹ Остановлено") {
      currentStage = loadedTranslations.stage_stopped || "⏹ Stopped";
    } else if (currentStage === "Ошибка авторизации") {
      currentStage = loadedTranslations.stage_auth_error || "Auth Error";
    } else if (currentStage === "Инициализация источника") {
      currentStage = currentLang.startsWith("RU") ? "Инициализация источника" : "Initializing source...";
    } else {
      if (!currentLang.startsWith("RU")) {
        currentStage = currentStage
          .replace("Сеть: разрешение артиста", "Net: resolving artist")
          .replace("Сеть: получение альбомов", "Net: fetching albums")
          .replace("Сеть: сбор треков", "Net: gathering tracks")
          .replace("Сеть: локальная сверка файлов...", "Net: checking local files...")
          .replace("Сеть: сбор", "Net: gathered")
          .replace("альбомов", "albums")
          .replace("Сбор треков: ", "Gathering tracks: ")
          .replace("Скачивание: ", "Downloading: ")
          .replace("подготовка...", "preparing...")
          .replace("Авто-сортировка: перенесён трек", "Auto-sort: migrated track")
          .replace("Авто-копирование: продублирован трек", "Auto-copy: duplicated track")
          .replace("из альбома", "from album")
          .replace("в альбом", "to album")
          .replace("без скачивания", "without download")
          .replace("Удалена опустевшая папка", "Deleted empty folder");
      }
    }

    const currentStageEl = document.getElementById("currentStage");
    if (currentStageEl) currentStageEl.textContent = currentStage;
    
    // Live update libraryStatusMsg based on current stage
    const libMsgEl = document.getElementById("libraryStatusMsg");
    if (libMsgEl) {
      if (st.running || (st.current_stage && st.current_stage !== "Ожидание" && st.current_stage !== "✅ Завершено" && st.current_stage !== "⏹ Остановлено" && st.current_stage !== "Ошибка авторизации")) {
        libMsgEl.textContent = currentStage;
        libMsgEl.style.display = "block";
      } else {
        libMsgEl.style.display = "none";
      }
    }

    const lastScanEl = document.getElementById("lastScan");
    if (lastScanEl) lastScanEl.textContent = st.last_scan ? new Date(st.last_scan).toLocaleTimeString() : "—";
    
    const nextScanEl = document.getElementById("nextScan");
    if (nextScanEl) nextScanEl.textContent = st.next_scan ? new Date(st.next_scan).toLocaleTimeString() : "—";

    const stProcessedEl = document.getElementById("stProcessed");
    if (stProcessedEl) stProcessedEl.textContent = st.processed || 0;
    
    const stFoundEl = document.getElementById("stFound");
    if (stFoundEl) stFoundEl.textContent = st.found || 0;
    
    const stDownloadedEl = document.getElementById("stDownloaded");
    if (stDownloadedEl) stDownloadedEl.textContent = st.downloaded || 0;
    
    const stSkippedEl = document.getElementById("stSkipped");
    if (stSkippedEl) stSkippedEl.textContent = st.skipped || 0;
    
    const stFailedEl = document.getElementById("stFailed");
    if (stFailedEl) stFailedEl.textContent = st.failed || 0;

    const currentArtistEl = document.getElementById("currentArtist");
    if (currentArtistEl) currentArtistEl.textContent = st.current_artist ? (currentLang.startsWith("RU") ? `Артист: ${st.current_artist}` : `Artist: ${st.current_artist}`) : "";
    
    const ffmpegWarnEl = document.getElementById("ffmpegWarn");
    if (ffmpegWarnEl) {
      ffmpegWarnEl.style.display = st.ffmpeg ? "none" : "inline";
      if (!st.ffmpeg) {
        ffmpegWarnEl.textContent = loadedTranslations.ffmpeg_warn || "⚠ ffmpeg not found";
      }
    }

    renderThreadTasks(st);

    const logBox = document.getElementById("log");
    if (logBox) {
      const logs = data.logs || [];
      logBox.innerHTML = logs.map(l => {
        let msg = l.message || "";
        if (!currentLang.startsWith("RU")) {
          msg = msg
            .replace("Resolved artist by ID:", "Resolved artist by ID:")
            .replace("Found", "Found")
            .replace("albums/singles", "albums/singles")
            .replace("Total unique tracks:", "Total unique tracks:")
            .replace("Downloaded:", "Downloaded:")
            .replace("Nothing new for", "Nothing new for")
            .replace("Scan finished. Downloaded:", "Scan finished. Downloaded:")
            .replace("Failed:", "Failed:")
            .replace("Авто-сортировка: перенесён трек", "Auto-sort: migrated track")
            .replace("из корня в альбом", "from root to album")
            .replace("из папки", "from folder")
            .replace("в главный альбом", "to main album")
            .replace("Удалена опустевшая папка сингла/дубликата:", "Deleted empty single/duplicate folder:")
            .replace("Удалена опустевшая папка сингла:", "Deleted empty single folder:")
            .replace("Удалена опустевшая папка:", "Deleted empty folder:")
            .replace("Авто-копирование: продублирован трек", "Auto-copy: duplicated track")
            .replace("в альбом", "to album")
            .replace("без скачивания", "without download")
            .replace("Инициализация источника", "Initializing source...")
            .replace("Скачивание:", "Downloading:")
            .replace("Ожидание", "Idling");
        }
        return `<div class="log-line ${esc(l.level)}">[${esc(l.ts)}] ${esc(l.level)}: ${esc(msg)}</div>`;
      }).join("");
      logBox.scrollTop = logBox.scrollHeight;
    }
  } catch (err) {
    console.error("Poll status error:", err);
  }
}

async function pollTracks() {
  try {
    const res = await fetch("/api/tracks");
    const tracks = await res.json();
    const tbody = document.querySelector("#tracksTable tbody");
    if (tbody) {
      tbody.innerHTML = tracks.map(t => `
        <tr>
          <td title="${esc(t.artist || '')}">${esc(t.artist || '')}</td>
          <td title="${esc(t.track || '')}">${esc(t.track || '')}</td>
          <td>${t.downloaded_at ? new Date(t.downloaded_at).toLocaleString(currentLang.startsWith("RU") ? "ru-RU" : "en-US") : ''}</td>
        </tr>
      `).join("");
    }
  } catch (err) {
    console.error("Poll tracks error:", err);
  }
}

document.addEventListener("DOMContentLoaded", async () => {
  loadSettings();
  
  // 1. Fetch available languages and load current language
  await loadAvailableLanguages();
  await loadLanguage(currentLang);
  
  // 2. Load the library tree
  loadLibrary();
  
  pollStatus();
  pollTracks();
  setInterval(pollStatus, 2000);
  setInterval(pollTracks, 5000);

  // Filter input event listener
  const artistSearchEl = document.getElementById("artistSearch");
  if (artistSearchEl) {
    artistSearchEl.addEventListener("input", (e) => {
      renderLibraryTree(e.target.value);
    });
  }

  document.addEventListener("click", (e) => {
    const suggest = document.getElementById("artistSuggest");
    const input = document.getElementById("artistInput");
    if (suggest && !suggest.contains(e.target) && e.target !== input) {
      suggest.style.display = "none";
    }
  });
});
