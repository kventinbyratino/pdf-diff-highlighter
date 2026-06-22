const viewer = document.getElementById('viewer');
const viewerImg = document.getElementById('viewer-img');
const viewerClose = document.getElementById('viewer-close');
const viewerDownload = document.getElementById('viewer-download');
const imageStatus = document.getElementById('image-status');

const cropModal = document.getElementById('crop-modal');
const cropStage = document.getElementById('crop-stage');
const cropImage = document.getElementById('crop-image');
const cropSelection = document.getElementById('crop-selection');
const cropClose = document.getElementById('crop-close');
const cropCancelButtons = document.querySelectorAll('[data-crop-close]');
const cropReset = document.getElementById('crop-reset');
const cropApply = document.getElementById('crop-apply');
const cropHint = document.getElementById('crop-hint');

const cacheDbName = 'pdf-diff-highlighter-cache';
const cacheStoreName = 'files';

const cropState = {
  input: null,
  file: null,
  objectUrl: '',
  selection: null,
  dragging: false,
  pointerId: null,
  startX: 0,
  startY: 0,
};

function setImageStatus(message, tone = 'muted') {
  if (!imageStatus) return;
  imageStatus.textContent = message;
  imageStatus.className = `status ${tone}`;
}

function updateUploadStatus(input) {
  const status = document.querySelector(`[data-upload-status="${input.id}"]`);
  if (!status) return;
  const label = input.id === 'pdf1' ? 'Чертеж 1' : input.id === 'pdf2' ? 'Чертеж 2' : input.id;
  const loaded = Boolean(input.files?.[0]);
  status.textContent = `${label} — статус: ${loaded ? 'загружен' : 'не загружен'}`;
  status.classList.toggle('slot-status-loaded', loaded);
  status.classList.toggle('slot-status-empty', !loaded);
}

function setPreviewSlot(slotId, file) {
  const image = document.querySelector(`[data-preview="${slotId}"]`);
  if (!image) return;

  const shell = image.closest('.slot-preview-shell');
  if (image.dataset.objectUrl) {
    URL.revokeObjectURL(image.dataset.objectUrl);
    delete image.dataset.objectUrl;
  }

  if (!file) {
    image.hidden = true;
    image.removeAttribute('src');
    if (shell) shell.classList.remove('has-preview');
    return;
  }

  const objectUrl = URL.createObjectURL(file);
  image.dataset.objectUrl = objectUrl;
  image.src = objectUrl;
  image.hidden = false;
  if (shell) shell.classList.add('has-preview');
}

function applyFileToInput(input, file) {
  if (!input || !file) return;
  const dt = new DataTransfer();
  dt.items.add(file);
  input.files = dt.files;
  input.dispatchEvent(new Event('change', { bubbles: true }));
}

function openCacheDb() {
  return new Promise((resolve, reject) => {
    if (!window.indexedDB) {
      reject(new Error('IndexedDB недоступен'));
      return;
    }
    const request = indexedDB.open(cacheDbName, 1);
    request.onupgradeneeded = () => {
      request.result.createObjectStore(cacheStoreName);
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error || new Error('Не удалось открыть кэш'));
  });
}

async function saveFileToCache(key, file) {
  const db = await openCacheDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(cacheStoreName, 'readwrite');
    tx.objectStore(cacheStoreName).put(file, key);
    tx.oncomplete = () => resolve(true);
    tx.onerror = () => reject(tx.error || new Error('Не удалось сохранить файл'));
  });
}

async function loadFileFromCache(key) {
  const db = await openCacheDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(cacheStoreName, 'readonly');
    const req = tx.objectStore(cacheStoreName).get(key);
    req.onsuccess = () => resolve(req.result || null);
    req.onerror = () => reject(req.error || new Error('Не удалось прочитать файл'));
  });
}

function closeViewer() {
  viewer?.classList.add('hidden');
  viewer?.setAttribute('aria-hidden', 'true');
  if (viewerImg) viewerImg.src = '';
}

