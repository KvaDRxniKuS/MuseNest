let config = {};
let allArtists = [];
let allBlack = [];
let searchTimeout = null;
let searchReqId = 0;

let libraryData = { artists: [] };
let expandedNodes = {}; // persistent expanded states: "art_Name", "alb_Name_ID", "genre_Name"
let currentlyCopiedText = "";
let lastLogsHtml = "";
let currentLogsCached = [];
let lastLibraryHtml = "";

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
    el.innerHTML = `<span style="vertical-align: middle;">${msg}</span>`;
    el.style.display = "flex";
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
      const parts = lang.split("-");
      const label = parts[1] || parts[0];
      const opt = document.createElement("option");
      opt.value = lang;
      opt.textContent = label;
      select.appendChild(opt);
    });
    
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
    const baselineRes = await fetch("/api/translations/EN-English");
    const baseline = await baselineRes.json();
    
    if (langName !== "EN-English") {
      const res = await fetch(`/api/translations/${langName}`);
      const target = await res.json();
      loadedTranslations = Object.assign({}, baseline, target);
    } else {
      loadedTranslations = baseline;
    }
    
    applyLanguage();
    if (loadedTranslations.appTitle) {
      document.title = loadedTranslations.appTitle.replace(/^🎵\s*/, "") || "MuseNest";
    }
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

  const artsHint = document.querySelector(".add-wrap + .hint");
  if (artsHint) artsHint.textContent = dict.artists_hint || "";

  const blHint = document.querySelector("#blackList + .hint");
  if (blHint) blHint.textContent = dict.blacklist_hint || "";

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
  
  const artistCountEl = document.getElementById("artistCount");
  if (artistCountEl) {
    artistCountEl.textContent = `(${allArtists.length})`;
  }
  
  syncTileTexts();
}

