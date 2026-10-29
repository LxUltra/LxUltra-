(() => {
  const $ = id => document.getElementById(id);
  const api = () => window.pywebview?.api;
  const escape = value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
  let timer = null;
  let startedAt = 0;
  let latest = null;
  let visited = false;

  function render(state) {
    if (!state) return;
    latest = state;
    const running = ['scanning','installing'].includes(state.state);
    const root = $('driver-updates');
    if (root) root.classList.toggle('is-running', running);
    const status = $('driver-status');
    if (status) status.textContent = state.error || state.message || '驱动状态未知。';
    const progress = $('driver-progress');
    const hasPercent = state.state === 'installing' && Number.isFinite(Number(state.percent)) && state.percent !== null;
    const percent = hasPercent ? Math.max(0, Math.min(100, Number(state.percent))) : null;
    if (progress) {
      progress.hidden = !running;
      progress.classList.toggle('is-indeterminate', running && percent === null);
      progress.setAttribute('aria-valuetext', state.message || '驱动任务正在运行');
      if (percent !== null) progress.setAttribute('aria-valuenow', String(percent));
      else progress.removeAttribute('aria-valuenow');
    }
    if ($('driver-progress-bar')) $('driver-progress-bar').style.width = percent !== null ? `${percent}%` : '';
    if ($('driver-progress-text')) $('driver-progress-text').textContent = running
      ? (state.phase === 'downloading' ? '下载' : state.phase === 'installing' ? '安装' : '检测中') +
        (percent !== null ? ` ${percent}%` : ' · 正在处理…')
      : '';
    const scan = $('driver-scan');
    if (scan) scan.disabled = running;
    const list = $('driver-list');
    if (list && !running) {
      const rows = state.updates || [];
      list.innerHTML = rows.length ? rows.map(row => `<label class="driver-row"><input type="checkbox" value="${escape(row.key)}"><span><strong>${escape(row.title)}</strong><small>Windows Update · ${row.size ? (row.size / 1048576).toFixed(1) + ' MB' : '大小未报告'}${row.rebootRequired ? ' · 可能需要重启' : ''}</small></span></label>`).join('') : '<p class="note">没有可安装的驱动。可稍后重新检测。</p>';
      list.querySelectorAll('input').forEach(input => input.addEventListener('change', updateInstallButton));
    }
    updateInstallButton();
    if (running) {
      if (!startedAt) startedAt = Date.now();
      if (!timer) timer = window.setInterval(poll, 2000);
    } else {
      startedAt = 0;
      if (timer) { clearInterval(timer); timer = null; }
      if ($('driver-elapsed')) $('driver-elapsed').textContent = '';
    }
  }

  function updateInstallButton() {
    const install = $('driver-install');
    if (install) install.disabled = ['scanning','installing'].includes(latest?.state) || !$('driver-list')?.querySelector('input:checked');
  }

  async function poll() {
    if (!api()?.driver_updates_status) return;
    try {
      const state = await api().driver_updates_status();
      if (state?.ok) render(state);
      else if ($('driver-status')) $('driver-status').textContent = state?.error || '无法读取驱动任务状态。';
    } catch (error) {
      if ($('driver-status')) $('driver-status').textContent = '无法读取驱动任务状态，请稍后重试。';
    }
    if (startedAt && $('driver-elapsed')) {
      const seconds = Math.floor((Date.now() - startedAt) / 1000);
      $('driver-elapsed').textContent = `已运行 ${String(Math.floor(seconds / 60)).padStart(2,'0')}:${String(seconds % 60).padStart(2,'0')}`;
    }
  }

  async function scan() {
    if (!api()?.driver_updates_scan) return;
    $('driver-scan').disabled = true;
    $('driver-status').textContent = '正在开始驱动检测…';
    try {
      const result = await api().driver_updates_scan();
      if (!result?.ok) throw new Error(result?.error || '检测失败');
      render(result);
    } catch (error) {
      $('driver-status').textContent = String(error.message || error);
      $('driver-scan').disabled = false;
    }
  }

  async function install() {
    const keys = [...$('driver-list').querySelectorAll('input:checked')].map(input => input.value);
    if (!keys.length) return;
    const proceed = async () => {
      $('driver-install').disabled = true;
      try {
        const result = await api().driver_updates_install(keys);
        if (!result?.ok) throw new Error(result?.error || '安装未启动');
        render(result);
      } catch (error) {
        $('driver-status').textContent = String(error.message || error);
        updateInstallButton();
      }
    };
    if (typeof window.confirmAction === 'function') {
      window.confirmAction('安装所选驱动', `将通过 Windows Update 下载并安装 ${keys.length} 项驱动，过程中设备可能短暂断开，完成后可能需要重启。`, proceed);
    } else if (window.confirm(`通过 Windows Update 安装 ${keys.length} 项驱动？`)) {
      await proceed();
    }
  }

  function initialize() {
    $('driver-scan')?.addEventListener('click', scan);
    $('driver-install')?.addEventListener('click', install);
  }
  function enter() {
    if (!visited) {
      visited = true;
      scan();
    } else {
      poll();
    }
  }
  window.LxDriverUpdates = {initialize, enter, refresh: poll};
})();
