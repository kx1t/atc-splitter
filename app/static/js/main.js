/* ATC Splitter — main.js
 * Uses WaveSurfer.js v7 (loaded from CDN in index.html)
 */

// ── Utility ──────────────────────────────────────────────────────────────────

const API = (path) => path;

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

let toastTimer;
function toast(msg, kind = '') {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = `toast show${kind ? ' ' + kind : ''}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), 3000);
}

// ── State ─────────────────────────────────────────────────────────────────────

let currentFile = null;          // { name, duration_sec, … }
let sourceWS    = null;          // WaveSurfer instance for source
let segmentWSMap = {};           // seg_name → WaveSurfer instance
let selectedSegs = new Set();    // checked segment names

// ── Bootstrap ─────────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
  loadFiles();
  setupUpload();
  setupSourceControls();
  setupRebuildButton();
});

// ── File list ─────────────────────────────────────────────────────────────────

async function loadFiles() {
  const files = await apiFetch(API('/api/files')).catch(() => []);
  renderFileList(files);
}

function renderFileList(files) {
  const el = document.getElementById('file-list');
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
  const fd = new FormData();
  Array.from(files).forEach(f => fd.append('files', f));
  const errEl = document.getElementById('upload-errors');

  try {
    const result = await apiFetch(API('/api/upload'), { method: 'POST', body: fd });
    if (result.errors?.length) {
      errEl.textContent = result.errors.join('; ');
      errEl.classList.remove('hidden');
    } else {
      errEl.classList.add('hidden');
    }
    if (result.saved?.length) {
      toast(`Uploaded ${result.saved.length} file(s)`, 'success');
      loadFiles();
    }
  } catch (err) {
    errEl.textContent = err.message;
    errEl.classList.remove('hidden');
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

  document.getElementById('btn-play-source').addEventListener('click', () => {
    sourceWS.playPause();
    document.getElementById('btn-play-source').textContent =
      sourceWS.isPlaying() ? '⏸ Pause source' : '▶ Play source';
  });
}

// ── Auto-split ────────────────────────────────────────────────────────────────

function setupSourceControls() {
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
  updateRebuildButton();
}

function renderSegments(segments) {
  selectedSegs.clear();
  updateRebuildButton();

  const area = document.getElementById('segments-area');
  const list = document.getElementById('segment-list');

  destroyAllSegmentWS();
  list.innerHTML = '';
  area.classList.remove('hidden');

  segments.forEach(seg => {
    const row = buildSegmentRow(seg);
    list.appendChild(row);
  });
}

function buildSegmentRow(seg) {
  const row = document.createElement('div');
  row.className = 'seg-row';
  row.id = `seg-${seg.name}`;

  row.innerHTML = `
    <div class="seg-header">
      <input type="checkbox" data-seg="${seg.name}" title="Select for rebuild" />
      <span class="seg-name" title="${seg.name}">${seg.name}</span>
      <span class="seg-dur">${fmtSec(seg.duration_sec)}</span>
      <button class="btn btn-sm btn-danger btn-del-seg" data-seg="${seg.name}" title="Delete segment">✕</button>
    </div>
    <div class="seg-waveform-wrap" style="height:70px"></div>
    <div class="seg-controls">
      <button class="btn btn-sm btn-primary btn-play-seg" data-seg="${seg.name}">▶ Play</button>
      <span class="seg-time-display">0:00.00 / ${fmtSec(seg.duration_sec)}</span>
      <div class="seg-split-row">
        <label>Split at</label>
        <input type="number" min="0" step="0.01" placeholder="sec" class="seg-split-input" data-seg="${seg.name}" />
        <button class="btn btn-sm btn-secondary btn-resplit" data-seg="${seg.name}">✂ Split here</button>
      </div>
    </div>
  `;

  // Checkbox selection
  row.querySelector('input[type=checkbox]').addEventListener('change', e => {
    if (e.target.checked) selectedSegs.add(seg.name);
    else selectedSegs.delete(seg.name);
    row.classList.toggle('selected', e.target.checked);
    updateRebuildButton();
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

// Simple helper – strip directory, return stem
const Path = (name) => ({
  stem: name.replace(/\.[^.]+$/, ''),
});

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
  updateRebuildButton();
}

// ── Rebuild ───────────────────────────────────────────────────────────────────

function setupRebuildButton() {
  document.getElementById('btn-rebuild').addEventListener('click', rebuildSelected);
}

function updateRebuildButton() {
  document.getElementById('btn-rebuild').disabled = selectedSegs.size < 2;
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
    toast(`Rebuilt → ${result.rebuilt}`, 'success');
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
