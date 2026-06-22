const viewer = document.getElementById('viewer');
const viewerImg = document.getElementById('viewer-img');
const viewerClose = document.getElementById('viewer-close');
const viewerDownload = document.getElementById('viewer-download');

const viewerState = {
  scale: 1,
  offsetX: 0,
  offsetY: 0,
  dragging: false,
  dragStartX: 0,
  dragStartY: 0,
  startOffsetX: 0,
  startOffsetY: 0,
};

function applyViewerTransform() {
  if (!viewerImg) return;
  viewerImg.style.transform = `translate(${viewerState.offsetX}px, ${viewerState.offsetY}px) scale(${viewerState.scale})`;
  viewerImg.classList.toggle('is-zoomed', viewerState.scale > 1);
}

function resetViewerZoom() {
  viewerState.scale = 1;
  viewerState.offsetX = 0;
  viewerState.offsetY = 0;
  viewerState.dragging = false;
  applyViewerTransform();
}

function openViewer(src, downloadName = 'diff.png') {
  if (!viewer || !viewerImg || !viewerDownload) return;
  resetViewerZoom();
  viewerImg.src = src;
  viewerDownload.href = src;
  viewerDownload.download = downloadName;
  viewer.classList.remove('hidden');
  viewer.setAttribute('aria-hidden', 'false');
}

function closeViewer() {
  if (!viewer || !viewerImg) return;
  viewer.classList.add('hidden');
  viewer.setAttribute('aria-hidden', 'true');
  resetViewerZoom();
  viewerImg.src = '';
}

document.querySelectorAll('.preview-btn').forEach((btn) => {
  btn.addEventListener('click', () => {
    const src = btn.dataset.src;
    if (!src) return;
    const downloadName = btn.closest('.diff-wrap')?.querySelector('.download')?.download || 'diff.png';
    openViewer(src, downloadName);
  });
});

viewerClose?.addEventListener('click', closeViewer);
viewer?.addEventListener('click', (e) => {
  if (e.target === viewer) closeViewer();
});
viewer?.addEventListener('wheel', (e) => {
  if (viewer.classList.contains('hidden')) return;
  e.preventDefault();
  const direction = e.deltaY < 0 ? 1 : -1;
  viewerState.scale = Math.min(4, Math.max(1, viewerState.scale + direction * 0.25));
  if (viewerState.scale === 1) {
    viewerState.offsetX = 0;
    viewerState.offsetY = 0;
  }
  applyViewerTransform();
}, { passive: false });
viewerImg?.addEventListener('pointerdown', (e) => {
  if (viewerState.scale <= 1) return;
  e.preventDefault();
  viewerState.dragging = true;
  viewerState.dragStartX = e.clientX;
  viewerState.dragStartY = e.clientY;
  viewerState.startOffsetX = viewerState.offsetX;
  viewerState.startOffsetY = viewerState.offsetY;
  viewerImg.setPointerCapture?.(e.pointerId);
  viewerImg.classList.add('is-dragging');
});
viewerImg?.addEventListener('pointermove', (e) => {
  if (!viewerState.dragging) return;
  viewerState.offsetX = viewerState.startOffsetX + e.clientX - viewerState.dragStartX;
  viewerState.offsetY = viewerState.startOffsetY + e.clientY - viewerState.dragStartY;
  applyViewerTransform();
});
const stopViewerDrag = (e) => {
  if (!viewerState.dragging) return;
  viewerState.dragging = false;
  viewerImg?.releasePointerCapture?.(e.pointerId);
  viewerImg?.classList.remove('is-dragging');
};
viewerImg?.addEventListener('pointerup', stopViewerDrag);
viewerImg?.addEventListener('pointercancel', stopViewerDrag);
viewerImg?.addEventListener('dragstart', (e) => e.preventDefault());
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && viewer && !viewer.classList.contains('hidden')) closeViewer();
});

const precisionInput = document.getElementById('precision');
const precisionValue = document.getElementById('precision-value');
if (precisionInput && precisionValue) {
  const syncPrecision = () => {
    precisionValue.value = precisionInput.value;
    precisionValue.textContent = precisionInput.value;
  };
  syncPrecision();
  precisionInput.addEventListener('input', syncPrecision);
}

const CACHE_DB = 'pdf-diff-highlighter-cache-v1';
const STORE_NAME = 'files';
const CACHE_LIMIT = 6;
const dbPromise = window.indexedDB
  ? new Promise((resolve, reject) => {
      const request = indexedDB.open(CACHE_DB, 1);
      request.onupgradeneeded = () => {
        const db = request.result;
        const store = db.createObjectStore(STORE_NAME, { keyPath: 'key' });
        store.createIndex('slot', 'slot', { unique: false });
        store.createIndex('updatedAt', 'updatedAt', { unique: false });
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    })
  : null;

function fileKey(slot, file) {
  return `${slot}:${file.name}:${file.size}:${file.lastModified}`;
}

async function withStore(mode, callback) {
  if (!dbPromise) return null;
  const db = await dbPromise;
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE_NAME, mode);
    const store = tx.objectStore(STORE_NAME);
    const result = callback(store);
    tx.oncomplete = () => resolve(result);
    tx.onerror = () => reject(tx.error);
    tx.onabort = () => reject(tx.error);
  });
}