function openViewer(src, downloadName = 'preview.png') {
  if (!viewer || !viewerImg || !viewerDownload) return;
  viewerImg.src = src;
  viewerDownload.href = src;
  viewerDownload.download = downloadName;
  viewer.classList.remove('hidden');
  viewer.setAttribute('aria-hidden', 'false');
}

function closeCropModal() {
  if (!cropModal) return;
  cropModal.classList.add('hidden');
  cropModal.setAttribute('aria-hidden', 'true');
  if (cropState.objectUrl) {
    URL.revokeObjectURL(cropState.objectUrl);
    cropState.objectUrl = '';
  }
  if (cropImage) cropImage.removeAttribute('src');
  cropState.input = null;
  cropState.file = null;
  cropState.selection = null;
  cropState.dragging = false;
  cropState.pointerId = null;
  cropSelection?.classList.add('hidden');
}

function clamp(value, min, max) {
  return Math.min(max, Math.max(min, value));
}

function normalizeSelection(selection) {
  if (!selection) return null;
  const x1 = Math.min(selection.x, selection.x + selection.w);
  const y1 = Math.min(selection.y, selection.y + selection.h);
  const x2 = Math.max(selection.x, selection.x + selection.w);
  const y2 = Math.max(selection.y, selection.y + selection.h);
  return {
    x: x1,
    y: y1,
    w: x2 - x1,
    h: y2 - y1,
  };
}

function renderSelection() {
  if (!cropSelection) return;
  const selection = normalizeSelection(cropState.selection);
  if (!selection || selection.w <= 1 || selection.h <= 1) {
    cropSelection.classList.add('hidden');
    return;
  }
  cropSelection.classList.remove('hidden');
  cropSelection.style.left = `${selection.x}px`;
  cropSelection.style.top = `${selection.y}px`;
  cropSelection.style.width = `${selection.w}px`;
  cropSelection.style.height = `${selection.h}px`;
}

function stagePoint(event) {
  if (!cropStage) return { x: 0, y: 0, width: 0, height: 0 };
  const rect = cropStage.getBoundingClientRect();
  return {
    x: clamp(event.clientX - rect.left, 0, rect.width),
    y: clamp(event.clientY - rect.top, 0, rect.height),
    width: rect.width,
    height: rect.height,
  };
}

function setFullSelection() {
  if (!cropStage) return;
  const rect = cropStage.getBoundingClientRect();
  cropState.selection = { x: 0, y: 0, w: rect.width, h: rect.height };
  renderSelection();
}

async function canvasToFile(canvas, filename) {
  const blob = await new Promise((resolve, reject) => {
    canvas.toBlob((result) => {
      if (!result) {
        reject(new Error('Не удалось сформировать изображение'));
        return;
      }
      resolve(result);
    }, 'image/png');
  });
  return new File([blob], filename, { type: 'image/png' });
}

async function captureBrowserShot() {
  if (!navigator.mediaDevices?.getDisplayMedia) {
    throw new Error('Браузер не поддерживает захват экрана');
  }

  const stream = await navigator.mediaDevices.getDisplayMedia({
    video: { cursor: 'always' },
    audio: false,
  });

  try {
    const video = document.createElement('video');
    video.srcObject = stream;
    video.playsInline = true;
    video.muted = true;
    await new Promise((resolve, reject) => {
      video.onloadedmetadata = () => resolve();
      video.onerror = () => reject(new Error('Не удалось подготовить видеопоток'));
    });
    await video.play();
    await new Promise((resolve) => requestAnimationFrame(resolve));

    const canvas = document.createElement('canvas');
    canvas.width = video.videoWidth || 1;
    canvas.height = video.videoHeight || 1;
    const ctx = canvas.getContext('2d');
    if (!ctx) {
      throw new Error('Canvas недоступен');
    }
    ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
    return canvasToFile(canvas, `browser-shot-${Date.now()}.png`);
  } finally {
    stream.getTracks().forEach((track) => track.stop());
  }
}

