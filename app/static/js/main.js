/* ATC Splitter — main.js
 * Uses WaveSurfer.js v7 (loaded from CDN in index.html)
 */

// ── Utility ──────────────────────────────────────────────────────────────────

const API = (path) => String(path || '').replace(/^\/+/, '');

async function apiFetch(url, opts = {}) {
  const res = await fetch(url, opts);
  if (!res.ok) {
    const msg = await res.text().catch(() => res.statusText);
    throw new Error(msg || `HTTP ${res.status}`);
  }
  return res.json();
}

function fmtSec(s) {
  const mm = String(Math.floor(s / 60)).padStart(2, '0');
  const ss = (s % 60).toFixed(2).padStart(5, '0');
  return `${mm}:${ss}`;
}

function waitMs(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

let toastTimer;
function toast(msg, kind = '') {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = `toast show${kind ? ' ' + kind : ''}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), 3000);
}

// ── State ─────────────────────────────────────────────────────────────────────

const ENABLE_TRANSCRIPTION = document.querySelector('meta[name="app-config"]')
  ?.dataset.enableTranscription === 'true';
const THEME_STORAGE_KEY = 'atc-splitter-theme';

let currentFile = null;          // { name, duration_sec, … }
let sourceWS    = null;          // WaveSurfer instance for source
let segmentWSMap = {};           // seg_name → WaveSurfer instance
let selectedSegs = new Set();    // checked segment names
const transcriptCache = {};      // sourceStem::seg_name -> transcript text

function updateSourcePlayButton() {
  const btn = document.getElementById('btn-play-source');
  if (!btn) return;
  btn.textContent = sourceWS && sourceWS.isPlaying() ? '⏸ Pause source' : '▶ Play source';
}

function toggleSourcePlayback() {
  if (!sourceWS) return;
  sourceWS.playPause();
  updateSourcePlayButton();
}

function transcriptKey(sourceStem, segName) {
  return `${sourceStem}::${segName}`;
}

// ── Bootstrap ─────────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
  setupThemeToggle();
  loadFiles();
  setupUpload();
  setupSourceControls();
  setupSegmentToolbar();

  const deleteAllBtn = document.getElementById('btn-delete-all-files');
  if (deleteAllBtn) deleteAllBtn.addEventListener('click', deleteAllFiles);
});

function setupThemeToggle() {
  const btn = document.getElementById('btn-theme-toggle');
  if (!btn) return;

  const root = document.documentElement;
  const savedTheme = localStorage.getItem(THEME_STORAGE_KEY);
  const systemPrefersDark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
  const initialTheme = savedTheme || (systemPrefersDark ? 'dark' : 'light');

  applyTheme(initialTheme, btn);

  btn.addEventListener('click', () => {
    const nextTheme = root.dataset.theme === 'light' ? 'dark' : 'light';
    localStorage.setItem(THEME_STORAGE_KEY, nextTheme);
    applyTheme(nextTheme, btn);
  });
}

function applyTheme(theme, btn) {
  const root = document.documentElement;
  root.dataset.theme = theme;
  if (theme === 'light') {
    btn.textContent = '🌙 Night mode';
    btn.setAttribute('aria-pressed', 'false');
  } else {
    btn.textContent = '☀ Day mode';
    btn.setAttribute('aria-pressed', 'true');
  }
}

// ── File list ─────────────────────────────────────────────────────────────────

async function loadFiles() {
  const files = await apiFetch(API('/api/files')).catch(() => []);
  renderFileList(files);
}

function renderFileList(files) {
  const el = document.getElementById('file-list');
  const deleteAllBtn = document.getElementById('btn-delete-all-files');
  if (deleteAllBtn) deleteAllBtn.hidden = files.length === 0;
  if (!files.length) {
    el.innerHTML = '<p class="empty-hint">No files uploaded yet.</p>';
    return;
  }
  el.innerHTML = '';
  files.forEach(f => {
    const item = document.createElement('div');
    item.className = 'file-item' + (currentFile?.name === f.name ? ' active' : '');
    item.dataset.name = f.name;
    item.innerHTML = `
      <span class="file-name" title="${f.name}">${f.name}</span>
      <span class="file-dur">${fmtSec(f.duration_sec)}</span>
      ${f.has_segments ? '<span class="file-badge">split</span>' : ''}
      <button class="btn btn-sm btn-danger del-btn" title="Delete">✕</button>
    `;
    item.querySelector('.del-btn').addEventListener('click', e => {
      e.stopPropagation();
      deleteFile(f.name);
    });
    item.addEventListener('click', () => openFile(f));
    el.appendChild(item);
  });
}

async function deleteFile(name) {
  if (!confirm(`Delete ${name} and all its segments?`)) return;
  await apiFetch(API(`/api/files/${encodeURIComponent(name)}`), { method: 'DELETE' });
  if (currentFile?.name === name) closeWorkPanel();
  toast('Deleted', 'success');
  loadFiles();
}

async function deleteAllFiles() {
  if (!confirm('Delete ALL uploaded recordings and their segments?\n\nThis cannot be undone.')) return;
  await apiFetch(API('/api/files'), { method: 'DELETE' });
  closeWorkPanel();
  toast('All files deleted', 'success');
  loadFiles();
}

// ── Upload ────────────────────────────────────────────────────────────────────

function setupUpload() {
  const zone  = document.getElementById('drop-zone');
  const input = document.getElementById('file-input');

  zone.addEventListener('dragover', e => { e.preventDefault(); zone.classList.add('hover'); });
  zone.addEventListener('dragleave', () => zone.classList.remove('hover'));
  zone.addEventListener('drop', e => {
    e.preventDefault();
    zone.classList.remove('hover');
    uploadFiles(e.dataTransfer.files);
  });
  input.addEventListener('change', () => uploadFiles(input.files));
}

async function uploadFiles(files) {
  if (!files.length) return;
  const errEl = document.getElementById('upload-errors');
  const statusEl = document.getElementById('upload-status');

  const CHUNK_SIZE = 512 * 1024;

  async function sha256Hex(file) {
    const buf = await file.arrayBuffer();
    const digest = await crypto.subtle.digest('SHA-256', buf);
    return Array.from(new Uint8Array(digest)).map((b) => b.toString(16).padStart(2, '0')).join('');
  }

  async function uploadOneFileChunked(file) {
    if (!file.name.toLowerCase().endsWith('.wav') && !file.name.toLowerCase().endsWith('.mp3')) {
      throw new Error(`${file.name}: only WAV or MP3 files are accepted`);
    }

    const uploadId = `${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
    const totalChunks = Math.ceil(file.size / CHUNK_SIZE);
    const fileSha256 = await sha256Hex(file);

    for (let i = 0; i < totalChunks; i += 1) {
      const start = i * CHUNK_SIZE;
      const end = Math.min(start + CHUNK_SIZE, file.size);
      const chunkBlob = file.slice(start, end);

      const fd = new FormData();
      fd.append('upload_id', uploadId);
      fd.append('file_name', file.name);
      fd.append('chunk_index', String(i));
      fd.append('total_chunks', String(totalChunks));
      fd.append('total_size', String(file.size));
      fd.append('file_sha256', fileSha256);
      fd.append('chunk', chunkBlob, `${file.name}.part`);

      const chunkRes = await fetch(API('/api/upload-chunk'), { method: 'POST', body: fd });
      if (!chunkRes.ok) {
        const msg = await chunkRes.text().catch(() => `Chunk ${i + 1} failed`);
        throw new Error(msg || `Chunk ${i + 1} failed`);
      }

      statusEl.textContent = `Uploading ${file.name}: chunk ${i + 1}/${totalChunks}`;
      statusEl.classList.remove('hidden');
    }

    const finalizeRes = await fetch(API('/api/upload-complete'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ upload_id: uploadId, file_name: file.name }),
    });
    if (!finalizeRes.ok) {
      const msg = await finalizeRes.text().catch(() => 'Upload finalize failed');
      throw new Error(msg || 'Upload finalize failed');
    }

    return finalizeRes.json();
  }

  try {
    const uploadedNames = [];
    errEl.classList.add('hidden');
    statusEl.classList.remove('hidden');

    for (const file of Array.from(files)) {
      statusEl.textContent = `Preparing ${file.name}...`;
      await uploadOneFileChunked(file);
      uploadedNames.push(file.name);
    }

    statusEl.classList.add('hidden');
    if (uploadedNames.length) {
      toast(`Uploaded ${uploadedNames.length} file(s)`, 'success');
      loadFiles();
    }
  } catch (err) {
    errEl.textContent = err.message;
    errEl.classList.remove('hidden');
    statusEl.classList.add('hidden');
  }
}

