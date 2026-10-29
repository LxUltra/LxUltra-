(() => {
  const section = document.getElementById('fm-section-software');
  if (!section || section.dataset.softwareViewsReady === 'true') return;
  section.dataset.softwareViewsReady = 'true';

  const api = () => window.pywebview?.api;
  const e = value => String(value ?? '').replace(/[&<>"']/g, char => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  })[char]);
  const bytes = value => window.fmtBytes ? window.fmtBytes(value) : '—';
  const status = message => {
    const notice = document.getElementById('fm-notice');
    if (notice) notice.textContent = message;
    const line = document.getElementById('fm-software-move-status');
    if (line) line.textContent = message;
  };

  const nav = document.createElement('nav');
  nav.className = 'fm-subtabs fm-software-views';
  nav.setAttribute('role', 'tablist');
  nav.setAttribute('aria-label', '软件管理功能');
  nav.innerHTML = '<button class="active" type="button" role="tab" data-software-view="manage" aria-selected="true">软件管理</button><button type="button" role="tab" data-software-view="move" aria-selected="false">软件搬家</button>';

  const manage = document.createElement('div');
  manage.className = 'fm-layout';
  manage.dataset.softwareViewPanel = 'manage';
  const previousChildren = [...section.children];
  previousChildren.forEach(child => manage.append(child));

  const move = document.createElement('div');
  move.className = 'fm-layout fm-hidden';
  move.dataset.softwareViewPanel = 'move';
  move.innerHTML = `<div class="fm-panel fm-software-move-panel">
    <div class="fm-row fm-software-heading"><div><h2>软件搬家</h2><p class="fm-muted">把符合条件的软件迁移到另一块磁盘。搬家前会逐项预览并确认；完成后保留原安装路径以维持快捷方式和卸载信息。</p></div><button class="btn" id="fm-software-move-refresh" type="button">重新扫描</button></div>
    <div class="fm-software-move-summary" id="fm-software-move-summary" role="status" aria-live="polite">正在读取软件清单…</div>
    <label class="fm-software-move-target-label">搬家目标文件夹<div class="fm-row"><span id="fm-software-move-target-path" class="fm-paths">尚未选择目标磁盘</span><button class="btn primary" id="fm-software-move-target" type="button">选择目标</button></div></label>
    <div class="fm-software-move-table" role="table" aria-label="可搬家软件清单"><div class="fm-software-move-header" role="row"><span role="columnheader">名称与安装位置</span><span role="columnheader">大小</span><span role="columnheader">操作</span></div><div id="fm-software-move-list" class="fm-software-move-list" role="rowgroup" aria-live="polite"></div></div>
    <p class="fm-software-move-status fm-muted" id="fm-software-move-status" role="status" aria-live="polite">系统目录、商店应用、位置不明或受保护的软件不会执行搬家。</p>
  </div><aside class="fm-panel"><h3>搬家说明</h3><p class="fm-help">搬家会把软件文件复制到目标磁盘并校验内容，再将原位置替换成目录联接。软件仍会从原路径启动；如果文件被占用、校验失败或 Windows 权限不足，操作会停止并尽量恢复原目录。</p><p class="fm-help">只能迁移另一块本机磁盘上的普通桌面软件目录。系统组件、Microsoft Store 应用、公共组件和无法确认安装位置的软件会禁用。</p><p class="fm-help">此操作需要访问安装目录。权限不足时，请按提示以管理员身份重新启动 LxUltra。</p></aside></div>`;

  section.classList.remove('fm-layout');
  section.append(nav, manage, move);

  const dialog = document.createElement('div');
  dialog.id = 'fm-software-move-dialog';
  dialog.className = 'fm-software-move-dialog';
  dialog.hidden = true;
  dialog.setAttribute('role', 'dialog');
  dialog.setAttribute('aria-modal', 'true');
  dialog.setAttribute('aria-labelledby', 'fm-software-move-dialog-title');
  dialog.innerHTML = `<div class="fm-software-move-dialog-card"><button class="fm-software-move-dialog-close" id="fm-software-move-dialog-close" type="button" aria-label="关闭">×</button><h2 id="fm-software-move-dialog-title">确认软件搬家</h2><p class="fm-help">请核对软件、原安装位置和目标位置。搬家期间请关闭该软件；搬家完成后会保留原路径联接。</p><dl><dt>软件</dt><dd id="fm-software-move-dialog-name"></dd><dt>原位置</dt><dd id="fm-software-move-source"></dd><dt>目标位置</dt><dd id="fm-software-move-destination"></dd></dl><p class="fm-software-move-warning">确认后将搬动此软件文件；系统目录和无法确认位置的软件不会执行。</p><div class="fm-toolbar fm-software-move-dialog-actions"><button class="btn" id="fm-software-move-cancel" type="button">取消</button><button class="btn danger" id="fm-software-move-confirm" type="button">确认搬家</button></div><p class="fm-muted" id="fm-software-move-dialog-status" role="status" aria-live="polite"></p></div>`;
  document.body.append(dialog);

  let activeView = 'manage';
  let items = [];
  let targetRoot = '';
  let pending = null;
  let busy = false;
  let scanBusy = false;
  let moveComplete = false;
  let moveTimer = null;
  let moveStartedAt = 0;
  let iconMap = new Map();

  function updateMoveButtons() {
    move.querySelectorAll('[data-software-move]').forEach(button => {
      const item = items.find(entry => entry.id === button.dataset.softwareMove);
      button.disabled = busy || !targetRoot || !item?.canMove;
    });
  }

  function renderMoveItems() {
    const list = document.getElementById('fm-software-move-list');
    if (!items.length) {
      list.innerHTML = `<p class="fm-software-move-empty">${scanBusy ? '正在读取软件清单…' : '未发现可显示的软件。'}</p>`;
      updateMoveButtons();
      return;
    }
    list.innerHTML = items.map(item => {
      const canMove = !!item.canMove;
      const icon = iconMap.get(item.id);
      const name = item.name || '未命名软件';
      const reason = item.moveReason || '此软件的安装位置不符合安全迁移条件';
      return `<div class="fm-software-move-row" role="row"><span class="fm-software-move-name" role="cell">${icon ? `<img class="fm-software-move-icon" src="${e(icon)}" alt="">` : `<span class="fm-software-move-icon fm-software-move-icon-fallback" aria-hidden="true">${e(name.trim().slice(0, 1) || '软')}</span>`}<span><strong>${e(name)}</strong><small>${e(item.publisher || '发布者未提供')}${item.version ? ` · ${e(item.version)}` : ''}</small><small title="${e(item.installLocation || reason)}">${e(item.installLocation || reason)}</small></span></span><span role="cell">${item.sizeBytes ? e(bytes(item.sizeBytes)) : '未提供'}</span><span role="cell"><button type="button" class="btn small" data-software-move="${e(item.id)}" ${canMove && targetRoot && !busy ? '' : 'disabled'} title="${canMove ? (targetRoot ? '预览并搬家' : '请先选择目标文件夹') : e(reason)}">搬家</button></span></div>`;
    }).join('');
    updateMoveButtons();
  }

  async function loadMoveItems() {
    if (scanBusy || !api()) return;
    scanBusy = true;
    const refresh = document.getElementById('fm-software-move-refresh');
    refresh.disabled = true;
    document.getElementById('fm-software-move-summary').textContent = '正在读取 Windows 软件清单…';
    document.getElementById('fm-software-move-list').setAttribute('aria-busy', 'true');
    try {
      const result = await api().fm_software_list();
      if (!result?.ok || !Array.isArray(result.items)) throw new Error(result?.error || '软件清单格式无效');
      items = result.items;
      iconMap = new Map();
      renderMoveItems();
      const eligible = items.filter(item => item.canMove).length;
      document.getElementById('fm-software-move-summary').textContent = `发现 ${items.length} 款软件，其中 ${eligible} 款符合搬家条件 · 搬家前逐项确认`;
      const iconResult = await api().fm_software_icons(items.slice(0, 48).map(item => item.id));
      if (iconResult?.ok && iconResult.icons && typeof iconResult.icons === 'object') {
        for (const [identifier, value] of Object.entries(iconResult.icons)) {
          if (items.some(item => item.id === identifier) && typeof value === 'string' && value.startsWith('data:image/png;base64,')) iconMap.set(identifier, value);
        }
        renderMoveItems();
      }
    } catch (error) {
      console.error('[LxUltra] software move list unavailable', error);
      items = [];
      renderMoveItems();
      document.getElementById('fm-software-move-summary').textContent = '暂时无法读取软件清单，请检查应用权限后重试。';
    } finally {
      scanBusy = false;
      refresh.disabled = false;
      document.getElementById('fm-software-move-list').removeAttribute('aria-busy');
    }
  }

  function showView(view) {
    activeView = view === 'move' ? 'move' : 'manage';
    nav.querySelectorAll('[data-software-view]').forEach(button => {
      const selected = button.dataset.softwareView === activeView;
      button.classList.toggle('active', selected);
      button.setAttribute('aria-selected', String(selected));
      button.tabIndex = selected ? 0 : -1;
    });
    manage.classList.toggle('fm-hidden', activeView !== 'manage');
    move.classList.toggle('fm-hidden', activeView !== 'move');
    if (activeView === 'move') loadMoveItems();
  }

  function openMovePreview(item) {
    if (!item?.canMove || !targetRoot || busy) return;
    pending = item;
    moveComplete = false;
    const leaf = String(item.installLocation || '').replace(/[\\/]+$/, '').split(/[\\/]/).pop();
    const separator = targetRoot.includes('\\') ? '\\' : '/';
    document.getElementById('fm-software-move-dialog-name').textContent = `${item.name || '未命名软件'}${item.version ? ` · ${item.version}` : ''}`;
    document.getElementById('fm-software-move-source').textContent = item.installLocation || '未提供';
    document.getElementById('fm-software-move-destination').textContent = `${targetRoot.replace(/[\\/]+$/, '')}${separator}${leaf}`;
    document.getElementById('fm-software-move-dialog-status').textContent = '';
    dialog.hidden = false;
    dialog.classList.add('is-visible');
    document.getElementById('fm-software-move-cancel').focus();
  }

  function closeMovePreview() {
    if (busy) return;
    dialog.classList.remove('is-visible');
    dialog.hidden = true;
    pending = null;
    moveComplete = false;
    const confirm = document.getElementById('fm-software-move-confirm');
    confirm.textContent = '确认搬家';
    confirm.classList.add('danger');
    confirm.classList.remove('primary');
  }

  nav.addEventListener('click', event => {
    const button = event.target.closest('[data-software-view]');
    if (button) showView(button.dataset.softwareView);
  });
  document.querySelector('.fm-tabs [data-fm-tab="software"]')?.addEventListener('click', () => showView('manage'));
  document.getElementById('fm-software-move-refresh').addEventListener('click', loadMoveItems);
  document.getElementById('fm-software-move-target').addEventListener('click', async event => {
    if (!api() || busy) return;
    const button = event.currentTarget;
    button.disabled = true;
    try {
      const paths = await api().fm_choose('folder', false);
      if (Array.isArray(paths) && paths.length) {
        targetRoot = paths[0];
        document.getElementById('fm-software-move-target-path').textContent = targetRoot;
        updateMoveButtons();
      }
    } catch (error) {
      console.error('[LxUltra] software move target picker failed', error);
      status('无法打开目标文件夹选择器，请稍后重试。');
    } finally { button.disabled = false; }
  });
  document.getElementById('fm-software-move-list').addEventListener('click', event => {
    const button = event.target.closest('[data-software-move]');
    if (!button) return;
    openMovePreview(items.find(item => item.id === button.dataset.softwareMove));
  });
  document.getElementById('fm-software-move-dialog-close').addEventListener('click', closeMovePreview);
  document.getElementById('fm-software-move-cancel').addEventListener('click', closeMovePreview);
  dialog.addEventListener('click', event => { if (event.target === dialog) closeMovePreview(); });
  document.addEventListener('keydown', event => { if (event.key === 'Escape' && !dialog.hidden) closeMovePreview(); });
  document.getElementById('fm-software-move-confirm').addEventListener('click', async () => {
    if (moveComplete) { closeMovePreview(); return; }
    if (!pending || !targetRoot || !api() || busy) return;
    busy = true;
    updateMoveButtons();
    const confirm = document.getElementById('fm-software-move-confirm');
    const line = document.getElementById('fm-software-move-dialog-status');
    confirm.disabled = true;
    confirm.textContent = '正在搬家并校验…';
    moveStartedAt = Date.now();
    dialog.classList.add('is-busy');
    dialog.setAttribute('aria-busy', 'true');
    line.textContent = '正在复制并校验软件文件；耗时取决于软件大小和磁盘速度。';
    moveTimer = window.setInterval(() => {
      const seconds = Math.floor((Date.now() - moveStartedAt) / 1000);
      line.textContent = `正在复制并校验软件文件 · 已用时 ${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
    }, 1000);
    try {
      const result = await api().fm_software_move(pending.id, targetRoot);
      if (!result?.ok) throw new Error(result?.error || '软件搬家未完成。');
      line.textContent = `搬家完成：${result.destination || targetRoot}${result.warning ? `；${result.warning}` : ''}`;
      status(`“${pending.name}”搬家完成。请从原快捷方式启动，核对正常后再关闭此提示。`);
      confirm.textContent = '完成';
      confirm.disabled = false;
      confirm.classList.remove('danger');
      confirm.classList.add('primary');
      moveComplete = true;
      await loadMoveItems();
    } catch (error) {
      console.error('[LxUltra] software move failed', error);
      line.textContent = error?.message || '搬家失败，原软件目录已保留。';
      confirm.textContent = '重试确认';
      confirm.disabled = false;
      status(line.textContent);
    } finally {
      clearInterval(moveTimer);
      moveTimer = null;
      dialog.classList.remove('is-busy');
      dialog.setAttribute('aria-busy', 'false');
      busy = false;
      updateMoveButtons();
    }
  });

  section.insertBefore(nav, manage);
})();
