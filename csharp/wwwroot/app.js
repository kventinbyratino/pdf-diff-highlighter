const viewer = document.getElementById('viewer');
const viewerImg = document.getElementById('viewer-img');
const viewerClose = document.getElementById('viewer-close');
const viewerDownload = document.getElementById('viewer-download');

let compareRequestSeq = 0;

function updateUploadStatus(input) {
  const status = document.querySelector(`[data-upload-status="${input.id}"]`);
  const card = input.closest('.slot-card');
  const clearButton = card?.querySelector('[data-clear-file]');
  if (!status || !card) return;

  const label = input.id === 'pdf1' ? 'Чертеж 1' : 'Чертеж 2';
  const loaded = Boolean(input.files?.[0]);
  status.textContent = `${label} — статус: ${loaded ? 'загружен' : 'не загружен'}`;
  status.classList.toggle('slot-status-loaded', loaded);
  card.classList.toggle('has-file', loaded);
  if (clearButton) clearButton.hidden = !loaded;
}

function applyFileToInput(input, file) {
  if (!input || !file) return;
  const dt = new DataTransfer();
  dt.items.add(file);
  input.files = dt.files;
  input.dispatchEvent(new Event('change', { bubbles: true }));
}

function clearFileInput(input) {
  if (!input) return;
  input.value = '';
  compareRequestSeq += 1;
  input.dispatchEvent(new Event('change', { bubbles: true }));
}

function formHasSelectedFiles(form) {
  const inputs = Array.from(form.querySelectorAll('.file-input'));
  return inputs.length > 0 && inputs.every((input) => Boolean(input.files?.[0]));
}

function setInlineError(message) {
  const currentError = document.querySelector('.error');
  if (currentError) {
    currentError.textContent = message;
    return;
  }
  const error = document.createElement('div');
  error.className = 'error';
  error.textContent = message;
  document.querySelector('.hero')?.insertAdjacentElement('afterend', error);
}

function replaceResultsAndErrors(doc) {
  const nextError = doc.querySelector('.error');
  const currentError = document.querySelector('.error');
  if (nextError) {
    if (currentError) currentError.replaceWith(nextError);
    else document.querySelector('.hero')?.insertAdjacentElement('afterend', nextError);
  } else {
    currentError?.remove();
  }

  const nextResults = doc.querySelector('.results-card');
  const currentResults = document.querySelector('.results-card');
  if (nextResults) {
    if (currentResults) currentResults.replaceWith(nextResults);
    else document.querySelector('.workspace')?.insertAdjacentElement('afterend', nextResults);
  } else {
    currentResults?.remove();
  }

  bindPageNav();
  bindPreviewButtons();
  bindCompareSliders();
}

async function submitCompareForm(form) {
  const requestSeq = ++compareRequestSeq;
  const response = await fetch(form.action || window.location.href, {
    method: (form.method || 'POST').toUpperCase(),
    body: new FormData(form),
    headers: { 'X-Requested-With': 'fetch' },
  });
  const html = await response.text();
  if (requestSeq !== compareRequestSeq) return;
  const doc = new DOMParser().parseFromString(html, 'text/html');
  replaceResultsAndErrors(doc);
}

function bindCompareForm() {
  document.querySelectorAll('form[action="/compare"]').forEach((form) => {
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      try {
        await submitCompareForm(form);
      } catch (error) {
        const message = error instanceof Error ? error.message : 'Не удалось обновить сравнение';
        setInlineError(message);
      }
    });

    form.querySelector('.precision-input')?.addEventListener('change', () => {
      if (formHasSelectedFiles(form)) form.requestSubmit();
    });
  });
}

