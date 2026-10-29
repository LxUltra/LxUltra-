(() => {
  const waitingPattern = /正在|准备(?:扫描|读取|检查)|读取中|扫描中|检查中|搜索中|处理中|下载中|正在停止|正在复制|正在校验|已检查\s*[\d,]+\s*个文件\s*[·|，,]\s*当前/i;
  const settledPattern = /(?:扫描|检查|搜索|处理|下载|读取|修复|评测|清理|保存|刷新|导入|导出|迁移|粉碎|整理|命名).{0,12}(?:完成|失败|取消|中断|未完成|暂不可用)|已(?:完成|取消|移入|应用|保存|找到|读取)|尚未|没有发现|无法/i;
  const statusSelector = '[role="status"], [aria-live="polite"], [aria-live="assertive"]';
  const pendingButtons = new Set();

  function statusIsWaiting(element) {
    if (!element || !element.isConnected || element.hidden) return false;
    if (element.getAttribute('aria-busy') === 'true') return true;
    const text = (element.innerText || element.textContent || '').replace(/\s+/g, ' ').trim();
    return !!text && waitingPattern.test(text) && !settledPattern.test(text);
  }

  function updateStatus(element) {
    if (!(element instanceof HTMLElement)) return;
    const waiting = statusIsWaiting(element);
    if (element.hasAttribute('data-ui-waiting') !== waiting) element.toggleAttribute('data-ui-waiting', waiting);
  }

  function scopeFor(button) {
    return button.closest('.panel, .fm-panel, .settings-group, .pane, .opt-subpane') || document;
  }

  function scopeIsWaiting(scope) {
    return [...scope.querySelectorAll(statusSelector)].some(statusIsWaiting);
  }

  function syncPendingButtons() {
    for (const button of pendingButtons) {
      if (!button.isConnected) {
        pendingButtons.delete(button);
        continue;
      }
      const waiting = button.disabled || scopeIsWaiting(scopeFor(button));
      button.toggleAttribute('data-ui-busy', waiting);
      if (!waiting) pendingButtons.delete(button);
    }
  }

  function scanStatuses(root = document) {
    if (root instanceof Element && root.matches(statusSelector)) updateStatus(root);
    root.querySelectorAll?.(statusSelector).forEach(updateStatus);
  }

  document.addEventListener('click', event => {
    const button = event.target instanceof Element ? event.target.closest('button') : null;
    if (!button || button.disabled || button.matches('#confirm-cancel, [role="tab"], .nav button')) return;
    pendingButtons.add(button);
    window.setTimeout(() => {
      if (!button.isConnected) return pendingButtons.delete(button);
      const waiting = button.disabled || scopeIsWaiting(scopeFor(button));
      button.toggleAttribute('data-ui-busy', waiting);
      if (!waiting) pendingButtons.delete(button);
    }, 100);
  }, true);

  const observer = new MutationObserver(records => {
    const affected = new Set();
    for (const record of records) {
      const target = record.target instanceof Element ? record.target : record.target.parentElement;
      if (target?.matches?.(statusSelector)) affected.add(target);
      const status = target?.closest?.(statusSelector);
      if (status) affected.add(status);
      record.addedNodes?.forEach(node => {
        if (node instanceof Element) {
          if (node.matches(statusSelector)) affected.add(node);
          node.querySelectorAll?.(statusSelector).forEach(item => affected.add(item));
        }
      });
    }
    affected.forEach(updateStatus);
    syncPendingButtons();
  });

  document.querySelectorAll('#storage-status').forEach(status => {
    status.setAttribute('role', 'status');
    status.setAttribute('aria-live', 'polite');
  });
  scanStatuses();
  observer.observe(document.documentElement, {subtree: true, childList: true, characterData: true, attributes: true, attributeFilter: ['aria-busy', 'disabled', 'hidden']});
})();
