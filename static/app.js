const viewer = document.getElementById('viewer');
const viewerImg = document.getElementById('viewer-img');
const viewerClose = document.getElementById('viewer-close');
const viewerDownload = document.getElementById('viewer-download');

const JOB_STORAGE_KEY = 'pdfDiffActiveJobV1';
const JOB_POLL_INTERVAL_MS = 1000;
let compareRequestSeq = 0;
let compareAbortController = null;
let activeJob = null;
let activeCompareForm = null;
let jobPollTimer = null;

function abortPendingRequest() {
  compareAbortController?.abort();
  compareAbortController = null;
}

function setCompareBusy(form, isBusy) {
  form.setAttribute('aria-busy', String(isBusy));
  form.querySelectorAll('input, select, [data-reset-all], [data-area-mode], [data-compare-submit]').forEach((control) => {
    control.disabled = isBusy;
  });
  const submit = form.querySelector('[data-compare-submit]');
  if (!submit) return;
  submit.textContent = isBusy ? 'Сравниваю…' : 'Сравнить';
}

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
  if (activeJob) void cancelActiveJob();
  else abortPendingRequest();
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

function httpErrorMessage(status, fallbackMessage) {
  const statusMessages = {
    400: 'Проверьте выбранные PDF и параметры.',
    403: 'Запрос отклонён. Обновите страницу и повторите попытку.',
    405: 'Операция недоступна по этому адресу.',
    413: 'Файлы слишком большие для загрузки.',
    429: 'Сервис уже обрабатывает другое сравнение. Повторите через несколько секунд.',
    500: 'Не удалось обработать запрос.',
  };
  return statusMessages[status] || fallbackMessage;
}

function syncUsageMetrics(doc) {
  const nextMetrics = doc.querySelector('.usage-metrics');
  const currentMetrics = document.querySelector('.usage-metrics');
  if (nextMetrics && currentMetrics) currentMetrics.replaceWith(nextMetrics);
  else if (nextMetrics) document.querySelector('.hero')?.insertAdjacentElement('afterend', nextMetrics);
}

function replaceResultsAndErrors(doc) {
  syncUsageMetrics(doc);

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
  bindLazyResultImages();
}

function jobEndpoint(suffix = '') {
  const base = activeCompareForm?.action || document.querySelector('form[action="compare"]')?.action || document.baseURI;
  const endpoint = new URL(`api/jobs${suffix}`, base);
  if (document.body?.dataset.uiMode === 'km') endpoint.searchParams.set('ui', 'km');
  return endpoint.toString();
}

function jobHeaders() {
  return activeJob ? { 'X-Job-Token': activeJob.token } : {};
}

function persistActiveJob() {
  try {
    if (activeJob) localStorage.setItem(JOB_STORAGE_KEY, JSON.stringify(activeJob));
    else localStorage.removeItem(JOB_STORAGE_KEY);
  } catch (_error) {
    // Private browsing can make localStorage unavailable; polling still works in the current page.
  }
}

function jobStageLabel(stage) {
  return {
    queued: 'Ожидание в очереди',
    preparing: 'Подготовка файлов',
    validating: 'Проверка PDF',
    rendering: 'Рендер листов',
    comparing: 'Сравнение листов',
    finalizing: 'Формирование результата',
    completed: 'Сравнение готово',
    cancelling: 'Остановка вычисления',
    cancelled: 'Сравнение отменено',
    error: 'Ошибка сравнения',
  }[stage] || 'Обработка PDF';
}