// ── Open / close file ─────────────────────────────────────────────────────────

async function openFile(f) {
  currentFile = f;
  document.getElementById('work-title').textContent = f.name;
  document.getElementById('work-panel').classList.remove('hidden');

  // Re-highlight file list
  document.querySelectorAll('.file-item').forEach(el => {
    el.classList.toggle('active', el.dataset.name === f.name);
  });

  // Destroy previous source waveform
  if (sourceWS) { sourceWS.destroy(); sourceWS = null; }
  destroyAllSegmentWS();

  // Create source WaveSurfer
  await buildSourceWS(f);

  // If already split, show segments
  const segments = await apiFetch(API(`/api/segments/${encodeURIComponent(f.name)}`)).catch(() => []);
  if (segments.length) {
    renderSegments(segments);
  } else {
    document.getElementById('segments-area').classList.add('hidden');
    document.getElementById('segment-list').innerHTML = '';
  }
}

function closeWorkPanel() {
  currentFile = null;
  if (sourceWS) { sourceWS.destroy(); sourceWS = null; }
  destroyAllSegmentWS();
  updateSourcePlayButton();
  document.getElementById('work-panel').classList.add('hidden');
}

// ── Source WaveSurfer ─────────────────────────────────────────────────────────

async function buildSourceWS(f) {
  const TimelinePlugin  = WaveSurfer.Timeline  ?? window.WaveSurferTimeline  ?? (WaveSurfer.default?.Timeline);
  const RegionsPlugin   = WaveSurfer.Regions   ?? window.WaveSurferRegions   ?? (WaveSurfer.default?.Regions);

  const plugins = [];
  if (TimelinePlugin) plugins.push(TimelinePlugin.create({ container: '#source-timeline' }));

  sourceWS = WaveSurfer.create({
    container: '#source-waveform',
    waveColor: getComputedStyle(document.documentElement).getPropertyValue('--wave-color').trim() || '#3b82f6',
    progressColor: getComputedStyle(document.documentElement).getPropertyValue('--progress-color').trim() || '#22d3ee',
    height: 100,
    plugins,
    url: API(`/api/audio/source/${encodeURIComponent(f.name)}`),
  });

  sourceWS.on('timeupdate', (t) => {
    document.getElementById('source-time').textContent =
      `${fmtSec(t)} / ${fmtSec(sourceWS.getDuration() || f.duration_sec)}`;
  });

  sourceWS.on('interaction', (t) => {
    // A click on the source waveform sets a split-at cursor but doesn't split immediately
    document.getElementById('source-time').textContent =
      `${fmtSec(t)} / ${fmtSec(sourceWS.getDuration() || f.duration_sec)}`;
  });

  sourceWS.on('finish', () => {
    if (typeof sourceWS.setTime === 'function') {
      sourceWS.setTime(0);
    } else if (typeof sourceWS.seekTo === 'function') {
      sourceWS.seekTo(0);
    }
    updateSourcePlayButton();
  });

  sourceWS.on('play', updateSourcePlayButton);
  sourceWS.on('pause', updateSourcePlayButton);
  updateSourcePlayButton();
}

