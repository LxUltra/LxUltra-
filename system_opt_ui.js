(() => {
  let catalog = null;
  let busy = false;
  const rootFor = () => document.getElementById('optimization-root');

  function actionText(row, state) {
    if (row.id === 'modern_menu') return state?.enabled ? '切换为传统菜单' : '启用新版菜单';
    if (row.kind === 'toggle') return state?.enabled ? '关闭' : '开启';
    if (row.kind === 'fix') return state?.enabled ? '' : '解除禁用';
    if (row.kind === 'repair') return '开始修复';
    if (row.kind === 'navigate') return '查看详情';
    if (row.kind === 'link') return '打开系统设置';
    return '';
  }

  function stateText(row, state) {
    if (row.id === 'modern_menu') return !state?.supported ? (state?.error || '不支持') : state.enabled ? '当前配置：新版' : '当前配置：传统';
    if (row.kind !== 'toggle' && row.kind !== 'fix') return row.kind === 'info' ? '系统说明' : '可操作';
    if (!state?.supported) return state?.error || '不支持';
    return (state.enabled ? '已开启' : '已关闭') + (state.configured ? '' : ' · 系统默认');
  }

  function render(rows, states) {
    const root = rootFor();
    const preferred = ['桌面与文件', '任务栏', 'Win11 专项', '故障排查', '系统与安全'];
    const groups = [...new Set(rows.map(row => row.group))]
      .sort((a, b) => (preferred.indexOf(a) < 0 ? 99 : preferred.indexOf(a)) - (preferred.indexOf(b) < 0 ? 99 : preferred.indexOf(b)));
    const jumps = document.getElementById('optimization-jumps');
    jumps.innerHTML = groups.map((group, index) => `<button type="button" data-opt-jump="${index}" class="btn small">${escapeHtml(group)}</button>`).join('');
    jumps.querySelectorAll('[data-opt-jump]').forEach(button => {
      button.onclick = () => root.querySelectorAll('.opt-group')[Number(button.dataset.optJump)]?.scrollIntoView({block:'start'});
    });
    root.innerHTML = groups.map(group => {
      const items = rows.filter(row => row.group === group);
      return `<section class="opt-group"><div class="opt-group-head"><h2>${escapeHtml(group)}</h2><span>${items.length} 项</span></div><div class="opt-list">${items.map(row => {
        const state = states[row.id];
        const isUnsupported = (row.kind === 'toggle' || row.kind === 'fix') && !state?.supported;
        const action = actionText(row, state);
        return `<div class="opt-row"><div class="opt-copy"><strong>${escapeHtml(row.title)}</strong><p>${escapeHtml(row.note)}</p></div><div class="opt-controls"><span class="opt-state ${state?.enabled ? 'on' : ''}">${escapeHtml(stateText(row, state))}</span>${action ? `<button class="btn ${row.kind === 'repair' ? 'danger' : ''}" data-opt-id="${escapeHtml(row.id)}" ${isUnsupported ? 'disabled' : ''}>${escapeHtml(action)}</button>` : ''}</div></div>`;
      }).join('')}</div></section>`;
    }).join('');
    root.querySelectorAll('[data-opt-id]').forEach(button => {
      button.onclick = () => runAction(button.dataset.optId, rows.find(row => row.id === button.dataset.optId), states[button.dataset.optId]);
    });
  }

  async function runAction(id, row, state, confirmed = false) {
    if (busy || !api()) return;
    if (id === 'modern_menu' && !confirmed) {
      confirmAction(state?.enabled ? '切换为传统右键菜单' : '启用 Win11 新版右键菜单',
        '将调整当前用户的右键菜单配置。此方法并非 Windows 官方设置开关，系统更新后可能失效；切换后需重启资源管理器或重新登录才能看到效果。',
        () => runAction(id, row, state, true));
      return;
    }
    if (row.kind === 'navigate') {
      const destinations = {slow_pc:'processes', network_issue:'network', component_repair:'systemrepair'};
      showPane(destinations[id] || 'processes');
      return;
    }
    if (row.kind === 'repair') {
      confirmAction('修复 Windows 组件', '将请求管理员权限并打开可见的命令窗口，依次运行 DISM /RestoreHealth 和 sfc /scannow。修复可能耗时较久，也可能需要重启。', async () => {
        const result = await api().optimization_repair_components();
        toast(result.ok ? result.note : result.error, result.ok ? 'good' : 'warn');
      });
      return;
    }
    busy = true;
    const button = rootFor().querySelector(`[data-opt-id="${id}"]`);
    if (button) button.disabled = true;
    try {
      const result = row.kind === 'toggle' || row.kind === 'fix'
        ? await api().optimization_set(id, row.kind === 'fix' ? true : !state.enabled)
        : await api().optimization_open(id);
      toast(result.ok ? (result.note || '已打开 Windows 设置') : result.error, result.ok ? 'good' : 'warn');
      if (row.kind === 'toggle' || row.kind === 'fix') await window.optimizationEnter();
    } catch (error) {
      console.error('[LxUltra] Windows optimization action failed', {id, error});
      toast('这项设置没有完成，请检查权限或刷新设置后重试。', 'bad');
    } finally {
      busy = false;
      if (button && button.isConnected) button.disabled = false;
    }
  }

  window.optimizationEnter = async () => {
    if (!api()) return;
    const root = rootFor();
    root.innerHTML = '<p class="note">正在读取 Windows 当前设置…</p>';
    try {
      if (!catalog) catalog = await api().optimization_catalog();
      const status = await api().optimization_status();
      render(catalog.computer, status.items);
    } catch (error) {
      console.error('[LxUltra] Windows optimization settings unavailable', error);
      root.innerHTML = '<div class="status bad" role="status">无法读取 Windows 当前设置。请重试；设置未作修改。</div><button type="button" class="btn" data-opt-retry>重新读取设置</button>';
      root.querySelector('[data-opt-retry]').onclick = () => window.optimizationEnter();
    }
  };
})();