function renderJobState(job) {
  const panel = document.querySelector('[data-job-progress]');
  if (!panel) return;
  panel.classList.remove('hidden');
  const progress = Math.max(0, Math.min(100, Number(job.progress || 0)));
  const bar = panel.querySelector('[data-job-progress-bar]');
  const percent = panel.querySelector('[data-job-percent]');
  const queue = panel.querySelector('[data-job-queue]');
  const cancel = panel.querySelector('[data-job-cancel]');
  const stage = panel.querySelector('[data-job-stage]');
  const message = panel.querySelector('[data-job-message]');
  if (bar) {
    bar.value = progress;
    bar.textContent = `${progress}%`;
  }
  if (percent) percent.textContent = `${progress}%`;
  if (stage) stage.textContent = jobStageLabel(job.stage);
  if (message) message.textContent = job.message || 'Обрабатываю PDF.';
  if (queue) {
    const position = Number(job.queue_position || 0);
    queue.hidden = job.status !== 'queued';
    queue.textContent = position > 0 ? `Позиция в очереди: ${position}` : '';
  }
  if (cancel) {
    cancel.hidden = ['completed', 'failed', 'cancelled'].includes(job.status);
    cancel.disabled = job.status === 'cancelling';
    cancel.textContent = job.status === 'cancelling' ? 'Останавливаю…' : 'Отменить сравнение';
  }
}

function clearActiveJob({ hideProgress = false } = {}) {
  if (jobPollTimer) window.clearTimeout(jobPollTimer);
  jobPollTimer = null;
  activeJob = null;
  persistActiveJob();
  if (activeCompareForm) setCompareBusy(activeCompareForm, false);
  if (hideProgress) document.querySelector('[data-job-progress]')?.classList.add('hidden');
}

async function jobJson(response, fallbackMessage) {
  let payload = {};
  try {
    payload = await response.json();
  } catch (_error) {
    // Bounded server/proxy errors can be HTML.
  }
  if (!response.ok) {
    const error = new Error(payload.message || httpErrorMessage(response.status, fallbackMessage));
    error.status = response.status;
    throw error;
  }
  return payload;
}