// ── Auto-split ────────────────────────────────────────────────────────────────

function setupSourceControls() {
  document.getElementById('btn-play-source').addEventListener('click', toggleSourcePlayback);
  document.getElementById('btn-auto-split').addEventListener('click', autoSplit);
  document.getElementById('btn-close-work').addEventListener('click', closeWorkPanel);
}

async function autoSplit() {
  if (!currentFile) return;
  const btn = document.getElementById('btn-auto-split');
  btn.disabled = true;
  btn.textContent = '⏳ Splitting…';
  try {
    const result = await apiFetch(API(`/api/split/${encodeURIComponent(currentFile.name)}`), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        min_silence_ms: Number(document.getElementById('param-silence-ms').value),
        silence_threshold_db: Number(document.getElementById('param-threshold-db').value),
      }),
    });
    toast(`Split into ${result.segments.length} segment(s)`, 'success');
    renderSegments(result.segments);
    loadFiles();           // refresh badge
  } catch (err) {
    toast(err.message, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = '⚡ Auto-Split';
  }
}

// ── Segments rendering ────────────────────────────────────────────────────────

function destroyAllSegmentWS() {
  Object.values(segmentWSMap).forEach(ws => { try { ws.destroy(); } catch (_) {} });
  segmentWSMap = {};
  selectedSegs.clear();
  updateSegmentActionButtons();
}