async function captureDesktopShot() {
  const response = await fetch('capture-screen?monitor=1');
  if (!response.ok) {
    const message = await response.text();
    throw new Error(message || 'Не удалось снять экран ОС');
  }
  const blob = await response.blob();
  return new File([blob], `desktop-shot-${Date.now()}.png`, { type: blob.type || 'image/png' });
}

function withBusyButton(button, label, callback) {
  const originalLabel = button.dataset.originalLabel || button.textContent || '';
  button.dataset.originalLabel = originalLabel;
  button.disabled = true;
  button.textContent = label;
  return Promise.resolve()
    .then(callback)
    .finally(() => {
      button.disabled = false;
      button.textContent = originalLabel;
    });
}

function bindDropzone(zone) {
  const input = zone.querySelector('input[type="file"]');
  if (!input) return;

  input.addEventListener('change', () => {
    updateUploadStatus(input);
    setPreviewSlot(input.id, input.files?.[0] || null);
  });

  zone.addEventListener('dragover', (event) => {
    event.preventDefault();
    zone.classList.add('dragover');
  });

  zone.addEventListener('dragleave', () => {
    zone.classList.remove('dragover');
  });

  zone.addEventListener('drop', (event) => {
    event.preventDefault();
    zone.classList.remove('dragover');
    const file = event.dataTransfer?.files?.[0];
    if (file) applyFileToInput(input, file);
  });
}

function bindCaptureButtons() {
  document.querySelectorAll('[data-capture-browser]').forEach((button) => {
    button.addEventListener('click', async () => {
      const slotId = button.dataset.captureBrowser;
      const input = slotId ? document.getElementById(slotId) : null;
      if (!input) return;
      try {
        await withBusyButton(button, 'Снимаю…', async () => {
          const file = await captureBrowserShot();
          applyFileToInput(input, file);
          setImageStatus(`Браузерный кадр сохранён в ${slotId}`, 'good');
        });
      } catch (error) {
        const message = error instanceof Error ? error.message : 'Не удалось снять экран браузера';
        setImageStatus(message, 'bad');
      }
    });
  });

  document.querySelectorAll('[data-capture-screen]').forEach((button) => {
    button.addEventListener('click', async () => {
      const slotId = button.dataset.captureScreen;
      const input = slotId ? document.getElementById(slotId) : null;
      if (!input) return;
      try {
        await withBusyButton(button, 'Снимаю…', async () => {
          const file = await captureDesktopShot();
          applyFileToInput(input, file);
          setImageStatus(`Скрин экрана ОС сохранён в ${slotId}`, 'good');
        });
      } catch (error) {
        const message = error instanceof Error ? error.message : 'Не удалось снять экран ОС';
        setImageStatus(message, 'bad');
      }
    });
  });
}

function bindCropButtons() {
  document.querySelectorAll('[data-crop]').forEach((button) => {
    button.addEventListener('click', () => {
      const slotId = button.dataset.crop;
      const input = slotId ? document.getElementById(slotId) : null;
      const file = input?.files?.[0];
      if (!input || !file) {
        setImageStatus('Сначала выберите или снимите изображение', 'bad');
        return;
      }

      if (!cropModal || !cropImage || !cropHint) return;
      cropState.input = input;
      cropState.file = file;
      cropState.selection = null;
      cropState.dragging = false;
      cropHint.textContent = 'Потяните мышью, чтобы выделить нужную область. Если ничего не выделять, сохранится весь кадр.';

      if (cropState.objectUrl) {
        URL.revokeObjectURL(cropState.objectUrl);
      }
      cropState.objectUrl = URL.createObjectURL(file);
      cropImage.src = cropState.objectUrl;
      cropModal.classList.remove('hidden');
      cropModal.setAttribute('aria-hidden', 'false');
    });
  });
}

