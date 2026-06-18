const viewer = document.getElementById('viewer');
const viewerImg = document.getElementById('viewer-img');
const viewerClose = document.getElementById('viewer-close');
const viewerDownload = document.getElementById('viewer-download');

document.querySelectorAll('.preview-btn').forEach((btn) => {
  btn.addEventListener('click', () => {
    const src = btn.dataset.src;
    if (!src) return;
    viewerImg.src = src;
    viewerDownload.href = src;
    viewerDownload.download = btn.closest('.diff-wrap')?.querySelector('.download')?.download || 'diff.png';
    viewer.classList.remove('hidden');
    viewer.setAttribute('aria-hidden', 'false');
  });
});

function closeViewer() {
  viewer.classList.add('hidden');
  viewer.setAttribute('aria-hidden', 'true');
  viewerImg.src = '';
}

viewerClose?.addEventListener('click', closeViewer);
viewer?.addEventListener('click', (e) => {
  if (e.target === viewer) closeViewer();
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !viewer.classList.contains('hidden')) closeViewer();
});