function bindDropzone(zone) {
  const input = zone.querySelector('input[type="file"]');
  if (!input) return;

  input.addEventListener('change', () => updateUploadStatus(input));

  zone.querySelector('[data-clear-file]')?.addEventListener('click', (event) => {
    event.preventDefault();
    event.stopPropagation();
    clearFileInput(input);
    input.focus();
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

function bindDropzones() {
  document.querySelectorAll('.dropzone').forEach((zone) => bindDropzone(zone));
  document.querySelectorAll('.file-input').forEach((input) => updateUploadStatus(input));
}

function bindResetButtons() {
  document.querySelectorAll('[data-reset-all]').forEach((button) => {
    button.addEventListener('click', () => {
      document.querySelectorAll('.file-input').forEach((input) => clearFileInput(input));
      document.querySelector('.results-card')?.remove();
      document.querySelector('.error')?.remove();
    });
  });
}

function bindPrecisionInputs() {
  document.querySelectorAll('.precision-input').forEach((input) => {
    const output = input.closest('.precision-row')?.querySelector('.precision-value');
    const sync = () => {
      if (output) output.textContent = input.value;
    };
    input.addEventListener('input', sync);
    sync();
  });
}

function bindCompareSliders() {
  document.querySelectorAll('[data-compare-slider]').forEach((slider) => {
    const range = slider.querySelector('[data-compare-range]');
    const stage = slider.querySelector('.compare-stage');
    if (!range) return;

    const sync = () => {
      const split = `${range.value}%`;
      slider.style.setProperty('--split', split);
      stage?.style.setProperty('--split', split);
    };

    range.addEventListener('input', sync);
    range.addEventListener('change', sync);
    sync();
  });
}

function bindPageNav() {
  const buttons = Array.from(document.querySelectorAll('[data-page-target]'));
  if (!buttons.length) return;

  const setActive = (targetId) => {
    buttons.forEach((button) => {
      const active = button.dataset.pageTarget === targetId;
      button.classList.toggle('is-active', active);
      if (active) button.setAttribute('aria-current', 'page');
      else button.removeAttribute('aria-current');
    });
  };

  buttons.forEach((button) => {
    button.addEventListener('click', () => {
      const targetId = button.dataset.pageTarget;
      const target = targetId ? document.getElementById(targetId) : null;
      setActive(targetId);
      target?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
  });

  const pages = buttons
    .map((button) => document.getElementById(button.dataset.pageTarget || ''))
    .filter(Boolean);

  if (pages.length && 'IntersectionObserver' in window) {
    const observer = new IntersectionObserver(
      (entries) => {
        const visible = entries
          .filter((entry) => entry.isIntersecting)
          .sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];
        if (visible?.target?.id) setActive(visible.target.id);
      },
      { rootMargin: '-25% 0px -60% 0px', threshold: [0.18, 0.35, 0.55] },
    );
    pages.forEach((page) => observer.observe(page));
  }

  setActive(pages[0]?.id || buttons[0].dataset.pageTarget || '');
}

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

function closeViewer() {
  viewer?.classList.add('hidden');
  viewer?.setAttribute('aria-hidden', 'true');
  resetViewerZoom();
  if (viewerImg) viewerImg.src = '';
}

function openViewer(src, downloadName = 'preview.png') {
  if (!viewer || !viewerImg || !viewerDownload) return;
  resetViewerZoom();
  viewerImg.src = src;
  viewerDownload.href = src;
  viewerDownload.download = downloadName;
  viewer.classList.remove('hidden');
  viewer.setAttribute('aria-hidden', 'false');
}

function bindPreviewButtons() {
  document.querySelectorAll('[data-viewer-src]').forEach((button) => {
    button.addEventListener('click', () => {
      const src = button.dataset.viewerSrc || '';
      if (src) openViewer(src, button.dataset.download || 'diff.png');
    });
  });
}

function bindViewer() {
  viewerClose?.addEventListener('click', closeViewer);
  viewer?.addEventListener('click', (event) => {
    if (event.target === viewer) closeViewer();
  });
  viewer?.addEventListener('wheel', (event) => {
    if (viewer.classList.contains('hidden')) return;
    event.preventDefault();
    const direction = event.deltaY < 0 ? 1 : -1;
    viewerState.scale = Math.min(4, Math.max(1, viewerState.scale + direction * 0.25));
    if (viewerState.scale === 1) {
      viewerState.offsetX = 0;
      viewerState.offsetY = 0;
    }
    applyViewerTransform();
  }, { passive: false });
  viewerImg?.addEventListener('pointerdown', (event) => {
    if (viewerState.scale <= 1) return;
    event.preventDefault();
    viewerState.dragging = true;
    viewerState.dragStartX = event.clientX;
    viewerState.dragStartY = event.clientY;
    viewerState.startOffsetX = viewerState.offsetX;
    viewerState.startOffsetY = viewerState.offsetY;
    viewerImg.setPointerCapture?.(event.pointerId);
    viewerImg.classList.add('is-dragging');
  });
  viewerImg?.addEventListener('pointermove', (event) => {
    if (!viewerState.dragging) return;
    viewerState.offsetX = viewerState.startOffsetX + event.clientX - viewerState.dragStartX;
    viewerState.offsetY = viewerState.startOffsetY + event.clientY - viewerState.dragStartY;
    applyViewerTransform();
  });
  const stopDrag = (event) => {
    if (!viewerState.dragging) return;
    viewerState.dragging = false;
    viewerImg?.releasePointerCapture?.(event.pointerId);
    viewerImg?.classList.remove('is-dragging');
  };
  viewerImg?.addEventListener('pointerup', stopDrag);
  viewerImg?.addEventListener('pointercancel', stopDrag);
  viewerImg?.addEventListener('dragstart', (event) => event.preventDefault());
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') closeViewer();
  });
}

function init() {
  bindPrecisionInputs();
  bindDropzones();
  bindResetButtons();
  bindCompareForm();
  bindCompareSliders();
  bindPreviewButtons();
  bindPageNav();
  bindViewer();
}

init();