async function loadJobResult() {
  if (!activeJob) return;
  const response = await fetch(jobEndpoint(`/${activeJob.id}/result`), { headers: jobHeaders() });
  if (!response.ok) {
    const payload = await jobJson(response, 'Не удалось получить результат');
    throw new Error(payload.message || 'Не удалось получить результат');
  }
  const html = await response.text();
  const doc = new DOMParser().parseFromString(html, 'text/html');
  replaceResultsAndErrors(doc);
  clearActiveJob({ hideProgress: true });
  document.querySelector('.results-card')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function handleTerminalJob(job) {
  renderJobState(job);
  if (job.status === 'failed') {
    setInlineError(job.error || 'Не удалось сравнить PDF.');
    clearActiveJob();
  } else if (job.status === 'cancelled') {
    clearActiveJob();
  }
}

async function pollActiveJob() {
  if (!activeJob) return;
  const current = activeJob;
  try {
    const response = await fetch(jobEndpoint(`/${current.id}`), { headers: jobHeaders(), cache: 'no-store' });
    const payload = await jobJson(response, 'Не удалось получить состояние сравнения');
    if (!activeJob || activeJob.id !== current.id) return;
    const job = payload.job;
    renderJobState(job);
    if (job.status === 'completed') {
      await loadJobResult();
      return;
    }
    if (job.status === 'failed' || job.status === 'cancelled') {
      handleTerminalJob(job);
      return;
    }
  } catch (error) {
    if (!activeJob || activeJob.id !== current.id) return;
    const message = error instanceof Error ? error.message : 'Связь с сервисом прервана';
    if (error?.status === 403 || error?.status === 404) {
      setInlineError(error?.status === 404 ? 'Сохранённая задача больше недоступна.' : message);
      clearActiveJob({ hideProgress: true });
      return;
    }
    renderJobState({ status: 'running', stage: 'preparing', progress: 0, message: `${message}. Повторяю подключение…` });
  }
  jobPollTimer = window.setTimeout(pollActiveJob, JOB_POLL_INTERVAL_MS);
}

async function cancelActiveJob() {
  abortPendingRequest();
  if (!activeJob) {
    if (activeCompareForm) setCompareBusy(activeCompareForm, false);
    renderJobState({ status: 'cancelled', stage: 'cancelled', progress: 0, message: 'Загрузка отменена' });
    return;
  }
  const current = activeJob;
  renderJobState({ status: 'cancelling', stage: 'cancelling', progress: 0, message: 'Останавливаю вычисление' });
  try {
    const response = await fetch(jobEndpoint(`/${current.id}`), { method: 'DELETE', headers: jobHeaders() });
    const payload = await jobJson(response, 'Не удалось отменить сравнение');
    if (activeJob?.id === current.id) handleTerminalJob(payload.job);
  } catch (error) {
    const message = error instanceof Error ? error.message : 'Не удалось отменить сравнение';
    setInlineError(message);
    if (activeJob?.id === current.id) jobPollTimer = window.setTimeout(pollActiveJob, JOB_POLL_INTERVAL_MS);
  }
}

async function submitCompareForm(form) {
  if (activeJob) return;
  abortPendingRequest();
  const controller = new AbortController();
  compareAbortController = controller;
  const requestSeq = ++compareRequestSeq;
  const formData = new FormData(form);
  activeCompareForm = form;
  setCompareBusy(form, true);
  renderJobState({ status: 'queued', stage: 'preparing', progress: 0, message: 'Загружаю и проверяю PDF', queue_position: 0 });
  try {
    const response = await fetch(jobEndpoint(), {
      method: 'POST',
      body: formData,
      headers: { 'X-Requested-With': 'fetch' },
      signal: controller.signal,
    });
    const payload = await jobJson(response, 'Не удалось создать задачу сравнения');
    if (requestSeq !== compareRequestSeq) return;
    activeJob = { id: payload.job.job_id, token: payload.job_token };
    persistActiveJob();
    renderJobState(payload.job);
    await pollActiveJob();
  } finally {
    if (compareAbortController === controller) compareAbortController = null;
    if (!activeJob && requestSeq === compareRequestSeq && !controller.signal.aborted) setCompareBusy(form, false);
  }
}

function restoreActiveJob() {
  try {
    const saved = JSON.parse(localStorage.getItem(JOB_STORAGE_KEY) || 'null');
    if (!saved?.id || !saved?.token) return;
    activeJob = saved;
    activeCompareForm = document.querySelector('form[action="compare"]');
    if (!activeCompareForm) return;
    setCompareBusy(activeCompareForm, true);
    renderJobState({ status: 'running', stage: 'preparing', progress: 0, message: 'Восстанавливаю состояние задачи' });
    void pollActiveJob();
  } catch (_error) {
    activeJob = null;
    persistActiveJob();
  }
}

function bindCompareForm() {
  document.querySelectorAll('form[action="compare"]').forEach((form) => {
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      try {
        await submitCompareForm(form);
      } catch (error) {
        if (error instanceof DOMException && error.name === 'AbortError') return;
        const message = error instanceof Error ? error.message : 'Не удалось обновить сравнение';
        setInlineError(message);
        clearActiveJob();
      }
    });

    form.querySelector('.precision-input')?.addEventListener('change', () => {
      if (!activeJob && formHasSelectedFiles(form)) form.requestSubmit();
    });
  });
  document.querySelector('[data-job-cancel]')?.addEventListener('click', () => void cancelActiveJob());
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
      const form = button.closest('form');
      const alignment = form?.querySelector('[name="align_pages"]');
      if (alignment instanceof HTMLInputElement) alignment.checked = false;
      const precision = form?.querySelector('[name="precision"]');
      if (precision instanceof HTMLInputElement) {
        precision.value = '10';
        precision.dispatchEvent(new Event('input', { bubbles: true }));
      }
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

function loadPageArtifacts(page) {
  if (!page || page.dataset.artifactsRequested === 'true') return;
  const images = Array.from(page.querySelectorAll('img[data-lazy-src]'));
  if (!images.length) return;
  page.dataset.artifactsRequested = 'true';
  const stage = page.querySelector('.compare-stage');
  let remaining = images.length;
  const complete = () => {
    remaining -= 1;
    if (remaining <= 0) stage?.classList.add('is-loaded');
  };
  images.forEach((image) => {
    image.addEventListener('load', complete, { once: true });
    image.addEventListener('error', complete, { once: true });
    image.src = image.dataset.lazySrc || '';
    image.removeAttribute('data-lazy-src');
  });
}