function renderSegments(segments) {
  selectedSegs.clear();
  updateSegmentActionButtons();

  const area = document.getElementById('segments-area');
  const list = document.getElementById('segment-list');

  destroyAllSegmentWS();
  list.innerHTML = '';
  area.classList.remove('hidden');

  segments.forEach(seg => {
    const row = buildSegmentRow(seg);
    list.appendChild(row);
  });

  updateSegmentActionButtons();
}

function buildSegmentRow(seg) {
  const row = document.createElement('div');
  row.className = 'seg-row';
  row.id = `seg-${seg.name}`;

  row.innerHTML = `
    <div class="seg-header">
      <input type="checkbox" data-seg="${seg.name}" title="Select for merge" />
      <span class="seg-name" title="${seg.name}">${seg.name}</span>
      <span class="seg-dur">${fmtSec(seg.duration_sec)}</span>
      <button class="btn btn-sm btn-danger btn-del-seg" data-seg="${seg.name}" title="Delete segment">✕</button>
    </div>
    <textarea class="seg-transcript hidden" rows="3" spellcheck="true" placeholder="Transcription will appear here"></textarea>
    <span class="seg-transcript-saved hidden">✓ Saved</span>
    <div class="seg-waveform-wrap" style="height:70px"></div>
    <div class="seg-controls">
      <button class="btn btn-sm btn-primary btn-play-seg" data-seg="${seg.name}">▶ Play</button>
      <span class="seg-time-display">0:00.00 / ${fmtSec(seg.duration_sec)}</span>
      <div class="seg-split-row">
        <label>Split at</label>
        <input type="number" min="0" step="0.01" placeholder="sec" class="seg-split-input" data-seg="${seg.name}" />
        <button class="btn btn-sm btn-secondary btn-resplit" data-seg="${seg.name}">✂ Split here</button>
        ${ENABLE_TRANSCRIPTION ? `<button class="btn btn-sm btn-transcribe" data-seg="${seg.name}">🗣 Transcribe</button>` : ''}
      </div>
    </div>
  `;

  // Checkbox selection
  row.querySelector('input[type=checkbox]').addEventListener('change', e => {
    if (e.target.checked) selectedSegs.add(seg.name);
    else selectedSegs.delete(seg.name);
    row.classList.toggle('selected', e.target.checked);
    updateSegmentActionButtons();
  });

  // Delete segment
  row.querySelector('.btn-del-seg').addEventListener('click', async () => {
    if (!currentFile) return;
    const sourceStem = currentFile.name.replace(/\.wav$/i, '');
    await apiFetch(API(`/api/segments/${encodeURIComponent(sourceStem)}/${encodeURIComponent(seg.name)}`),
      { method: 'DELETE' });
    row.remove();
    destroySegmentWS(seg.name);
    toast('Segment deleted');
  });

  // Re-split button
  row.querySelector('.btn-resplit').addEventListener('click', async () => {
    if (!currentFile) return;
    const input = row.querySelector('.seg-split-input');
    const splitAt = parseFloat(input.value);
    if (isNaN(splitAt) || splitAt <= 0) { toast('Enter a valid time in seconds', 'error'); return; }
    const sourceStem = currentFile.name.replace(/\.wav$/i, '');
    try {
      const result = await apiFetch(API('/api/resplit'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ source_stem: sourceStem, segment_name: seg.name, split_at_sec: splitAt }),
      });
      toast(`Split into ${result.created.join(', ')}`, 'success');
      refreshSegments();
    } catch (err) {
      toast(err.message, 'error');
    }
  });

  const transcriptInput = row.querySelector('.seg-transcript');
  const sourceStem = currentFile ? currentFile.name.replace(/\.wav$/i, '') : '';
  const tKey = transcriptKey(sourceStem, seg.name);

  // Preload persisted transcription from backend segment list.
  if (seg.transcription) {
    transcriptCache[tKey] = seg.transcription;
    transcriptInput.value = seg.transcription;
    transcriptInput.classList.remove('hidden');
  }

  // Persist manual edits on blur.
  transcriptInput.addEventListener('blur', async () => {
    if (!currentFile) return;
    const latestSourceStem = currentFile.name.replace(/\.wav$/i, '');
    const latestKey = transcriptKey(latestSourceStem, seg.name);
    const text = transcriptInput.value || '';
    if ((transcriptCache[latestKey] || '') === text) return;

    try {
      await apiFetch(API('/api/transcription'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          source_stem: latestSourceStem,
          segment_name: seg.name,
          text,
        }),
      });
      transcriptCache[latestKey] = text;
      const savedLabel = transcriptInput.nextElementSibling;
      if (savedLabel && savedLabel.classList.contains('seg-transcript-saved')) {
        savedLabel.classList.remove('hidden');
        setTimeout(() => savedLabel.classList.add('hidden'), 2000);
      }
    } catch (err) {
      toast(`Failed to save transcription: ${err.message}`, 'error');
    }
  });

  // Transcribe button (only present when ENABLE_TRANSCRIPTION=true)
  if (ENABLE_TRANSCRIPTION) {
    row.querySelector('.btn-transcribe').addEventListener('click', async () => {
      if (!currentFile) return;
      const btn = row.querySelector('.btn-transcribe');
      const liveSourceStem = currentFile.name.replace(/\.wav$/i, '');
      const liveKey = transcriptKey(liveSourceStem, seg.name);

      // If already transcribed, toggle editor visibility.
      if (transcriptCache[liveKey]) {
        transcriptInput.classList.toggle('hidden');
        return;
      }

      transcriptInput.classList.remove('hidden');
      transcriptInput.value = 'Transcribing...';
      transcriptInput.readOnly = true;
      btn.textContent = '⏳ Loading…';
      btn.disabled = true;

      try {
        const url = API(`/api/transcribe/${encodeURIComponent(liveSourceStem)}/${encodeURIComponent(seg.name)}`);
        let text = '';
        let loaded = false;

        for (let attempt = 1; attempt <= 20; attempt++) {
          const res = await fetch(url);
          if (res.status === 202) {
            btn.textContent = '⏳ Loading model…';
            await waitMs(2000);
            continue;
          }

          const data = await res.json().catch(() => ({}));
          if (!res.ok || data.error) {
            throw new Error(data.error || `HTTP ${res.status}`);
          }

          text = data.text || '(no speech detected)';
          loaded = true;
          break;
        }

        if (!loaded) {
          throw new Error('Model took too long to load. Try again later.');
        }

        transcriptCache[liveKey] = text;
        transcriptInput.value = text;
      } catch (err) {
        transcriptInput.value = `⚠ ${err.message}`;
      } finally {
        transcriptInput.readOnly = false;
        transcriptInput.classList.remove('hidden');
        btn.textContent = '🗣 Transcribe';
        btn.disabled = false;
      }
    });
  }

  // Play button
  row.querySelector('.btn-play-seg').addEventListener('click', () => {
    const ws = segmentWSMap[seg.name];
    if (!ws) return;
    ws.playPause();
    row.querySelector('.btn-play-seg').textContent = ws.isPlaying() ? '⏸ Pause' : '▶ Play';
  });

  const waveContainer = row.querySelector('.seg-waveform-wrap');
  const timeDisplay = row.querySelector('.seg-time-display');

  // Build WaveSurfer for segment (deferred so DOM is ready)
  setTimeout(() => buildSegmentWS(seg, waveContainer, timeDisplay, row), 50);

  return row;
}