function bindCropInteractions() {
  if (!cropStage) return;

  cropStage.addEventListener('pointerdown', (event) => {
    if (!cropState.input || !cropImage || cropImage.complete === false) return;
    event.preventDefault();
    cropState.dragging = true;
    cropState.pointerId = event.pointerId;
    const pt = stagePoint(event);
    cropState.startX = pt.x;
    cropState.startY = pt.y;
    cropState.selection = { x: pt.x, y: pt.y, w: 1, h: 1 };
    renderSelection();
    cropHint.textContent = 'Тяните рамку и отпустите мышь, чтобы зафиксировать область.';
  });

  window.addEventListener('pointermove', (event) => {
    if (!cropState.dragging || event.pointerId !== cropState.pointerId) return;
    const pt = stagePoint(event);
    cropState.selection = {
      x: cropState.startX,
      y: cropState.startY,
      w: pt.x - cropState.startX,
      h: pt.y - cropState.startY,
    };
    renderSelection();
  });

  window.addEventListener('pointerup', (event) => {
    if (!cropState.dragging || event.pointerId !== cropState.pointerId) return;
    cropState.dragging = false;
    cropState.pointerId = null;
    cropHint.textContent = 'Можно применить кроп или выделить другую область.';
  });
}

async function applyCrop() {
  if (!cropState.input || !cropImage || !cropStage) return;
  const normalized = normalizeSelection(cropState.selection);
  const stageRect = cropStage.getBoundingClientRect();
  const cropRect = normalized && normalized.w > 1 && normalized.h > 1
    ? normalized
    : { x: 0, y: 0, w: stageRect.width, h: stageRect.height };

  const scaleX = cropImage.naturalWidth / Math.max(1, stageRect.width);
  const scaleY = cropImage.naturalHeight / Math.max(1, stageRect.height);
  const sx = clamp(Math.max(0, Math.round(cropRect.x * scaleX)), 0, Math.max(0, cropImage.naturalWidth - 1));
  const sy = clamp(Math.max(0, Math.round(cropRect.y * scaleY)), 0, Math.max(0, cropImage.naturalHeight - 1));
  const sw = Math.max(1, Math.round(cropRect.w * scaleX));
  const sh = Math.max(1, Math.round(cropRect.h * scaleY));
  const safeW = Math.max(1, Math.min(sw, cropImage.naturalWidth - sx));
  const safeH = Math.max(1, Math.min(sh, cropImage.naturalHeight - sy));

  const canvas = document.createElement('canvas');
  canvas.width = safeW;
  canvas.height = safeH;
  const ctx = canvas.getContext('2d');
  if (!ctx) {
    setImageStatus('Canvas недоступен для кропа', 'bad');
    return;
  }

  ctx.drawImage(cropImage, sx, sy, canvas.width, canvas.height, 0, 0, canvas.width, canvas.height);
  const sourceName = cropState.file?.name || cropState.input.files?.[0]?.name || 'image.png';
  const fileName = sourceName.replace(/\.[^.]+$/, '') + '-crop.png';
  const blob = await new Promise((resolve, reject) => {
    canvas.toBlob((result) => {
      if (!result) {
        reject(new Error('Не удалось сохранить кроп'));
        return;
      }
      resolve(result);
    }, 'image/png');
  });

  applyFileToInput(cropState.input, new File([blob], fileName, { type: 'image/png' }));
  setImageStatus('Кадр вырезан и готов к сравнению', 'good');
  closeCropModal();
}

function bindPreviewThumbs() {
  document.querySelectorAll('.diff-thumb, .slot-preview').forEach((img) => {
    img.addEventListener('click', () => {
      if (!img.src) return;
      const downloadName = img.dataset.download || img.getAttribute('alt') || 'preview.png';
      openViewer(img.src, downloadName);
    });
  });
}

function bindViewer() {
  viewerClose?.addEventListener('click', closeViewer);
  viewer?.addEventListener('click', (event) => {
    if (event.target === viewer) closeViewer();
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && viewer && !viewer.classList.contains('hidden')) {
      closeViewer();
    }
    if (event.key === 'Escape' && cropModal && !cropModal.classList.contains('hidden')) {
      closeCropModal();
    }
  });
}

