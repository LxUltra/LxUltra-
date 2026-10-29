(() => {
  const $ = id => document.getElementById(id);
  let items = [];
  let busy = false;
  let hasScanned = false;

  function formatBytes(value) {
    let bytes = Math.max(0, Number(value) || 0);
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let index = 0;
    while (bytes >= 1024 && index < units.length - 1) { bytes /= 1024; index++; }
    return `${bytes >= 10 ? bytes.toFixed(0) : bytes.toFixed(1)} ${units[index]}`;
  }

  function setStatus(message, waiting = false) {
    const status = $('accelerator-status');
    status.textContent = message;
    status.setAttribute('aria-busy', String(waiting));
  }

  function setBusy(value) {
    busy = value;
    $('accelerator-scan').disabled = value;
    $('accelerator-run').disabled = value || !items.some(item => item.selected);
    $('accelerator-select-all').disabled = value || !items.length;
    $('accelerator-clear').disabled = value || !items.some(item => item.selected);
  }

  function renderItems() {
    const list = $('accelerator-file-list');
    const total = items.reduce((sum, item) => sum + Math.max(0, Number(item.size) || 0), 0);
    $('accelerator-temp-size').textContent = hasScanned ? formatBytes(total) : '尚未扫描';
    $('accelerator-result-count').textContent = hasScanned ? `找到 ${items.length} 个旧临时文件 · ${formatBytes(total)}` : '';
    $('accelerator-results').hidden = !hasScanned;
    list.innerHTML = items.length ? items.slice(0, 300).map((item, index) =>
      `<label class="accelerator-file"><input type="checkbox" data-accelerator-index="${index}" ${item.selected ? 'checked' : ''}><span><strong>${escapeHtml(item.name || item.path.split(/[\\/]/).pop())}</strong><small>${escapeHtml(item.path)} · ${formatBytes(item.size)}</small></span></label>`
    ).join('') : hasScanned ? '<p class="note">没有发现超过 24 小时的系统临时文件。</p>' : '';
    if (items.length > 300) list.insertAdjacentHTML('beforeend', '<p class="note">列表显示前 300 项；“勾选全部”会包含本次扫描发现的全部项目。</p>');
    list.querySelectorAll('[data-accelerator-index]').forEach(input => input.addEventListener('change', () => {
      items[Number(input.dataset.acceleratorIndex)].selected = input.checked;
      setBusy(busy);
      const selectedCount = items.filter(item => item.selected).length;
      $('accelerator-run').textContent = selectedCount ? `立即加速（${selectedCount}）` : '立即加速';
    }));
    setBusy(busy);
  }

  async function refreshMemory() {
    if (!window.pywebview?.api) return;
    const output = $('accelerator-memory');
    output.textContent = '正在读取…';
    output.setAttribute('aria-busy', 'true');
    try {
      const snapshot = await window.pywebview.api.metrics();
      const percent = Number(snapshot?.memory?.percent);
      output.textContent = Number.isFinite(percent) ? `${Math.round(percent)}%` : '暂不可用';
    } catch (error) {
      console.warn('[LxUltra] accelerator memory reading failed', error);
      output.textContent = '暂不可用';
    } finally {
      output.setAttribute('aria-busy', 'false');
    }
  }

  async function scan() {
    if (busy || !window.pywebview?.api) return;
    setBusy(true);
    hasScanned = false;
    items = [];
    renderItems();
    setStatus('正在扫描系统临时文件…', true);
    try {
      const result = await window.pywebview.api.junk_scan(['system']);
      if (!result?.ok) throw new Error(result?.error || '扫描未完成');
      items = (Array.isArray(result.items) ? result.items : []).filter(item => item && typeof item.id === 'string' && typeof item.path === 'string').map(item => ({...item, selected: false}));
      hasScanned = true;
      renderItems();
      setStatus(items.length ? '扫描完成。结果默认未勾选；请核对文件后选择要移入回收站的项目。' : '扫描完成，没有发现可处理的旧临时文件。');
    } catch (error) {
      console.error('[LxUltra] accelerator scan failed', error);
      setStatus('扫描未完成，请稍后重试。');
    } finally {
      setBusy(false);
    }
  }

  async function accelerate() {
    if (busy || !window.pywebview?.api) return;
    const selected = items.filter(item => item.selected);
    if (!selected.length) return;
    const bytes = selected.reduce((sum, item) => sum + (Number(item.size) || 0), 0);
    const accepted = await window.askConfirmation('将所选临时文件移入回收站？', `共 ${selected.length} 个文件，${formatBytes(bytes)}。这些文件可从 Windows 回收站恢复。`, '移入回收站');
    if (!accepted) return;
    setBusy(true);
    setStatus('正在将所选临时文件移入回收站…', true);
    try {
      const result = await window.pywebview.api.junk_cleanup(selected.map(item => item.id));
      if (!result || typeof result !== 'object') throw new Error('清理结果暂不可用');
      const moved = Math.max(0, Number(result.moved) || 0);
      const failed = Math.max(0, Number(result.failed) || 0);
      const movedBytes = Math.max(0, Number(result.bytes) || 0);
      const movedIds = new Set(Array.isArray(result.movedIds) ? result.movedIds : []);
      if (movedIds.size) items = items.filter(item => !movedIds.has(item.id));
      else if (moved) { items = []; hasScanned = false; }
      items.forEach(item => { item.selected = false; });
      renderItems();
      setStatus(`加速完成：${moved} 个文件已移入回收站，释放 ${formatBytes(movedBytes)}；失败 ${failed} 个。`);
      if (failed) setStatus(`部分完成：${moved} 个文件已移入回收站（${formatBytes(movedBytes)}），${failed} 个未处理；请重新扫描核对。`);
    } catch (error) {
      console.error('[LxUltra] accelerator cleanup failed', error);
      setStatus('加速未完成，请重新扫描后核对文件状态。');
    } finally {
      setBusy(false);
    }
  }

  $('accelerator-scan').addEventListener('click', scan);
  $('accelerator-run').addEventListener('click', accelerate);
  $('accelerator-select-all').addEventListener('click', () => { items.forEach(item => { item.selected = true; }); renderItems(); });
  $('accelerator-clear').addEventListener('click', () => { items.forEach(item => { item.selected = false; }); renderItems(); });

  window.LxAccelerator = {enter() { refreshMemory(); }};
  refreshMemory();
})();