async function saveFileToCache(slot, file) {
  const entry = {
    key: fileKey(slot, file),
    slot,
    name: file.name,
    type: file.type,
    size: file.size,
    lastModified: file.lastModified,
    updatedAt: Date.now(),
    blob: file,
  };
  await withStore('readwrite', (store) => store.put(entry));
}

async function getCachedFiles(slot) {
  if (!dbPromise) return [];
  const db = await dbPromise;
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE_NAME, 'readonly');
    const store = tx.objectStore(STORE_NAME);
    const index = store.index('slot');
    const request = index.getAll(slot);
    request.onsuccess = () => {
      const items = (request.result || [])
        .sort((a, b) => b.updatedAt - a.updatedAt)
        .slice(0, CACHE_LIMIT)
        .map((item) => ({
          key: item.key,
          name: item.name,
          type: item.type,
          size: item.size,
          lastModified: item.lastModified,
          updatedAt: item.updatedAt,
        }));
      resolve(items);
    };
    request.onerror = () => reject(request.error);
  });
}

async function loadCachedFile(key) {
  if (!dbPromise) return null;
  const db = await dbPromise;
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE_NAME, 'readonly');
    const store = tx.objectStore(STORE_NAME);
    const request = store.get(key);
    request.onsuccess = () => resolve(request.result || null);
    request.onerror = () => reject(request.error);
  });
}

function setInputFile(input, file) {
  const dt = new DataTransfer();
  dt.items.add(file);
  input.files = dt.files;
  input.dispatchEvent(new Event('change', { bubbles: true }));
}

function fileLabel(file) {
  if (!file) return 'Файл не выбран';
  const mb = (file.size / (1024 * 1024)).toFixed(1);
  return `${file.name} · ${mb} MB`;
}

function updateDropzoneLabel(input) {
  if (!input) return;
  const slot = input.id;
  const label = document.querySelector(`[data-file-name="${slot}"]`);
  if (!label) return;
  label.textContent = input.files?.[0] ? fileLabel(input.files[0]) : 'Файл не выбран';
}

async function renderCache(slot) {
  const list = document.querySelector(`[data-cache-list="${slot}"]`);
  const input = document.getElementById(slot);
  if (!list || !input) return;
  list.innerHTML = '';
  try {
    const items = await getCachedFiles(slot);
    if (!items.length) {
      const empty = document.createElement('div');
      empty.className = 'muted';
      empty.textContent = 'Кэш пуст';
      list.appendChild(empty);
      return;
    }
    for (const item of items) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'cache-btn';
      btn.textContent = `${item.name} (${(item.size / (1024 * 1024)).toFixed(1)} MB)`;
      btn.title = 'Загрузить из кэша';
      btn.addEventListener('click', async () => {
        try {
          const cached = await loadCachedFile(item.key);
          if (!cached) return;
          const file = cached.blob instanceof File
            ? cached.blob
            : new File([cached.blob], cached.name, { type: cached.type, lastModified: cached.lastModified });
          setInputFile(input, file);
          await saveFileToCache(slot, file);
          await renderCache(slot);
        } catch {
          // cache is best-effort only
        }
      });
      list.appendChild(btn);
    }
  } catch {
    const fallback = document.createElement('div');
    fallback.className = 'muted';
    fallback.textContent = 'Кэш недоступен';
    list.appendChild(fallback);
  }
}

function wireDropzone(slot) {
  const input = document.getElementById(slot);
  const dropzone = document.querySelector(`[data-slot="${slot}"]`);
  if (!input || !dropzone) return;

  input.addEventListener('change', async () => {
    updateDropzoneLabel(input);
    const file = input.files?.[0];
    if (!file) return;
    try {
      await saveFileToCache(slot, file);
      await renderCache(slot);
    } catch {
      // cache is best-effort only
    }
  });

  const prevent = (e) => {
    e.preventDefault();
    e.stopPropagation();
  };

  ['dragenter', 'dragover'].forEach((eventName) => {
    dropzone.addEventListener(eventName, (e) => {
      prevent(e);
      dropzone.classList.add('dragover');
    });
  });
  ['dragleave', 'dragend'].forEach((eventName) => {
    dropzone.addEventListener(eventName, (e) => {
      prevent(e);
      dropzone.classList.remove('dragover');
    });
  });
  dropzone.addEventListener('drop', async (e) => {
    prevent(e);
    dropzone.classList.remove('dragover');
    const file = e.dataTransfer?.files?.[0];
    if (!file) return;
    setInputFile(input, file);
    await saveFileToCache(slot, file);
    await renderCache(slot);
  });
}

['pdf1', 'pdf2'].forEach((slot) => {
  wireDropzone(slot);
  updateDropzoneLabel(document.getElementById(slot));
  void renderCache(slot);
});