function buildSegmentWS(seg, containerEl, timeEl, row) {
  if (!currentFile) return;
  if (!containerEl) return;
  const sourceStem = currentFile.name.replace(/\.wav$/i, '');

  const ws = WaveSurfer.create({
    container: containerEl,
    waveColor: '#3b82f6',
    progressColor: '#22d3ee',
    height: 70,
    url: API(`/api/audio/segment/${encodeURIComponent(sourceStem)}/${encodeURIComponent(seg.name)}`),
  });

  ws.on('timeupdate', t => {
    const dur = ws.getDuration() || seg.duration_sec;
    if (timeEl) {
      timeEl.textContent = `${fmtSec(t)} / ${fmtSec(dur)}`;
    }
    // Auto-update seg-split-input as cursor moves (only if user hasn't typed)
    const inp = row.querySelector('.seg-split-input');
    if (inp && document.activeElement !== inp) {
      inp.value = t.toFixed(2);
    }
  });

  ws.on('finish', () => {
    row.querySelector('.btn-play-seg').textContent = '▶ Play';
  });

  ws.on('interaction', t => {
    const inp = row.querySelector('.seg-split-input');
    if (inp) inp.value = t.toFixed(2);
  });

  segmentWSMap[seg.name] = ws;
}

function destroySegmentWS(name) {
  const ws = segmentWSMap[name];
  if (ws) { try { ws.destroy(); } catch (_) {} delete segmentWSMap[name]; }
  selectedSegs.delete(name);
  updateSegmentActionButtons();
}