function syncTileTexts() {
  ['interval', 'tolerance', 'maxAlbums', 'threads'].forEach(id => {
    const sel = document.getElementById(id);
    const txt = document.getElementById('txt_' + id);
    if (sel && txt) {
      txt.textContent = sel.options[sel.selectedIndex]?.text || '';
    }
  });
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
    folders: config.folders || [],
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

async function importLocalFolders() {
  await saveSettings();
  const btn = document.getElementById("libImportFolderBtn");
  if (!btn) return;
  const oldText = btn.textContent;
  btn.textContent = currentLang.startsWith("RU") ? "⏳ Импорт..." : "⏳ Importing...";
  btn.disabled = true;
  showLibStatus(currentLang.startsWith("RU") ? "🔍 Сканирование папки сохранения и сопоставление артистов..." : "Scanning save folder and resolving artists...");
  try {
    const res = await fetch("/api/library/import_local", { method: "POST" });
    const data = await res.json();
    if (data.ok) {
      if (data.added > 0) {
        libraryData = data.library;
        await loadSettings(); // sync config artists with frontend
        alert(currentLang.startsWith("RU") ? `Успешно импортировано новых артистов: ${data.added}!` : `Successfully imported ${data.added} new artists!`);
      } else {
        alert(data.message || (currentLang.startsWith("RU") ? "Новых артистов не обнаружено на диске." : "No new artists found on disk."));
      }
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
      children.style.display = "block";
    } else {
      children.classList.remove("expanded");
      arrow.classList.remove("expanded");
      children.style.display = "none";
    }
  }
}

/* ------------------------- HTML5 Drag and Drop Handlers ------------------------ */

function onArtistDragStart(event, artName) {
  event.dataTransfer.setData("text/plain", artName);
  event.dataTransfer.setData("text/type", "artist");
  event.dataTransfer.effectAllowed = "move";
}

function onFolderDragStart(event, catName) {
  event.dataTransfer.setData("text/plain", catName);
  event.dataTransfer.setData("text/type", "folder");
  event.dataTransfer.effectAllowed = "move";
}

function onFolderDragOver(event, element) {
  event.preventDefault();
  element.style.borderColor = "#1db954";
  element.style.backgroundColor = "rgba(29, 185, 84, 0.12)";
}

function onFolderDragLeave(event, element) {
  element.style.borderColor = "rgba(29, 185, 84, 0.15)";
  element.style.backgroundColor = "rgba(29, 185, 84, 0.05)";
}

async function onArtistDrop(event, targetGenre, element) {
  event.preventDefault();
  if (element) {
    element.style.borderColor = "rgba(29, 185, 84, 0.15)";
    element.style.backgroundColor = "rgba(29, 185, 84, 0.05)";
  }
  
  const dragType = event.dataTransfer.getData("text/type");
  const value = event.dataTransfer.getData("text/plain");
  
  if (dragType === "artist") {
    if (value && value.toLowerCase() !== targetGenre.toLowerCase()) {
      await changeArtistGenrePath(value, targetGenre);
    }
  } else if (dragType === "folder") {
    if (value && value.toLowerCase() !== targetGenre.toLowerCase()) {
      await moveFolderCategory(value, targetGenre);
    }
  }
}

async function moveFolderCategory(oldPath, targetParentPath) {
  const oldParts = oldPath.split('/');
  const leafName = oldParts[oldParts.length - 1];
  
  // Calculate new nested path: "Rock" + "Symphonic" -> "Rock/Symphonic"
  const newPath = targetParentPath ? (targetParentPath + "/" + leafName) : leafName;
  
  if (oldPath === newPath) return;
  
  const confirmMsg = currentLang.startsWith("RU")
    ? `Вы действительно хотите переместить папку "${oldPath}" внутрь "${targetParentPath}"? Все артисты и файлы на диске будут перенесены.`
    : `Are you sure you want to move folder "${oldPath}" into "${targetParentPath}"? All artists and files on disk will be migrated.`;
    
  if (confirm(confirmMsg)) {
    showLibStatus(currentLang.startsWith("RU") ? `⏳ Перемещение папки ${oldPath}...` : `⏳ Migrating folder ${oldPath}...`);
    
    // 1. Update genre_path for all artists whose path starts with oldPath or is equal to it
    allArtists.forEach(a => {
      if (typeof a === "object") {
        let ap = (a.genre_path || "").trim().replace(/\\/g, '/').replace(/^\/+|\/+$/g, '');
        if (ap === oldPath) {
          a.genre_path = newPath;
        } else if (ap.startsWith(oldPath + "/")) {
          a.genre_path = ap.replace(oldPath + "/", newPath + "/");
        }
      }
    });
    
    // 2. Update empty folders list in config
    if (config.folders) {
      config.folders = config.folders.map(f => {
        let fp = f.trim().replace(/\\/g, '/').replace(/^\/+|\/+$/g, '');
        if (fp === oldPath) {
          return newPath;
        } else if (fp.startsWith(oldPath + "/")) {
          return fp.replace(oldPath + "/", newPath + "/");
        }
        return f;
      });
      config.folders = [...new Set(config.folders)];
    }
    
    await saveSettings();
    await updateLibraryMetadata();
    hideLibStatus();
  }
}

/* ------------------------- Collapsible Folder Tree render engine ------------------------ */

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

  // Collect all unique non-empty category/genre paths currently in use
  const existingGenres = [...new Set([
    ...(config.folders || []),
    ...allArtists.map(a => a.genre_path).filter(Boolean).map(g => g.trim())
  ])].sort();

  // Group artists by genre_path
  const groups = {};
  
  // Initialize folders so they exist in groups even if empty!
  existingGenres.forEach(cat => {
    groups[cat] = [];
  });
  groups[""] = []; // root category

  artists.forEach(art => {
    let gp = (art.genre_path || "").trim();
    gp = gp.replace(/\\/g, '/').replace(/^\/+|\/+$/g, '');
    if (!groups[gp]) groups[gp] = [];
    groups[gp].push(art);
  });

  // Get sorted list of categories (putting "No Category" at the bottom)
  const sortedCategories = Object.keys(groups).sort((a, b) => {
    if (a === "") return 1;
    if (b === "") return -1;
    return a.localeCompare(b);
  });
  
  let html = "";
  
  sortedCategories.forEach(cat => {
    const catArtists = groups[cat] || [];
    
    // Filter artists inside this category by search filter
    const matchedCategoryArtists = [];
    catArtists.forEach(art => {
      const artName = art.name || "";
      const artId = art.id || artName;
      const isArtIgnored = !!art.ignored;
      
      let artMatches = artName.toLowerCase().includes(f);
      let matchedAlbums = [];
      
      const realAlbums = art.albums || [];
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
        matchedCategoryArtists.push({
          ...art,
          albums: matchedAlbums,
          forceExpandArt: f && (artMatches || matchedAlbums.length > 0)
        });
      }
    });
    
    // Hide empty folders only if search query is active
    if (matchedCategoryArtists.length === 0 && (f !== "" || cat === "")) return;
    
    // Render the folder wrapper header (if it is a non-empty or empty genre folder)
    const isSpecialCategory = (cat !== "");
    if (isSpecialCategory) {
      const catKey = "genre_" + cat.replace(/[^a-zA-Z0-9]/g, "_");
      const isCatExpanded = f || !!expandedNodes[catKey];
      
      html += `
        <div class="genre-folder" style="margin-bottom: 10px;">
          <div onclick="toggleNodeExpand('${catKey}')" draggable="true" ondragstart="onFolderDragStart(event, '${esc(cat)}')" ondragover="onFolderDragOver(event, this)" ondragleave="onFolderDragLeave(event, this)" ondrop="onArtistDrop(event, '${esc(cat)}', this)" class="genre-folder-header" style="display: flex; align-items: center; justify-content: space-between; padding: 6px 10px; background: rgba(29, 185, 84, 0.05); border: 1px solid rgba(29, 185, 84, 0.15); border-radius: 6px; cursor: pointer; transition: all 0.15s ease; width: 100%;" onmouseover="this.style.borderColor='rgba(29,185,84,0.3)'" onmouseout="this.style.borderColor='rgba(29,185,84,0.15)'">
            <span style="font-size: 0.82rem; font-weight: 600; color: #1db954; display: flex; align-items: center; gap: 6px; user-select: none;">
              <span class="tree-node-arrow ${isCatExpanded ? 'expanded' : ''}" style="color: #1db954; font-size: 0.65rem;">▶</span>
              <span>📁 ${esc(cat)}</span>
              <span style="font-size: 0.72rem; font-weight: normal; color: var(--text-muted);">(${matchedCategoryArtists.length})</span>
            </span>
            ${matchedCategoryArtists.length === 0 ? `<button class="del-btn" onclick="event.stopPropagation(); removeEmptyFolder('${esc(cat)}')" style="background: none; border: none; color: rgba(255,255,255,0.4); cursor: pointer; padding: 2px 6px; font-size: 0.75rem; transition: color 0.15s;" onmouseover="this.style.color='#ff4a4a'" onmouseout="this.style.color='rgba(255,255,255,0.4)'">✕</button>` : ""}
          </div>
          <div class="tree-node-children ${isCatExpanded ? 'expanded' : ''}" id="children_${catKey}" style="padding: 6px 0 0 10px; border-left: 1px dashed rgba(29, 185, 84, 0.15); margin-left: 15px; margin-top: 4px; display: ${isCatExpanded ? 'block' : 'none'};">
      `;
      
      if (matchedCategoryArtists.length === 0) {
        html += `
          <div style="color:var(--text-muted);font-style:italic;font-size:0.75rem;padding:0.5rem 1rem;">
            ${currentLang.startsWith("RU") ? "Перетащите сюда артиста..." : "Drag an artist here..."}
          </div>
        `;
      }
    }
    
    // Render artists inside this category
    matchedCategoryArtists.forEach(art => {
      const artName = art.name || "";
      const artId = art.id || artName;
      const isArtIgnored = !!art.ignored;
      
      const realAlbums = art.albums || [];
      const isArtCompleted = realAlbums.length > 0 && realAlbums.every(alb => {
        const realTracks = alb.tracks || [];
        return realTracks.length > 0 && realTracks.every(t => t.downloaded);
      });
      
      const forceExpandArt = art.forceExpandArt;
      const artKey = "art_" + artName;
      const isArtExpanded = forceExpandArt || !!expandedNodes[artKey];
      
      let listenUrl = '';
      let listenTitle = 'Open Link';
      let clickHandler = '';
      const artSource = art.source || 'deezer';
      if (artSource === 'spotify') {
        const spId = art.spotify_id || (art.id && !/^\d+$/.test(art.id) ? art.id : null);
        if (spId) {
          listenUrl = 'https://open.spotify.com/artist/' + spId;
          listenTitle = 'Open Spotify';
        } else {
          listenTitle = currentLang.startsWith("RU") ? 'Spotify ID не найден' : 'Spotify ID not found';
          clickHandler = `alert('${currentLang.startsWith("RU") ? "Spotify ID не найден для этого исполнителя. Убедитесь, что вы настроили Client ID & Client Secret в настройках, и нажмите \\'Обновить из сети\\'." : "Spotify ID not found for this artist. Make sure you configure Client ID & Client Secret in settings and click \\'Update from Net\\'."}'); return false;`;
        }
      } else { // deezer
        const dzId = art.deezer_id || (art.id && /^\d+$/.test(art.id) ? art.id : null);
        if (dzId) {
          listenUrl = 'https://www.deezer.com/artist/' + dzId;
          listenTitle = 'Open Deezer';
        } else {
          listenTitle = currentLang.startsWith("RU") ? 'Deezer ID не найден' : 'Deezer ID not found';
          clickHandler = `alert('${currentLang.startsWith("RU") ? "Deezer ID не найден для этого исполнителя. Попробуйте обновить из сети." : "Deezer ID not found for this artist. Try updating from Net."}'); return false;`;
        }
      }

      const artHeaderClass = art.loading ? "not-completed-gray" : (isArtCompleted ? "completed-green" : "not-completed-gray");

      html += `
        <div class="tree-node" draggable="true" ondragstart="onArtistDragStart(event, '${esc(artName)}')" style="margin-left: 0 !important; padding-left: 0 !important; border-left: 1px solid ${isArtCompleted ? 'rgba(16, 185, 129, 0.25)' : 'rgba(255, 255, 255, 0.06)'} !important; border-top: 1px solid ${isArtCompleted ? 'rgba(16, 185, 129, 0.25)' : 'rgba(255, 255, 255, 0.06)'}; border-right: 1px solid ${isArtCompleted ? 'rgba(16, 185, 129, 0.25)' : 'rgba(255, 255, 255, 0.06)'}; border-bottom: 1px solid ${isArtCompleted ? 'rgba(16, 185, 129, 0.25)' : 'rgba(255, 255, 255, 0.06)'}; background: rgba(255, 255, 255, 0.01); border-radius: 8px; overflow: hidden; margin-bottom: 10px; cursor: grab;">
          <div class="tree-node-header ${artHeaderClass}" onclick="toggleNodeExpand('${artKey}')" style="display: flex; flex-direction: column; align-items: stretch; padding: 10px 12px; gap: 4px; height: auto; border: none !important; border-bottom: 1px solid rgba(255,255,255,0.04) !important; border-radius: 0; background: ${isArtCompleted ? 'rgba(16, 185, 129, 0.03)' : 'rgba(255,255,255,0.02)'} !important;">
            
            <!-- TOP ROW: Name + Delete button -->
            <div style="display: flex; justify-content: space-between; align-items: center; width: 100%;">
              <span class="tree-node-title" title="${esc(artName)}" style="font-size: 0.92rem; font-weight: 600; display: flex; align-items: center; gap: 4px;">
                <span class="tree-node-arrow ${isArtExpanded ? 'expanded' : ''}">▶</span>
                <span class="${isArtIgnored ? 'ignored-text' : ''}" style="color: var(--text);">${esc(artName)}</span>
                ${art.loading ? `<span style="font-size:0.75rem; font-weight:normal; color:var(--text-muted); margin-left: 4px;">(${currentLang.startsWith("RU") ? "добавление..." : "adding..."})</span>` : ""}
              </span>
              <button class="del-btn" onclick="event.stopPropagation(); removeArtistByName('${esc(artName)}')" style="background: none; border: none; color: rgba(255,255,255,0.4); cursor: pointer; padding: 4px; font-size: 0.9rem; transition: color 0.15s;">✕</button>
            </div>
            
            <!-- BOTTOM ROW: Meta icons and toggles -->
            <div class="tree-actions" onclick="event.stopPropagation()" style="display: flex; align-items: center; flex-wrap: nowrap !important; gap: 8px; font-size: 0.7rem; color: var(--text-muted); border-top: 1px solid rgba(255,255,255,0.03); padding-top: 4px; margin-top: 2px; width: 100%; height: auto; padding-left: 18px; justify-content: space-between; overflow: hidden;">
              
              <!-- Left grouped metadata icons -->
              <div style="display: flex; align-items: center; gap: 8px; flex-wrap: nowrap; overflow: hidden;">
                <!-- Source selector -->
                <div style="display: flex; align-items: center; gap: 2px; flex-shrink: 0;">
                  <span>🌐</span>
                  <select class="source-select" onchange="changeArtistSource('${esc(artName)}', this.value, event)" style="padding: 1px 4px; background: rgba(255,255,255,0.05); color: var(--text); border: 1px solid rgba(255,255,255,0.1); border-radius: 4px; font-size: 0.68rem; outline: none; cursor: pointer;">
                    <option value="deezer" ${artSource === 'deezer' ? 'selected' : ''}>Deezer</option>
                    <option value="spotify" ${artSource === 'spotify' ? 'selected' : ''}>Spotify</option>
                  </select>
                  ${art.fallback_deezer ? `<span style="color: #ffb300; font-size: 0.75rem; cursor: help; margin-left: 2px; display: inline-flex; align-items: center;" title="${esc((() => {
                    const ru = currentLang.startsWith("RU");
                    if (art.fallback_reason === "not_found") {
                      return ru ? "Артист не найден в Spotify по имени. Добавьте его поиском или вставьте ссылку open.spotify.com/artist/…" : "Artist not found on Spotify by name. Search again or paste an open.spotify.com/artist/… link.";
                    }
                    if (art.fallback_reason === "error") {
                      return ru ? "Ошибка запроса к Spotify (сеть/прокси). Ключи при этом могут быть верными. Данные из Deezer." : "Spotify request failed (network/proxy). Keys may still be valid. Loaded from Deezer.";
                    }
                    return ru ? "Ключи Spotify отсутствуют или неверны. Данные загружены из Deezer." : "Spotify keys missing or invalid. Loaded from Deezer.";
                  })())}">⚠️</span>` : ""}
                </div>
                
                <!-- Followers -->
                <div style="display: flex; align-items: center; gap: 2px; flex-shrink: 0;" title="Followers">
                  <span>👥</span>
                  <span>${esc(art.followers != null ? art.followers.toLocaleString() : '0')}</span>
                </div>

                <!-- Genre Subfolder Selector (No more text fields!) -->
                <div style="display: flex; align-items: center; gap: 2px; flex-shrink: 1; overflow: hidden;">
                  <span>📁</span>
                  <select class="genre-select" onchange="onGenreSelect('${esc(artName)}', this)" style="max-width: 65px; padding: 1px 4px; background: rgba(255,255,255,0.05); color: var(--text); border: 1px solid rgba(255,255,255,0.1); border-radius: 4px; font-size: 0.68rem; outline: none; cursor: pointer; text-overflow: ellipsis;" title="${currentLang.startsWith("RU") ? 'Выберите папку или создайте новую' : 'Choose subfolder or create new'}">
                    <option value="" ${!art.genre_path ? 'selected' : ''}>—</option>
                    ${existingGenres.map(g => `<option value="${esc(g)}" ${art.genre_path === g ? 'selected' : ''}>${esc(g)}</option>`).join('')}
                    <option value="__NEW_GENRE__" style="color: #1db954; font-weight: bold;">➕ ${currentLang.startsWith("RU") ? 'Новая...' : 'New...'}</option>
                  </select>
                </div>
                
                <!-- Headphone link -->
                <div style="display: flex; align-items: center; gap: 2px; flex-shrink: 0;">
                  <a href="${listenUrl ? esc(listenUrl) : '#'}" ${clickHandler ? `onclick="${clickHandler}"` : 'target="_blank"'} style="color: var(--text-muted); text-decoration: none; display: flex; align-items: center; gap: 2px;" title="${esc(listenTitle)}">
                    <span>🎧</span>
                    <span style="border-bottom: 1px dashed rgba(255,255,255,0.3); font-size: 0.65rem;">${artSource === 'spotify' ? 'Spotify' : 'Deezer'}</span>
                  </a>
                </div>
              </div>
              
              <!-- Ignore button (pill) -->
              <div style="display: flex; align-items: center; flex-shrink: 0;" onclick="event.stopPropagation()">
                <span class="ignore-pill ${isArtIgnored ? 'active' : ''}" onclick="toggleIgnore('${esc(artId)}', null, null, event)" style="cursor: pointer; padding: 2px 6px; border-radius: 8px; font-size: 0.6rem; font-weight: 500; display: inline-flex; align-items: center; gap: 2px; transition: all 0.15s ease; ${isArtIgnored ? 'background: rgba(239,68,68,0.15); border: 1px solid rgba(239,68,68,0.3); color:#ef4444;' : 'background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.08); color:var(--text-muted);'}" onmouseover="this.style.borderColor='#ef4444'; this.style.color='#ef4444'" onmouseout="this.style.borderColor='${isArtIgnored ? 'rgba(239,68,68,0.3)' : 'rgba(255,255,255,0.08)'}'; this.style.color='${isArtIgnored ? '#ef4444' : 'var(--text-muted)'}'">
                  <span>🚫</span> <span>${loadedTranslations.ignore_label || "Ignore"}</span>
                </span>
              </div>
              
            </div>
          </div>
          <div class="tree-node-children ${isArtExpanded ? 'expanded' : ''}" id="children_${artKey}" style="padding: 6px 10px 10px 10px; background: rgba(0,0,0,0.12); margin-bottom: 0 !important; padding-bottom: 10px !important;">
      `;

      if (art.loading) {
        html += `
          <div style="color:var(--text-muted);font-style:italic;font-size:0.75rem;padding:0.5rem 1rem; display:flex; align-items:center; gap:0.5rem;">
            <span>⏳</span> ${currentLang.startsWith("RU") ? "Загрузка дискографии..." : "Loading discography..."}
          </div>
        `;
      }
      
      art.albums.forEach(alb => {
        const albName = alb.name || "";
        const albId = alb.id;
        const isAlbIgnored = !!alb.ignored;
        const albKey = "alb_" + artName + "_" + albId;
        const isAlbExpanded = alb.forceExpand || !!expandedNodes[albKey];
        
        const realTracks = alb.tracks || [];
        const isAlbCompleted = realTracks.length > 0 && realTracks.every(t => t.downloaded);
        
        html += `
          <div class="tree-node" style="margin-left: 0; margin-bottom: 6px;">
            <div onclick="toggleNodeExpand('${albKey}')" style="display: flex; align-items: center; justify-content: space-between; padding: 6px 8px; border-left: 3px solid ${isAlbCompleted ? '#10b981' : 'rgba(255,255,255,0.15)'}; background: rgba(255,255,255,0.02); cursor: pointer; transition: background-color 0.15s; border-radius: 0 4px 4px 0;" onmouseover="this.style.backgroundColor='rgba(255,255,255,0.05)'" onmouseout="this.style.backgroundColor='rgba(255,255,255,0.02)'">
              
              <!-- Left: Expand arrow + title -->
              <span class="tree-node-title" title="${esc(albName)}" style="font-size: 0.85rem; font-weight: 500; display: flex; align-items: center; gap: 4px;">
                <span class="tree-node-arrow ${isAlbExpanded ? 'expanded' : ''}" style="font-size: 0.7rem; color: var(--text-muted);">▶</span>
                <span class="${isArtIgnored || isAlbIgnored ? 'ignored-text' : ''}" style="color: ${isAlbCompleted ? '#10b981' : 'var(--text)'};">${esc(albName)}</span>
              </span>
              
              <!-- Right: Ignore button -->
              <div class="tree-actions" onclick="event.stopPropagation()">
                <span class="ignore-pill ${isAlbIgnored ? 'active' : ''}" onclick="toggleIgnore('${esc(artId)}', '${esc(albId)}', null, event)" style="cursor: pointer; padding: 1px 6px; border-radius: 8px; font-size: 0.6rem; font-weight: 500; display: inline-flex; align-items: center; gap: 2px; transition: all 0.15s ease; ${isAlbIgnored ? 'background: rgba(239,68,68,0.15); border: 1px solid rgba(239,68,68,0.3); color:#ef4444;' : 'background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.08); color:var(--text-muted);'}" onmouseover="this.style.borderColor='#ef4444'; this.style.color='#ef4444'" onmouseout="this.style.borderColor='${isAlbIgnored ? 'rgba(239,68,68,0.3)' : 'rgba(255,255,255,0.08)'}'; this.style.color='${isAlbIgnored ? '#ef4444' : 'var(--text-muted)'}'">
                  <span>🚫</span> <span>${loadedTranslations.ignore_label || "Ignore"}</span>
                </span>
              </div>
              
            </div>
            <div class="tree-node-children ${isAlbExpanded ? 'expanded' : ''}" id="children_${albKey}" style="padding-left: 10px; margin-top: 4px; margin-bottom: 6px;">
        `;
        
        alb.tracks.forEach(trk => {
          const trkName = trk.name || "";
          const trkId = trk.id;
          const isTrkIgnored = !!trk.ignored;
          const isDownloaded = !!trk.downloaded;
          const isNoMatch = !!trk.no_match;
          
          const errCode = trk.error_code || "ERR-1";
          const labelKey = `err_${errCode.split("-")[1]}_label`;
          const titleKey = `err_${errCode.split("-")[1]}_title`;
          const badgeLabel = loadedTranslations[labelKey] || errCode;
          const badgeTitle = loadedTranslations[titleKey] || "Error occurred";
          
          const errBadge = (!isDownloaded && isNoMatch) ? `<span class="failed-badge" style="margin-left: 6px; flex: none; font-size: 0.6rem; padding: 1px 4px; border-radius: 3px;" title="${esc(badgeTitle)}">${esc(badgeLabel)}</span>` : "";
          
          html += `
            <div class="tree-track" style="margin-left: 0; padding: 3px 0; border-bottom: 1px solid rgba(255,255,255,0.02);">
              <div style="display: flex; align-items: center; justify-content: space-between; padding: 4px 6px; border-radius: 4px; background: rgba(255,255,255,0.015); transition: background-color 0.15s;" onmouseover="this.style.backgroundColor='rgba(255,255,255,0.04)'" onmouseout="this.style.backgroundColor='rgba(255,255,255,0.015)'">
                
                <!-- Left: Status dot + name -->
                <div style="display: flex; align-items: center; gap: 8px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">
                  <span style="display: inline-block; width: 6px; height: 6px; border-radius: 50%; background-color: ${isDownloaded ? '#10b981' : (isNoMatch ? '#ef4444' : '#6c757d')}; box-shadow: 0 0 6px ${isDownloaded ? 'rgba(16, 185, 129, 0.5)' : (isNoMatch ? 'rgba(239, 68, 68, 0.5)' : 'transparent')}; flex-shrink: 0;"></span>
                  <span class="${isArtIgnored || isAlbIgnored || isTrkIgnored ? 'ignored-text' : ''}" style="color: ${isDownloaded ? '#10b981' : (isNoMatch ? '#ef4444' : 'var(--text-muted)')}; font-size: 0.82rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;" title="${esc(trkName)}">${esc(trkName)}</span>
                  ${errBadge}
                </div>
                
                <!-- Right: Ignore button -->
                <div style="display: flex; align-items: center; gap: 8px; flex-shrink: 0;" onclick="event.stopPropagation()">
                  <span class="ignore-pill ${isTrkIgnored ? 'active' : ''}" onclick="toggleIgnore('${esc(artId)}', '${esc(albId)}', '${esc(trkId)}', event)" style="cursor: pointer; padding: 1px 6px; border-radius: 8px; font-size: 0.6rem; font-weight: 500; display: inline-flex; align-items: center; gap: 2px; transition: all 0.15s ease; ${isTrkIgnored ? 'background: rgba(239,68,68,0.15); border: 1px solid rgba(239,68,68,0.3); color:#ef4444;' : 'background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.08); color:var(--text-muted);'}" onmouseover="this.style.borderColor='#ef4444'; this.style.color='#ef4444'" onmouseout="this.style.borderColor='${isTrkIgnored ? 'rgba(239,68,68,0.3)' : 'rgba(255,255,255,0.08)'}'; this.style.color='${isTrkIgnored ? '#ef4444' : 'var(--text-muted)'}'">
                    <span>🚫</span> <span>${loadedTranslations.ignore_label || "Ignore"}</span>
                  </span>
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
      
      if (art.albums.length === 0 && !art.loading) {
        html += `
          <div style="color:var(--text-muted);font-style:italic;font-size:0.75rem;padding:0.3rem 1rem;">
            ${loadedTranslations.no_albums_hint || "No albums."}
          </div>`;
      }
      
      html += `
          </div>
        </div>
      `;
    });
    
    if (isSpecialCategory) {
      html += `
          </div>
        </div>
      `;
    }
  });

  // Render datalist of existing categories for autocomplete in inputs
  html += `
    <datalist id="genrePaths">
      ${existingGenres.map(g => `<option value="${esc(g)}">`).join('')}
    </datalist>
  `;
  
  if (html !== lastLibraryHtml) {
    container.innerHTML = html;
    lastLibraryHtml = html;
  }
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