function bindLazyResultImages() {
  const pages = Array.from(document.querySelectorAll('.results-pages .page'));
  if (!pages.length) return;
  loadPageArtifacts(pages[0]);
  if (!('IntersectionObserver' in window)) {
    pages.forEach(loadPageArtifacts);
    return;
  }
  const observer = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        loadPageArtifacts(entry.target);
        observer.unobserve(entry.target);
      });
    },
    { rootMargin: '800px 0px', threshold: 0.01 },
  );
  pages.slice(1).forEach((page) => observer.observe(page));
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
      loadPageArtifacts(target);
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

const areaState = {
  form: null,
  left: null,
  right: null,
  sourceRect: null,
  targetRect: null,
  active: null,
  trigger: null,
};

function areaFormData() {
  const data = new FormData(areaState.form);
  const selectedPage = document.querySelector('[data-area-page]')?.value || '0';
  data.set('page', selectedPage);
  return data;
}

function appendRect(data, prefix, rect) {
  data.append(`${prefix}[x]`, Math.round(rect.x));
  data.append(`${prefix}[y]`, Math.round(rect.y));
  data.append(`${prefix}[width]`, Math.round(rect.width));
  data.append(`${prefix}[height]`, Math.round(rect.height));
}

function areaSetStatus(message) {
  const node = document.querySelector('[data-area-status]');
  if (node) node.textContent = message;
}

function areaEndpoint(endpoint) {
  const base = areaState.form?.action || document.baseURI;
  return new URL(endpoint, base).toString();
}

function areaSetBusy(isBusy) {
  const modal = document.getElementById('area-modal');
  modal?.setAttribute('aria-busy', String(isBusy));
  document.querySelectorAll('[data-area-mode], [data-area-detect], [data-area-compare]').forEach((button) => {
    button.disabled = isBusy || (button.hasAttribute('data-area-compare') && !areaState.targetRect);
  });
  const pageSelect = document.querySelector('[data-area-page]');
  if (pageSelect) pageSelect.disabled = isBusy || !areaState.left || pageSelect.options.length <= 1;
}

async function areaJson(response, fallbackMessage) {
  let payload = {};
  try {
    payload = await response.json();
  } catch (_error) {
    // Proxy and Flask limit errors can return HTML instead of JSON.
  }
  if (!response.ok) {
    throw new Error(payload.message || httpErrorMessage(response.status, fallbackMessage));
  }
  return payload;
}

function areaErrorMessage(error, fallbackMessage) {
  if (error instanceof TypeError) return 'Сервис временно недоступен. Повторите попытку.';
  return error instanceof Error ? error.message : fallbackMessage;
}

function areaOpen(trigger = null) {
  const modal = document.getElementById('area-modal');
  areaState.trigger = trigger || document.activeElement;
  modal?.classList.remove('hidden');
  modal?.setAttribute('aria-hidden', 'false');
  modal?.querySelector('[data-area-close]')?.focus();
}

function areaClose() {
  const modal = document.getElementById('area-modal');
  modal?.classList.add('hidden');
  modal?.setAttribute('aria-hidden', 'true');
  areaState.trigger?.focus?.();
}

function clientPointToImage(canvas, meta, event) {
  const box = canvas.getBoundingClientRect();
  return {
    x: Math.max(0, Math.min(meta.width, ((event.clientX - box.left) / box.width) * meta.width)),
    y: Math.max(0, Math.min(meta.height, ((event.clientY - box.top) / box.height) * meta.height)),
  };
}

function clampAreaRect(rect, meta) {
  const width = Math.max(1, Math.min(rect.width, meta.width));
  const height = Math.max(1, Math.min(rect.height, meta.height));
  return {
    x: Math.max(0, Math.min(rect.x, meta.width - width)),
    y: Math.max(0, Math.min(rect.y, meta.height - height)),
    width,
    height,
  };
}