// ── Rebuild ───────────────────────────────────────────────────────────────────

function setupSegmentToolbar() {
  const selectAll = document.getElementById('chk-select-all');
  if (selectAll) {
    selectAll.addEventListener('change', (e) => {
      toggleSelectAllSegments(Boolean(e.target.checked));
    });
  }

  const btnDownload = document.getElementById('btn-download-selected');
  if (btnDownload) {
    btnDownload.addEventListener('click', downloadSelected);
  }

  const btnDeleteAll = document.getElementById('btn-delete-all');
  if (btnDeleteAll) {
    btnDeleteAll.addEventListener('click', deleteAllSegments);
  }

  document.getElementById('btn-rebuild').addEventListener('click', rebuildSelected);

  const btnRenumber = document.getElementById('btn-renumber');
  if (btnRenumber) {
    btnRenumber.addEventListener('click', renumberSegments);
  }
}

function updateSegmentActionButtons() {
  const totalCheckboxes = document.querySelectorAll('#segment-list .seg-header input[type=checkbox]');
  const checkedCheckboxes = document.querySelectorAll('#segment-list .seg-header input[type=checkbox]:checked');

  const btnRebuild = document.getElementById('btn-rebuild');
  const btnDownload = document.getElementById('btn-download-selected');
  const btnDeleteAll = document.getElementById('btn-delete-all');
  const btnRenumber = document.getElementById('btn-renumber');
  const chkSelectAll = document.getElementById('chk-select-all');

  if (btnRebuild) btnRebuild.disabled = selectedSegs.size < 2;
  if (btnDownload) btnDownload.disabled = selectedSegs.size < 1;
  if (btnRenumber) btnRenumber.disabled = totalCheckboxes.length < 1;

  if (chkSelectAll) {
    const total = totalCheckboxes.length;
    const checked = checkedCheckboxes.length;
    chkSelectAll.indeterminate = checked > 0 && checked < total;
    chkSelectAll.checked = total > 0 && checked === total;

    if (btnDeleteAll) {
      btnDeleteAll.disabled = !(chkSelectAll.checked && total > 0);
    }
  } else if (btnDeleteAll) {
    btnDeleteAll.disabled = true;
  }
}

