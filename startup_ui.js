(function () {
  const splash = () => document.getElementById('startup-splash');

  window.updateStartupSplashProgress = function (value, message) {
    const root = splash();
    if (!root) return;
    const progress = Math.max(0, Math.min(100, Math.round(Number(value) || 0)));
    const bar = root.querySelector('.splash-progress');
    const progressBar = root.querySelector('[role="progressbar"]');
    const label = root.querySelector('.splash-status');
    if (bar) {
      bar.style.width = `${progress}%`;
    }
    if (progressBar) progressBar.setAttribute('aria-valuenow', String(progress));
    if (label && message) label.textContent = message;
  };

  window.finishStartupSplash = function () {
    const root = splash();
    if (!root) return;
    window.updateStartupSplashProgress(100, '准备就绪');
    root.setAttribute('aria-hidden', 'true');
    root.classList.add('is-leaving');
    const reduced = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
    window.setTimeout(() => {
      root.hidden = true;
      root.setAttribute('aria-hidden', 'true');
    }, reduced ? 20 : 420);
  };
}());