function drawAreaRect(side) {
  const meta = areaState[side];
  const rect = side === 'left' ? areaState.sourceRect : areaState.targetRect;
  const node = document.querySelector(`[data-area-rect="${side}"]`);
  if (!node || !meta || !rect) return;
  node.hidden = false;
  node.style.left = `${(rect.x / meta.width) * 100}%`;
  node.style.top = `${(rect.y / meta.height) * 100}%`;
  node.style.width = `${(rect.width / meta.width) * 100}%`;
  node.style.height = `${(rect.height / meta.height) * 100}%`;
}

function setAreaImages(payload) {
  areaState.left = payload.left;
  areaState.right = payload.right;
  areaState.sourceRect = null;
  areaState.targetRect = null;
  const pageSelect = document.querySelector('[data-area-page]');
  const pagePicker = document.querySelector('[data-area-page-picker]');
  const pageCount = Math.max(1, Number(payload.page_count || 1));
  const selectedPage = Math.max(0, Math.min(pageCount - 1, Number(payload.page_index || 0)));
  if (pageSelect) {
    pageSelect.replaceChildren(...Array.from({ length: pageCount }, (_value, index) => {
      const option = document.createElement('option');
      option.value = String(index);
      option.textContent = `Лист ${index + 1}`;
      return option;
    }));
    pageSelect.value = String(selectedPage);
    pageSelect.disabled = pageCount <= 1;
  }
  if (pagePicker) pagePicker.hidden = pageCount <= 1;
  ['left', 'right'].forEach((side) => {
    const img = document.querySelector(`[data-area-image="${side}"]`);
    const rect = document.querySelector(`[data-area-rect="${side}"]`);
    if (img) img.src = payload[side].image_url || `data:image/png;base64,${payload[side].image}`;
    if (rect) rect.hidden = true;
  });
  document.querySelector('[data-area-compare]')?.setAttribute('disabled', 'disabled');
  areaSetStatus('Выделите область на исходном чертеже.');
}

function bindAreaSelection() {
  const leftCanvas = document.querySelector('[data-area-canvas="left"]');
  leftCanvas?.addEventListener('pointerdown', (event) => {
    if (!areaState.left) return;
    event.preventDefault();
    const start = clientPointToImage(leftCanvas, areaState.left, event);
    areaState.active = { side: 'left', mode: 'draw', start };
    areaState.sourceRect = { x: start.x, y: start.y, width: 1, height: 1 };
    drawAreaRect('left');
    leftCanvas.setPointerCapture?.(event.pointerId);
  });
  leftCanvas?.addEventListener('pointermove', (event) => {
    if (areaState.active?.side !== 'left' || !areaState.left) return;
    const point = clientPointToImage(leftCanvas, areaState.left, event);
    const start = areaState.active.start;
    areaState.sourceRect = {
      x: Math.min(start.x, point.x),
      y: Math.min(start.y, point.y),
      width: Math.abs(point.x - start.x),
      height: Math.abs(point.y - start.y),
    };
    drawAreaRect('left');
  });

  const rightCanvas = document.querySelector('[data-area-canvas="right"]');
  rightCanvas?.addEventListener('pointerdown', (event) => {
    if (!areaState.right || !areaState.targetRect) return;
    event.preventDefault();
    const point = clientPointToImage(rightCanvas, areaState.right, event);
    const rect = areaState.targetRect;
    const nearCorner = Math.abs(point.x - (rect.x + rect.width)) < 30 && Math.abs(point.y - (rect.y + rect.height)) < 30;
    areaState.active = { side: 'right', mode: nearCorner ? 'resize' : 'move', start: point, original: { ...rect } };
    rightCanvas.setPointerCapture?.(event.pointerId);
  });
  rightCanvas?.addEventListener('pointermove', (event) => {
    if (areaState.active?.side !== 'right' || !areaState.right) return;
    const point = clientPointToImage(rightCanvas, areaState.right, event);
    const { start, original, mode } = areaState.active;
    if (mode === 'move') {
      areaState.targetRect = clampAreaRect({ ...original, x: original.x + point.x - start.x, y: original.y + point.y - start.y }, areaState.right);
    } else {
      areaState.targetRect = clampAreaRect({ ...original, width: original.width + point.x - start.x, height: original.height + point.y - start.y }, areaState.right);
    }
    drawAreaRect('right');
  });

  document.addEventListener('pointerup', () => {
    areaState.active = null;
  });
}