function toggleSelectAllSegments(shouldSelect) {
  const checkboxes = document.querySelectorAll('#segment-list .seg-header input[type=checkbox]');
  selectedSegs.clear();

  checkboxes.forEach((cb) => {
    cb.checked = shouldSelect;
    const segName = cb.dataset.seg;
    const row = cb.closest('.seg-row');
    if (shouldSelect) {
      selectedSegs.add(segName);
      if (row) row.classList.add('selected');
    } else if (row) {
      row.classList.remove('selected');
    }
  });

  updateSegmentActionButtons();
}

function parseDownloadFilename(contentDisposition, fallback) {
  if (!contentDisposition) return fallback;
  const utf8Match = contentDisposition.match(/filename\*=UTF-8''([^;]+)/i);
  if (utf8Match && utf8Match[1]) return decodeURIComponent(utf8Match[1]);
  const simpleMatch = contentDisposition.match(/filename="?([^";]+)"?/i);
  if (simpleMatch && simpleMatch[1]) return simpleMatch[1];
  return fallback;
}

async function downloadSelected() {
  if (!currentFile || selectedSegs.size < 1) return;

  const sourceStem = currentFile.name.replace(/\.wav$/i, '');
  const segsArr = [...selectedSegs];

  const response = await fetch(API('/api/download-selected'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ source_stem: sourceStem, segments: segsArr }),
  });

  if (!response.ok) {
    const errText = await response.text().catch(() => 'Download failed');
    toast(errText || 'Download failed', 'error');
    return;
  }

  const blob = await response.blob();
  const fallbackName = segsArr.length === 1 ? segsArr[0] : `${sourceStem}_selected_segments.zip`;
  const filename = parseDownloadFilename(response.headers.get('content-disposition'), fallbackName);

  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);

  toast(`Downloaded ${selectedSegs.size} segment(s)`, 'success');
}

async function deleteAllSegments() {
  if (!currentFile) return;

  const checkboxes = Array.from(document.querySelectorAll('#segment-list .seg-header input[type=checkbox]'));
  if (!checkboxes.length) return;

  const sourceStem = currentFile.name.replace(/\.wav$/i, '');
  const confirmed = window.confirm(`Delete all ${checkboxes.length} split segments for ${currentFile.name}? This cannot be undone.`);
  if (!confirmed) return;

  const btnDeleteAll = document.getElementById('btn-delete-all');
  if (btnDeleteAll) btnDeleteAll.disabled = true;

  try {
    for (const cb of checkboxes) {
      const segName = cb.dataset.seg;
      if (!segName) continue;
      await apiFetch(API(`/api/segments/${encodeURIComponent(sourceStem)}/${encodeURIComponent(segName)}`),
        { method: 'DELETE' });
      const row = cb.closest('.seg-row');
      if (row) row.remove();
      destroySegmentWS(segName);
    }

    selectedSegs.clear();
    updateSegmentActionButtons();
    toast('All segments deleted', 'success');
  } catch (err) {
    toast(`Failed to delete all segments: ${err.message}`, 'error');
    refreshSegments();
  }
}

async function rebuildSelected() {
  if (!currentFile || selectedSegs.size < 2) return;
  const sourceStem = currentFile.name.replace(/\.wav$/i, '');
  const segsArr = [...selectedSegs];
  try {
    const result = await apiFetch(API('/api/rebuild'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ source_stem: sourceStem, segments: segsArr }),
    });
    toast(`Merged → ${result.merged}`, 'success');
    refreshSegments();
  } catch (err) {
    toast(err.message, 'error');
  }
}

async function renumberSegments() {
  if (!currentFile) return;
  const sourceName = currentFile.name;

  try {
    const result = await apiFetch(API(`/api/renumber/${encodeURIComponent(sourceName)}`), {
      method: 'POST',
    });
    const count = Number(result.renamed || 0);
    toast(`Renumbered ${count} segment(s)`, 'success');
    refreshSegments();
  } catch (err) {
    toast(err.message, 'error');
  }
}

// ── Refresh segment list ──────────────────────────────────────────────────────

async function refreshSegments() {
  if (!currentFile) return;
  destroyAllSegmentWS();
  const segments = await apiFetch(API(`/api/segments/${encodeURIComponent(currentFile.name)}`)).catch(() => []);
  renderSegments(segments);
}