function bindCropModal() {
  cropClose?.addEventListener('click', closeCropModal);
  cropCancelButtons.forEach((button) => button.addEventListener('click', closeCropModal));
  cropReset?.addEventListener('click', () => {
    setFullSelection();
    if (cropHint) cropHint.textContent = 'Выделение сброшено на весь кадр. Вы можете тянуть новую область.';
  });
  cropApply?.addEventListener('click', () => {
    applyCrop().catch((error) => {
      const message = error instanceof Error ? error.message : 'Не удалось применить кроп';
      setImageStatus(message, 'bad');
    });
  });

  cropImage?.addEventListener('load', () => {
    if (!cropModal || cropModal.classList.contains('hidden')) return;
    setFullSelection();
  });
}

function bindPrecisionInputs() {
  document.querySelectorAll('.precision-input').forEach((input) => {
    const output = input.closest('.precision-card')?.querySelector('.precision-value');
    const sync = () => {
      if (output) output.textContent = input.value;
    };
    input.addEventListener('input', sync);
    sync();
  });
}

function bindDropzones() {
  document.querySelectorAll('.dropzone').forEach((zone) => bindDropzone(zone));
}

function bindPageNav() {
  document.querySelectorAll('[data-page-target]').forEach((button) => {
    button.addEventListener('click', () => {
      const targetId = button.dataset.pageTarget;
      const target = targetId ? document.getElementById(targetId) : null;
      target?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
  });
}

function bindCacheButtons() {
  document.querySelectorAll('[data-cache-save]').forEach((button) => {
    button.addEventListener('click', async () => {
      const targetId = button.dataset.cacheSave;
      const input = targetId ? document.getElementById(targetId) : null;
      const file = input?.files?.[0];
      if (!input || !file) {
        button.textContent = 'Сначала выберите PDF';
        setTimeout(() => { button.textContent = 'Сохранить в кэш'; }, 1500);
        return;
      }

      try {
        await saveFileToCache(targetId, file);
        button.textContent = 'Сохранено';
        setTimeout(() => { button.textContent = 'Сохранить в кэш'; }, 1200);
      } catch (error) {
        button.textContent = 'Кэш недоступен';
        setTimeout(() => { button.textContent = 'Сохранить в кэш'; }, 1500);
      }
    });
  });

  document.querySelectorAll('[data-cache-load]').forEach((button) => {
    button.addEventListener('click', async () => {
      const targetId = button.dataset.cacheLoad;
      const input = targetId ? document.getElementById(targetId) : null;
      if (!input) return;

      try {
        const file = await loadFileFromCache(targetId);
        if (!file) {
          button.textContent = 'Кэш пуст';
          setTimeout(() => { button.textContent = 'Загрузить из кэша'; }, 1500);
          return;
        }
        applyFileToInput(input, file);
        button.textContent = 'Загружено';
        setTimeout(() => { button.textContent = 'Загрузить из кэша'; }, 1200);
      } catch (error) {
        button.textContent = 'Кэш недоступен';
        setTimeout(() => { button.textContent = 'Загрузить из кэша'; }, 1500);
      }
    });
  });
}

function init() {
  bindPrecisionInputs();
  bindDropzones();
  document.querySelectorAll('.file-input').forEach((input) => updateUploadStatus(input));
  bindCacheButtons();
  bindCaptureButtons();
  bindCropButtons();
  bindCropInteractions();
  bindCropModal();
  bindPreviewThumbs();
  bindPageNav();
  bindViewer();

  if (!navigator.mediaDevices?.getDisplayMedia) {
    document.querySelectorAll('[data-capture-browser]').forEach((button) => {
      button.disabled = true;
      button.title = 'Захват экрана браузера недоступен в этом браузере';
    });
  }
}

init();