async function requestAreaPreview(form, trigger = null) {
  areaState.form = form;
  areaOpen(trigger);
  areaSetBusy(true);
  areaSetStatus('Готовлю предпросмотр...');
  try {
    const response = await fetch(areaEndpoint('area-preview'), { method: 'POST', body: areaFormData() });
    const payload = await areaJson(response, 'Не удалось подготовить область');
    setAreaImages(payload);
  } finally {
    areaSetBusy(false);
  }
}

async function detectArea() {
  if (!areaState.sourceRect) {
    areaSetStatus('Сначала выделите область на исходном чертеже.');
    return;
  }
  areaSetBusy(true);
  areaSetStatus('Ищу область на втором чертеже...');
  try {
    const data = areaFormData();
    appendRect(data, 'sourceRect', areaState.sourceRect);
    const response = await fetch(areaEndpoint('detect-area'), { method: 'POST', body: data });
    const payload = await areaJson(response, 'Не удалось найти область');
    areaState.sourceRect = payload.sourceRect;
    areaState.targetRect = payload.targetRect;
    drawAreaRect('left');
    drawAreaRect('right');
    const percent = Math.round((payload.confidence || 0) * 100);
    areaSetStatus(`${payload.message || 'Область найдена.'} Уверенность: ${percent}%. При необходимости поправьте рамку справа.`);
  } finally {
    areaSetBusy(false);
  }
}

async function compareArea() {
  if (!areaState.sourceRect || !areaState.targetRect) return;
  areaSetBusy(true);
  areaSetStatus('Сравниваю выбранную область...');
  try {
    const data = areaFormData();
    appendRect(data, 'sourceRect', areaState.sourceRect);
    appendRect(data, 'targetRect', areaState.targetRect);
    const response = await fetch(areaEndpoint('compare-area'), { method: 'POST', body: data, headers: { 'X-Requested-With': 'fetch' } });
    const html = await response.text();
    const doc = new DOMParser().parseFromString(html, 'text/html');
    if (!response.ok) {
      const serverMessage = doc.querySelector('.error')?.textContent?.trim();
      throw new Error(serverMessage || httpErrorMessage(response.status, 'Не удалось сравнить область'));
    }
    replaceResultsAndErrors(doc);
    areaClose();
  } finally {
    areaSetBusy(false);
  }
}

function bindAreaMode() {
  bindAreaSelection();
  document.querySelectorAll('[data-area-mode]').forEach((button) => {
    button.addEventListener('click', async () => {
      const form = button.closest('form');
      if (!form || !formHasSelectedFiles(form)) {
        setInlineError('Нужно выбрать два PDF файла');
        return;
      }
      try {
        await requestAreaPreview(form, button);
      } catch (error) {
        setInlineError(areaErrorMessage(error, 'Не удалось открыть режим области'));
      }
    });
  });
  document.querySelector('[data-area-close]')?.addEventListener('click', areaClose);
  document.querySelector('[data-area-detect]')?.addEventListener('click', async () => {
    try { await detectArea(); } catch (error) { areaSetStatus(areaErrorMessage(error, 'Не удалось найти область')); }
  });
  document.querySelector('[data-area-compare]')?.addEventListener('click', async () => {
    try { await compareArea(); } catch (error) { areaSetStatus(areaErrorMessage(error, 'Не удалось сравнить область')); }
  });
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
  bindAreaMode();
  bindCompareSliders();
  bindLazyResultImages();
  bindPreviewButtons();
  bindPageNav();
  bindViewer();
  restoreActiveJob();
}

init();