async function changeArtistSource(artName, newSource, event) {
  if (event) event.stopPropagation();
  const idx = allArtists.findIndex(a => {
    const name = (typeof a === "object" ? a.name : a) || "";
    return name.toLowerCase() === artName.toLowerCase();
  });
  if (idx !== -1) {
    if (typeof allArtists[idx] === "string") {
      allArtists[idx] = {
        name: allArtists[idx],
        id: null,
        source: newSource,
        spotify_id: null,
        deezer_id: null,
        spotify_name: null,
        genre_path: ""
      };
    } else {
      allArtists[idx].source = newSource;
    }
    
    // Mark as loading locally for visual feedback while updating metadata!
    if (libraryData.artists) {
      const artNode = libraryData.artists.find(a => a.name.toLowerCase() === artName.toLowerCase());
      if (artNode) {
        artNode.source = newSource;
        artNode.loading = true;
        renderLibraryTree(document.getElementById("artistSearch").value);
      }
    }
    
    showLibStatus(currentLang.startsWith("RU") ? `⏳ Изменение источника для ${artName}...` : `⏳ Changing source for ${artName}...`);
    await saveSettings();
    await updateLibraryMetadata();
    hideLibStatus();
  }
}

async function changeArtistGenrePath(artName, newPath) {
  const idx = allArtists.findIndex(a => {
    const name = (typeof a === "object" ? a.name : a) || "";
    return name.toLowerCase() === artName.toLowerCase();
  });
  if (idx !== -1) {
    if (typeof allArtists[idx] === "string") {
      allArtists[idx] = {
        name: allArtists[idx],
        id: null,
        source: config.music_source || "deezer",
        spotify_id: null,
        deezer_id: null,
        spotify_name: null,
        genre_path: newPath.trim()
      };
    } else {
      allArtists[idx].genre_path = newPath.trim();
    }
    
    // Mark as loading locally for visual feedback
    if (libraryData.artists) {
      const artNode = libraryData.artists.find(a => a.name.toLowerCase() === artName.toLowerCase());
      if (artNode) {
        artNode.genre_path = newPath.trim();
        artNode.loading = true;
        renderLibraryTree(document.getElementById("artistSearch").value);
      }
    }
    
    showLibStatus(currentLang.startsWith("RU") ? `⏳ Перемещение файлов для ${artName}...` : `⏳ Migrating files for ${artName}...`);
    await saveSettings();
    await updateLibraryMetadata();
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
        body: JSON.stringify({
          query: q,
          source: (document.getElementById("musicSource") || {}).value || config.music_source || "deezer",
        }),
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
        div.style.cssText = "display: flex; justify-content: space-between; align-items: center; padding: 6px 12px; cursor: pointer; gap: 12px;";
        
        const isRu = currentLang.startsWith("RU");
        const followersStr = item.followers ? item.followers.toLocaleString() + (isRu ? " подписчиков" : " followers") : (isRu ? "0 подписчиков" : "0 followers");
        const idLabel = item.spotify_id
          ? "Spotify ID: " + item.spotify_id
          : "Deezer ID: " + (item.deezer_id || item.id || "—");

        div.innerHTML = `
          <span style="font-weight: 500; font-size: 0.88rem; color: var(--text);">${esc(item.name)}</span>
          <div style="display: flex; align-items: center; gap: 8px; margin-left: auto;">
            <small style="color: var(--text-muted); font-size: 0.72rem; text-align: right; display: inline-block;">
              ${esc(followersStr)} · ${esc(idLabel)}
            </small>
            ${item.link ? `<a href="${esc(item.link)}" target="_blank" onmousedown="event.stopPropagation()" style="display: inline-flex; align-items: center; justify-content: center; width: 22px; height: 22px; border-radius: 50%; border: 1px solid rgba(255,255,255,0.15); background: rgba(255,255,255,0.05); color: var(--text); text-decoration: none; font-size: 0.7rem; transition: all 0.15s ease;" onmouseover="this.style.borderColor='#1db954'; this.style.background='rgba(29,185,84,0.1)'" onmouseout="this.style.borderColor='rgba(255,255,255,0.15)'; this.style.background='rgba(255,255,255,0.05)'" title="${isRu ? "Открыть страницу артиста" : "Open artist page"}">🎧</a>` : ""}
          </div>
        `;
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
    const artName = item.name;
    allArtists.push({
      id: item.spotify_id || item.id || null,
      name: artName,
      source: item.spotify_id ? "spotify" : (config.music_source || "deezer"),
      spotify_name: item.spotify_name || (item.spotify_id ? artName : null),
      spotify_id: item.spotify_id || null,
      deezer_id: item.deezer_id || null,
      genre_path: ""
    });
    
    // Add placeholder immediately for instant visual feedback
    if (!libraryData.artists) libraryData.artists = [];
    if (!libraryData.artists.some(a => a.name.toLowerCase() === artName.toLowerCase())) {
      libraryData.artists.push({
        id: item.spotify_id || item.id || null,
        name: artName,
        source: item.spotify_id ? "spotify" : (config.music_source || "deezer"),
        spotify_name: item.spotify_name || (item.spotify_id ? artName : null),
        spotify_id: item.spotify_id || null,
        deezer_id: item.deezer_id || null,
        followers: item.followers || 0,
        ignored: false,
        albums: [],
        loading: true,
        genre_path: ""
      });
      libraryData.artists.sort((a, b) => a.name.localeCompare(b.name, currentLang.startsWith("RU") ? 'ru' : 'en'));
      renderLibraryTree(document.getElementById("artistSearch").value);
    }
    
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
      spotify_id: null,
      deezer_id: null,
      genre_path: ""
    });
    
    // Add placeholder immediately for instant visual feedback
    if (!libraryData.artists) libraryData.artists = [];
    if (!libraryData.artists.some(a => a.name.toLowerCase() === val.toLowerCase())) {
      libraryData.artists.push({
        id: null,
        name: val,
        source: config.music_source || "deezer",
        spotify_name: null,
        spotify_id: null,
        deezer_id: null,
        followers: 0,
        ignored: false,
        albums: [],
        loading: true,
        genre_path: ""
      });
      libraryData.artists.sort((a, b) => a.name.localeCompare(b.name, currentLang.startsWith("RU") ? 'ru' : 'en'));
      renderLibraryTree(document.getElementById("artistSearch").value);
    }
    
    showLibStatus((currentLang.startsWith("RU") ? `⏳ Добавление ${val}: загрузка структуры альбомов и треков...` : `⏳ Adding ${val}: fetching album and track structure...`));
    await saveSettings();
    await updateLibraryMetadata();
    hideLibStatus();
  }
}

async function addEmptyFolder() {
  const promptMsg = currentLang.startsWith("RU")
    ? "Введите имя новой папки (например: Rock/Metal):"
    : "Enter new folder path (e.g. Rock/Metal):";
  const name = prompt(promptMsg);
  if (name && name.trim()) {
    const cleaned = name.trim().replace(/\\/g, '/').replace(/^\/+|\/+$/g, '');
    if (!config.folders) config.folders = [];
    if (!config.folders.includes(cleaned)) {
      config.folders.push(cleaned);
      showLibStatus(currentLang.startsWith("RU") ? `⏳ Создание папки ${cleaned}...` : `⏳ Creating folder ${cleaned}...`);
      await saveSettings();
      await loadLibrary();
      hideLibStatus();
    }
  }
}

async function removeEmptyFolder(catName) {
  if (config.folders) {
    const idx = config.folders.indexOf(catName);
    if (idx !== -1) {
      config.folders.splice(idx, 1);
      showLibStatus(currentLang.startsWith("RU") ? `⏳ Удаление папки ${catName}...` : `⏳ Removing folder ${catName}...`);
      await saveSettings();
      await loadLibrary();
      hideLibStatus();
    }
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
      
      loadLibrary();
      wasRunning = true;
    } else {
      dot.className = "dot idle";
      txt.textContent = loadedTranslations.stage_idle || "Idling";
      if (wasRunning) {
        loadLibrary();
        wasRunning = false;
      }
    }

    let currentStage = st.current_stage || "Ожидание";
    
    if (currentStage === "Ожидание") {
      currentStage = loadedTranslations.stage_idle || "Idling";
    } else if (currentStage === "✅ Завершено") {
      currentStage = loadedTranslations.stage_completed || "✅ Completed";
    } else if (currentStage === "⏹ Остановлено") {
      currentStage = loadedTranslations.stage_stopped || "⏹ Stopped";
    } else if (currentStage === "Ошибка авторизации") {
      currentStage = loadedTranslations.stage_auth_error || "Auth Error";
    } else if (currentStage === "Инициализация источника") {
      currentStage = currentLang.startsWith("RU") ? "Инициализация источника" : "Инициализация источника...";
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
    
    const libMsgEl = document.getElementById("libraryStatusMsg");
    if (libMsgEl) {
      if (st.running || (st.current_stage && st.current_stage !== "Ожидание" && st.current_stage !== "✅ Завершено" && st.current_stage !== "⏹ Остановлено" && st.current_stage !== "Ошибка авторизации")) {
        libMsgEl.innerHTML = `<span style="vertical-align: middle;">${esc(currentStage)}</span>`;
        libMsgEl.style.display = "flex";
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

    currentLogsCached = data.logs || [];
    renderLogs();
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
  
  await loadAvailableLanguages();
  await loadLanguage(currentLang);
  
  loadLibrary();
  
  pollStatus();
  pollTracks();
  setInterval(pollStatus, 2000);
  setInterval(pollTracks, 5000);

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

function renderLogs() {
  const logBox = document.getElementById("log");
  if (!logBox) return;
  
  const logsHtml = currentLogsCached.map(l => {
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
    
    const plainText = `[${l.ts}] ${l.level}: ${msg}`;
    const isCopied = (currentlyCopiedText && currentlyCopiedText.trim().replace(/\s+/g, ' ') === plainText.trim().replace(/\s+/g, ' '));
    
    return `<div class="log-line ${esc(l.level)}" onclick="copyLogLine(this, this.innerText)" style="cursor: pointer; position: relative; padding-right: 70px; ${isCopied ? 'background-color: rgba(29, 185, 84, 0.25) !important;' : ''}" title="${currentLang.startsWith("RU") ? "Нажмите для копирования" : "Click to copy"}">[${esc(l.ts)}] ${esc(l.level)}: ${esc(msg)}${isCopied ? `<span class="copied-pill" style="position: absolute; right: 10px; top: 50%; transform: translateY(-50%); font-size: 0.62rem; background: #1db954; color: white; padding: 2px 6px; border-radius: 4px; font-weight: bold; z-index: 10; pointer-events: none; animation: fade-out 1.2s forwards; display: inline-block;">Copied!</span>` : ''}</div>`;
  }).join("");
  
  if (logsHtml !== lastLogsHtml) {
    const oldScrollTop = logBox.scrollTop;
    const isAtBottom = logBox.scrollHeight - logBox.clientHeight <= logBox.scrollTop + 40;
    
    logBox.innerHTML = logsHtml;
    lastLogsHtml = logsHtml;
    
    if (isAtBottom) {
      logBox.scrollTop = logBox.scrollHeight;
    } else {
      logBox.scrollTop = oldScrollTop;
    }
  }
}

function copyLogLine(element, text) {
  if (text.endsWith("Copied!")) {
    text = text.substring(0, text.length - 7).trim();
  }
  currentlyCopiedText = text;
  navigator.clipboard.writeText(text).then(() => {
    renderLogs();
    setTimeout(() => {
      currentlyCopiedText = "";
      renderLogs();
    }, 1200);
  }).catch(err => {
    console.error('Failed to copy: ', err);
  });
}

async function onGenreSelect(artName, selectElement) {
  const val = selectElement.value;
  if (val === "__NEW_GENRE__") {
    const promptMsg = currentLang.startsWith("RU") 
      ? "Введите имя новой папки/жанра (подпапки разделяйте косой чертой, например: Rock/Symphonic):" 
      : "Enter new subfolder/genre path (separate levels with slashes, e.g., Rock/Symphonic):";
    const newPath = prompt(promptMsg);
    if (newPath && newPath.trim()) {
      await changeArtistGenrePath(artName, newPath.trim());
    } else {
      loadLibrary();
    }
  } else {
    await changeArtistGenrePath(artName, val);
  }
}
