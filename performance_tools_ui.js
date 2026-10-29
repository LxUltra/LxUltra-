(function () {
  const $ = id => document.getElementById(id);
  const api = () => window.pywebview?.api;
  const escape = value => window.escapeHtml ? window.escapeHtml(value ?? '') : String(value ?? '');
  const bytes = value => window.fmtBytes ? window.fmtBytes(value) : '—';
  let cdrivePolling = false, cdriveScanStartedAt = 0, cdriveProgressPercent = 0, cdriveSuccessReturnFocus = null;
  let cdriveItems = [], cdriveSelected = new Set(), cdriveAIExclusions = [], cdriveScanTimer = null, cdriveScanBusy = false, cdriveOwnsScan = false, cdriveCancelRequested = false, cdriveCancelRequestedAt = 0, cdriveCleanupBusy = false;
  let cdriveSoftwareItems = [], cdriveAppIconMap = new Map(), cdriveAppIconsLoaded = false, cdriveAppIconsLoading = false;
  const cdriveName = value => String(value || '').replace(/[\\/]+$/, '').toUpperCase();
  const cdriveCategory = {temporary: '系统不常用文件', installers: '下载的安装包', large: '大文件', ai: 'AI 缓存与项目'};
  const cdriveGroups = [
    {key: 'system', label: '系统不常用文件', icon: '🧹'},
    {key: 'software', label: '软件缓存', icon: '🧩'},
    {key: 'social', label: '社交软件', icon: '👥'},
    {key: 'ai', label: 'AI 缓存与项目', icon: '🤖'},
    {key: 'installers', label: '下载的安装包', icon: '📦'},
    {key: 'large', label: '1 GB 以上的大文件', icon: '📁'},
  ];

  const cdriveKnownAppAliases = [
    {match: /(?:microsoftedge|msedge|edge)/, aliases: ['microsoftedge', 'msedge', 'edge', '微软edge']},
    {match: /(?:googlechrome|chrome)/, aliases: ['googlechrome', 'chrome', '谷歌浏览器']},
    {match: /(?:wechat|weixin|微信)/, aliases: ['wechat', 'weixin', '微信']},
    {match: /(?:baidunetdisk|baiduyun|百度网盘)/, aliases: ['baidunetdisk', 'baiduyun', '百度网盘']},
    {match: /(?:wps|kingsoft|金山)/, aliases: ['wpsoffice', 'wps', 'kingsoft', '金山']},
    {match: /(?:tencentqq|qqnt|qq)/, aliases: ['tencentqq', 'qqnt', 'qq']},
    {match: /(?:dingtalk|钉钉)/, aliases: ['dingtalk', '钉钉']},
    {match: /(?:feishu|lark|飞书)/, aliases: ['feishu', 'lark', '飞书']},
  ];

  function cdriveCompact(value) {
    return String(value || '').toLocaleLowerCase().replace(/[^a-z0-9\u4e00-\u9fff]+/g, '');
  }

  function cdriveAppFor(item) {
    const path = cdriveCompact(item?.path);
    if (!path) return null;
    return cdriveSoftwareItems.find(app => {
      const name = cdriveCompact(app.name);
      if (name.length >= 4 && path.includes(name)) return true;
      const mapping = cdriveKnownAppAliases.find(entry => entry.match.test(name));
      return !!mapping && mapping.aliases.some(alias => path.includes(cdriveCompact(alias)));
    }) || null;
  }

  async function loadCDriveSoftwareIcons() {
    if (cdriveAppIconsLoaded || cdriveAppIconsLoading || !api()?.fm_software_list || !cdriveItems.length) return;
    cdriveAppIconsLoading = true;
    try {
      const result = await api().fm_software_list();
      if (!result?.ok || !Array.isArray(result.items)) return;
      cdriveSoftwareItems = result.items;
      const relevant = cdriveSoftwareItems.filter(app => cdriveItems.some(item => cdriveAppFor(item)?.id === app.id)).slice(0, 48);
      if (relevant.length && api()?.fm_software_icons) {
        const icons = await api().fm_software_icons(relevant.map(app => app.id));
        if (icons?.ok && icons.icons && typeof icons.icons === 'object') {
          for (const app of relevant) {
            const icon = icons.icons[app.id];
            if (typeof icon === 'string' && icon.startsWith('data:image/png;base64,')) cdriveAppIconMap.set(app.id, icon);
          }
        }
      }
      cdriveAppIconsLoaded = true;
      renderCDriveResults();
    } catch (error) {
      console.debug('[LxUltra] C-drive cleanup app icons unavailable', error);
    } finally { cdriveAppIconsLoading = false; }
  }

  function cdriveGroupFor(item) {
    const path = `\\${String(item.path || '').replace(/\//g, '\\').toLowerCase()}\\`;
    const category = String(item.category || '');
    if (category === 'ai' || item.ai_recognized) return 'ai';
    const cached = /\\(?:cache|caches|temp|tmp|logs?|crash|code cache|gpu cache|shadercache)\\/i.test(path);
    if (category === 'installers') return 'installers';
    if (category === 'large') return 'large';
    if (category === 'temporary' && cached && /\\(?:wechat(?: files)?|weixin(?: files)?|微信|qq|qqnt|dingtalk|wecom|wxwork|feishu|lark|teams)\\/i.test(path)) return 'social';
    if (category === 'temporary' && cached && /\\(?:appdata|programdata|chrome|edge|firefox|msedge|browser)\\/i.test(path)) return 'software';
    return 'system';
  }

  function cdriveVisibleItems() {
    const controls = [...document.querySelectorAll('[data-cdrive-category-toggle]')];
    const enabled = controls.length
      ? new Set(controls.filter(control => control.checked).map(control => control.dataset.cdriveCategoryToggle))
      : new Set(cdriveGroups.map(group => group.key));
    return cdriveItems.filter(item => enabled.has(cdriveGroupFor(item))).slice(0, 500);
  }

  function cdriveCandidatesFromState(state) {
    const regular = Array.isArray(state?.items) ? state.items : [];
    const ai = Array.isArray(state?.ai_detected_items) ? state.ai_detected_items : [];
    return [...regular, ...ai].filter(item => cdriveName(item?.drive) === 'C:');
  }

  function setCDriveProgress(percent, label, detail) {
    const value = Math.max(0, Math.min(100, Math.round(Number(percent) || 0)));
    const track = $('cdrive-scan-progress-track');
    const bar = $('cdrive-scan-progress-bar');
    const number = $('cdrive-scan-percent');
    if (number) number.textContent = `${value}%`;
    if (bar) bar.style.width = `${value}%`;
    if (track) { track.setAttribute('aria-valuenow', String(value)); track.setAttribute('aria-valuetext', `${label}，估算 ${value}%`); }
    if ($('cdrive-scan-progress-label')) $('cdrive-scan-progress-label').textContent = label;
    if ($('cdrive-scan-progress-detail')) $('cdrive-scan-progress-detail').textContent = detail || '';
    cdriveProgressPercent = Math.max(cdriveProgressPercent, value);
  }

  function estimatedCDriveProgress(scanned) {
    const count = Math.max(0, Number(scanned) || 0);
    return Math.min(94, Math.max(2, Math.round(95 * (1 - Math.exp(-count / 22000)))));
  }

  function cdriveElapsedText() {
    const seconds = Math.max(0, Math.floor((Date.now() - cdriveScanStartedAt) / 1000));
    return `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
  }

  function showCDriveCleanupSuccess(moved, size, failed) {
    const dialog = $('cdrive-clean-success');
    if (!dialog) return;
    cdriveSuccessReturnFocus = document.activeElement;
    $('cdrive-clean-success-size').textContent = bytes(size);
    $('cdrive-clean-success-detail').textContent = failed
      ? `已移入回收站 ${moved} 个项目，${failed} 个项目未处理。移入回收站的文件可恢复。`
      : `已将 ${moved} 个项目移入回收站，仍可从 Windows 回收站恢复。`;
    dialog.hidden = false;
    dialog.classList.remove('is-visible');
    requestAnimationFrame(() => dialog.classList.add('is-visible'));
    $('cdrive-clean-success-close')?.focus();
  }

  function closeCDriveCleanupSuccess() {
    const dialog = $('cdrive-clean-success');
    if (!dialog || dialog.hidden) return;
    dialog.classList.remove('is-visible');
    dialog.hidden = true;
    cdriveSuccessReturnFocus?.focus?.();
  }

  function renderCDriveAIExclusions() {
    const list = $('cdrive-ai-exclusion-list');
    if (!list) return;
    list.replaceChildren();
    if (!cdriveAIExclusions.length) {
      const empty = document.createElement('p');
      empty.className = 'note';
      empty.textContent = '尚未添加自建项目目录；常见 AI 软件和模型目录仍会自动排除。';
      list.append(empty);
      return;
    }
    for (const path of cdriveAIExclusions) {
      const row = document.createElement('div');
      row.className = 'cdrive-ai-exclusion-row';
      row.setAttribute('role', 'listitem');
      const label = document.createElement('span');
      label.textContent = path;
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'btn small';
      remove.dataset.cdriveAiExclusionRemove = path;
      remove.textContent = '移除';
      row.append(label, remove);
      list.append(row);
    }
  }

  function renderCDriveAIDetectedDirectories(directories) {
    const list = $('cdrive-ai-detected-list');
    if (!list) return;
    list.replaceChildren();
    const selectable = Array.isArray(directories) ? directories.filter(item => item && item.source !== 'custom') : [];
    if (!selectable.length) {
      const empty = document.createElement('p');
      empty.className = 'note';
      empty.textContent = '本次扫描未发现 AI 候选；你添加的自建保护目录会在下方单独显示。';
      list.append(empty);
      return;
    }
    for (const directory of selectable) {
      if (!directory || typeof directory.path !== 'string') continue;
      const row = document.createElement('div');
      row.className = 'cdrive-ai-detected-row';
      row.setAttribute('role', 'listitem');
      const path = document.createElement('span');
      path.className = 'cdrive-ai-detected-path';
      path.textContent = directory.path;
      const summary = document.createElement('span');
      summary.className = 'cdrive-ai-detected-summary';
      const count = Math.max(0, Number(directory.count) || 0);
      summary.textContent = `${count} 个文件 · ${bytes(directory.bytes)}`;
      const source = document.createElement('span');
      source.className = 'cdrive-ai-detected-source';
      source.textContent = directory.source === 'custom' ? '自建保护' : '自动识别';
      row.append(path, summary, source);
      list.append(row);
    }
  }

  async function loadCDriveAIExclusions() {
    if (!api()?.storage_ai_exclusions_list) return;
    const list = $('cdrive-ai-exclusion-list');
    if (!list) return;
    try {
      const result = await api().storage_ai_exclusions_list();
      if (!result?.ok || !Array.isArray(result.paths)) throw new Error(result?.error || '读取失败');
      cdriveAIExclusions = result.paths;
      renderCDriveAIExclusions();
    } catch {
      list.textContent = '暂时无法读取保护目录；自动识别的 AI 目录仍会受到保护。';
    }
  }

  function invalidateCDriveResultsForProtectionChange() {
    cdriveItems = [];
    cdriveSelected.clear();
    renderCDriveAIDetectedDirectories([]);
    if ($('cdrive-scan-results')) $('cdrive-scan-results').hidden = true;
    if ($('cdrive-scan-status')) $('cdrive-scan-status').textContent = 'AI 保护目录已更新，请重新扫描后查看最新清理项目。';
  }

  function updateCDriveSelectionSummary() {
    const visible = cdriveVisibleItems();
    const selected = visible.filter(item => cdriveSelected.has(item.id));
    $('cdrive-scan-count').textContent = `发现 ${cdriveItems.length} 项 · 当前显示 ${visible.length} 项 · 已勾选 ${selected.length} 项${selected.length ? ` · ${bytes(selected.reduce((sum, item) => sum + Number(item.size || 0), 0))}` : ''}`;
    $('cdrive-clean-selected').disabled = !selected.length || cdriveCleanupBusy;
  }

  function renderCDriveResults() {
    const list = $('cdrive-result-list');
    if (!list) return;
    const visible = cdriveVisibleItems();
    if (!visible.length) {
      list.innerHTML = cdriveItems.length
        ? '<p class="note">请勾选至少一个类别查看扫描结果。类别勾选只筛选显示，不会自动勾选文件。</p>'
        : '<p class="note">没有发现可清理项目。AI 缓存、模型和项目目录已自动排除。</p>';
      updateCDriveSelectionSummary();
      return;
    }
    const groups = cdriveGroups.map(group => {
      const items = visible.filter(item => cdriveGroupFor(item) === group.key);
      if (!items.length) return '';
      const total = items.reduce((sum, item) => sum + Math.max(0, Number(item.size) || 0), 0);
      const rows = items.map(item => {
        const path = String(item.path || '');
        const name = path.split(/[\\/]/).pop() || path;
        const app = cdriveAppFor(item), icon = app && cdriveAppIconMap.get(app.id);
        const fileIcon = icon
          ? `<span class="cdrive-file-icon cdrive-file-icon-image" role="img" aria-label="${escape(app.name)}"><img src="${escape(icon)}" alt=""></span>`
          : `<span class="cdrive-file-icon" aria-hidden="true">${group.icon}</span>`;
        return `<label class="cdrive-result-row"><input type="checkbox" data-cdrive-id="${escape(item.id)}" ${cdriveSelected.has(item.id) ? 'checked' : ''} ${cdriveCleanupBusy ? 'disabled' : ''}>${fileIcon}<span><strong>${escape(name)}</strong><small>${escape(bytes(item.size))} · ${escape(path)}</small></span></label>`;
      }).join('');
      return `<section class="cdrive-result-group" data-cdrive-group="${group.key}"><header><span class="cdrive-category-icon" aria-hidden="true">${group.icon}</span><strong>${group.label}</strong><span>${items.length} 项 · ${escape(bytes(total))}</span></header>${rows}</section>`;
    }).join('');
    list.innerHTML = groups || '<p class="note">没有发现可清理项目。</p>';
    if (cdriveItems.length > visible.length) list.innerHTML += `<p class="note">当前显示 ${visible.length} 项；其余项目未勾选。类别筛选不会自动选择文件。</p>`;
    updateCDriveSelectionSummary();
  }

  async function pollCDriveScan() {
    if (!api() || cdrivePolling) return;
    cdrivePolling = true;
    try {
      const state = await api().storage_scan_status();
      const status = $('cdrive-scan-status');
      const progress = $('cdrive-scan-progress');
      const runningC = state.running && (state.drives || []).some(drive => cdriveName(drive) === 'C:');
      if (runningC) {
        progress.hidden = false;
        progress.classList.add('is-running');
        progress.setAttribute('aria-busy', 'true');
        const scanned = Math.max(0, Number(state.scanned) || 0);
        const estimate = Math.max(cdriveProgressPercent, estimatedCDriveProgress(scanned));
        const folder = state.folder ? ` · 当前目录：${state.folder}` : '';
        setCDriveProgress(estimate, '正在扫描 C 盘缓存和可清理文件', `已检查 ${scanned.toLocaleString()} 个文件 · 已用时 ${cdriveElapsedText()} · 百分比为扫描估算，不代表剩余时间${folder}`);
        $('cdrive-scan').disabled = true;
        const cancel = $('cdrive-cancel');
        const retryCancel = cdriveCancelRequested && Date.now() - cdriveCancelRequestedAt >= 8000;
        if (retryCancel) {
          cdriveCancelRequested = false;
          cdriveCancelRequestedAt = 0;
          if (status) status.textContent = '扫描仍在运行，取消尚未确认，可以再次尝试。';
        }
        if (cancel) {
          cancel.hidden = false;
          cancel.disabled = cdriveCancelRequested;
          cancel.textContent = cdriveCancelRequested ? '正在取消…' : '取消扫描';
        }
        if (!cdriveScanTimer) cdriveScanTimer = window.setInterval(pollCDriveScan, 1000);
        return;
      }
      if (state.running) {
        if (status) status.textContent = '另一项磁盘扫描正在进行，请结束后再扫描 C 盘。';
        progress.classList.remove('is-running');
        progress.setAttribute('aria-busy', 'false');
        if ($('cdrive-cancel')) { $('cdrive-cancel').hidden = true; $('cdrive-cancel').disabled = true; }
        if (cdriveScanTimer) { clearInterval(cdriveScanTimer); cdriveScanTimer = null; }
        return;
      }
      if (cdriveScanTimer) { clearInterval(cdriveScanTimer); cdriveScanTimer = null; }
      $('cdrive-scan').disabled = false;
      cdriveCancelRequested = false;
      cdriveCancelRequestedAt = 0;
      if ($('cdrive-cancel')) { $('cdrive-cancel').hidden = true; $('cdrive-cancel').disabled = true; $('cdrive-cancel').textContent = '取消扫描'; }
      progress.classList.remove('is-running');
      progress.setAttribute('aria-busy', 'false');
      if (!cdriveOwnsScan && !state.cancelled && !state.items?.length) {
        progress.hidden = true;
        return;
      }
      cdriveItems = cdriveCandidatesFromState(state);
      cdriveSelected.clear();
      renderCDriveAIDetectedDirectories(state.ai_detected_directories || state.ai_protected_directories);
      $('cdrive-scan-results').hidden = false;
      renderCDriveResults();
      loadCDriveSoftwareIcons();
      const detectedCount = Math.max(0, Number(state.ai_detected_count) || state.ai_detected_items?.length || 0);
      const protectedCount = Math.max(0, Number(state.ai_protected_count) || 0);
      const protectionNote = detectedCount
        ? `；识别到 ${detectedCount} 个 AI 文件，可在“AI 缓存与项目”中逐项勾选（默认不勾选）`
        : protectedCount ? `；${protectedCount} 个文件位于自建保护目录，已跳过` : '；AI 项目类别已就绪，扫描文件默认不勾选';
      if (status) status.textContent = state.cancelled
        ? `扫描已取消，找到 ${cdriveItems.length} 项；默认均未勾选${protectionNote}`
        : state.error
          ? `扫描未完成，请检查 C 盘访问权限后重试${protectionNote}`
          : `扫描完成，找到 ${cdriveItems.length} 项可清理候选；默认均未勾选${protectionNote}`;
      const finishedLabel = state.cancelled ? '扫描已取消' : state.error ? '扫描未完成' : '扫描完成';
      const finishedPercent = state.cancelled || state.error ? cdriveProgressPercent : 100;
      setCDriveProgress(finishedPercent, finishedLabel, `已检查 ${Math.max(0, Number(state.scanned) || 0).toLocaleString()} 个文件 · AI 候选可单独查看并勾选`);
      progress.hidden = false;
      cdriveOwnsScan = false;
    } catch {
      if ($('cdrive-scan-status')) $('cdrive-scan-status').textContent = '无法读取扫描进度，请稍后重试。';
    } finally { cdrivePolling = false; }
  }

  async function startCDriveScan() {
    if (!api() || cdriveScanBusy) return;
    cdriveScanBusy = true;
    cdriveCancelRequested = false;
    cdriveCancelRequestedAt = 0;
    if ($('cdrive-cancel')) { $('cdrive-cancel').hidden = true; $('cdrive-cancel').disabled = true; $('cdrive-cancel').textContent = '取消扫描'; }
    $('cdrive-scan').disabled = true;
    $('cdrive-scan-results').hidden = true;
    $('cdrive-scan-progress').hidden = false;
      $('cdrive-scan-progress').classList.add('is-running');
    $('cdrive-scan-progress').setAttribute('aria-busy', 'true');
    cdriveScanStartedAt = Date.now();
    cdriveProgressPercent = 0;
    setCDriveProgress(0, '正在准备扫描 C 盘', '正在连接只读扫描器…');
    $('cdrive-scan-status').textContent = '扫描只读取文件信息，不会自动删除。';
    cdriveItems = [];
    cdriveSelected.clear();
    renderCDriveAIDetectedDirectories([]);
    try {
      const drives = await api().storage_drives();
      const drive = (drives || []).find(item => cdriveName(item) === 'C:');
      if (!drive) throw new Error('未检测到可扫描的 C 盘。');
      const result = await api().storage_scan_start(drive);
      if (!result?.ok) {
        const active = await api().storage_scan_status();
        if (active.running && (active.drives || []).some(item => cdriveName(item) === 'C:')) {
          cdriveOwnsScan = false;
          $('cdrive-scan-status').textContent = 'C 盘扫描已在进行中，正在显示当前进度。';
          await pollCDriveScan();
          return;
        }
        throw new Error(result?.error || '无法启动 C 盘扫描。');
      }
      cdriveOwnsScan = true;
      await pollCDriveScan();
    } catch (error) {
      $('cdrive-scan').disabled = false;
      $('cdrive-scan-progress').hidden = true;
      $('cdrive-scan-progress').classList.remove('is-running');
      $('cdrive-scan-progress').setAttribute('aria-busy', 'false');
      if ($('cdrive-cancel')) { $('cdrive-cancel').hidden = true; $('cdrive-cancel').disabled = true; }
      $('cdrive-scan-status').textContent = error?.message || '无法启动 C 盘扫描，请稍后重试。';
    } finally {
      cdriveScanBusy = false;
    }
  }

  async function cleanCDriveSelection() {
    if (!api() || cdriveCleanupBusy) return;
    const selected = cdriveItems.filter(item => cdriveSelected.has(item.id));
    if (!selected.length) return;
    const total = selected.reduce((sum, item) => sum + Number(item.size || 0), 0);
    const aiSelected = selected.filter(item => cdriveGroupFor(item) === 'ai');
    const aiWarning = aiSelected.length ? `其中 ${aiSelected.length} 个 AI 缓存、模型或项目文件可能影响相关软件或项目，确认后将移入回收站。\n\n` : '';
    if (!await window.askConfirmation('将所选文件移入回收站？', `${aiWarning}仅处理已勾选的 ${selected.length} 个 C 盘文件，共 ${bytes(total)}。文件会移入 Windows 回收站，可恢复。`, '移入回收站')) return;
    cdriveCleanupBusy = true;
    renderCDriveResults();
    const result = $('cdrive-clean-result');
    try {
      const response = await api().storage_cleanup(selected.map(item => item.id), aiSelected.length > 0);
      if (!response?.ok) throw new Error('清理未完成，请重新扫描后核对文件。');
      const moved = Number(response.moved) || 0, failed = Number(response.failed) || 0;
      result.textContent = `已移入回收站 ${moved} 个，失败 ${failed} 个，共 ${bytes(response.bytes)}。`;
      result.hidden = false;
      cdriveSelected.clear();
      const latest = await api().storage_scan_status();
      cdriveItems = cdriveCandidatesFromState(latest);
      $('cdrive-scan-status').textContent = '清理结果已更新；如文件仍显示，请重新扫描确认。';
      if (moved > 0) showCDriveCleanupSuccess(moved, Number(response.bytes) || total, failed);
    } catch (error) {
      result.textContent = error?.message || '清理结果暂时无法确认，请重新扫描核对。';
      result.hidden = false;
    } finally {
      cdriveCleanupBusy = false;
      renderCDriveResults();
    }
  }

  async function openCDriveWindowsPage(target, statusId, label) {
    const status = $(statusId);
    const button = target === 'apps' ? $('cdrive-open-uninstall') : $('cdrive-open-storage');
    if (!api()) {
      if (status) status.textContent = 'Windows 设置暂不可用。';
      return;
    }
    if (button) button.disabled = true;
    if (status) status.textContent = `正在打开 Windows ${label}…`;
    try {
      const result = await api().open_system_settings(target);
      if (status) status.textContent = result.ok
        ? `已打开 Windows ${label}。具体可清理项目或软件占用以系统显示为准。`
        : result.error || `无法打开 Windows ${label}。`;
    } catch {
      if (status) status.textContent = `无法打开 Windows ${label}，请从系统设置中手动进入。`;
    } finally {
      if (button) button.disabled = false;
    }
  }

  function initialize() {
    $('cdrive-open-uninstall')?.addEventListener('click', () => openCDriveWindowsPage('apps', 'cdrive-uninstall-status', '应用'));
    $('cdrive-open-storage')?.addEventListener('click', () => openCDriveWindowsPage('storage', 'cdrive-settings-status', '存储'));
    $('cdrive-scan')?.addEventListener('click', startCDriveScan);
    $('cdrive-result-list')?.addEventListener('change', event => {
      const box = event.target.closest('[data-cdrive-id]');
      if (!box) return;
      if (box.checked) cdriveSelected.add(box.dataset.cdriveId);
      else cdriveSelected.delete(box.dataset.cdriveId);
      updateCDriveSelectionSummary();
    });
    $('cdrive-category-list')?.addEventListener('change', event => {
      const control = event.target.closest('[data-cdrive-category-toggle]');
      if (!control) return;
      if (!control.checked) {
        const hiddenGroup = control.dataset.cdriveCategoryToggle;
        cdriveItems.forEach(item => {
          if (cdriveGroupFor(item) === hiddenGroup) cdriveSelected.delete(item.id);
        });
      }
      renderCDriveResults();
    });
    $('cdrive-cancel')?.addEventListener('click', async () => {
      if (!api() || cdriveCancelRequested) return;
      cdriveCancelRequested = true;
      cdriveCancelRequestedAt = Date.now();
      $('cdrive-cancel').disabled = true;
      $('cdrive-cancel').textContent = '正在取消…';
      $('cdrive-scan-status').textContent = '正在取消 C 盘扫描…';
      try {
        const result = await api().storage_scan_cancel();
        if (result === false || result?.ok === false) throw new Error(result?.error || '扫描器未接受取消请求');
        await pollCDriveScan();
      } catch (error) {
        cdriveCancelRequested = false;
        cdriveCancelRequestedAt = 0;
        $('cdrive-cancel').disabled = false;
        $('cdrive-cancel').textContent = '取消扫描';
        $('cdrive-scan-status').textContent = `取消失败，可重试：${error?.message || '扫描器暂时无响应'}`;
        await pollCDriveScan();
      }
    });
    $('cdrive-select-all')?.addEventListener('click', () => {
      cdriveVisibleItems().forEach(item => cdriveSelected.add(item.id));
      renderCDriveResults();
    });
    $('cdrive-clear-selection')?.addEventListener('click', () => {
      cdriveSelected.clear();
      renderCDriveResults();
    });
    $('cdrive-clean-selected')?.addEventListener('click', cleanCDriveSelection);
    $('cdrive-ai-exclusion-add')?.addEventListener('click', async event => {
      const button = event.currentTarget;
      const line = $('cdrive-ai-exclusion-status');
      if (!api()?.fm_choose || !api()?.storage_ai_exclusions_add || cdriveScanBusy) {
        if (line) line.textContent = cdriveScanBusy ? '扫描期间不能更改保护目录。' : '目录选择器暂不可用。';
        return;
      }
      button.disabled = true;
      if (line) line.textContent = '请选择 AI 项目文件夹…';
      try {
        const selected = await api().fm_choose('folder', false);
        if (!Array.isArray(selected) || !selected.length) {
          if (line) line.textContent = '未添加目录。';
          return;
        }
        const result = await api().storage_ai_exclusions_add(selected[0]);
        if (!result?.ok) throw new Error(result?.error || '无法添加保护目录。');
        cdriveAIExclusions = Array.isArray(result.paths) ? result.paths : cdriveAIExclusions;
        renderCDriveAIExclusions();
        if (line) line.textContent = result.message || '目录已受到保护。';
        invalidateCDriveResultsForProtectionChange();
      } catch (error) {
        if (line) line.textContent = error?.message || '无法添加保护目录，请重试。';
      } finally { button.disabled = false; }
    });
    $('cdrive-ai-exclusion-list')?.addEventListener('click', async event => {
      const button = event.target.closest('[data-cdrive-ai-exclusion-remove]');
      if (!button || !api()?.storage_ai_exclusions_remove || cdriveScanBusy) return;
      button.disabled = true;
      const path = button.dataset.cdriveAiExclusionRemove;
      const line = $('cdrive-ai-exclusion-status');
      try {
        const result = await api().storage_ai_exclusions_remove(path);
        if (!result?.ok) throw new Error(result?.error || '无法移除保护目录。');
        cdriveAIExclusions = Array.isArray(result.paths) ? result.paths : [];
        renderCDriveAIExclusions();
        if (line) line.textContent = result.message || '已更新保护目录。';
        invalidateCDriveResultsForProtectionChange();
      } catch (error) {
        if (line) line.textContent = error?.message || '无法移除保护目录，请重试。';
        button.disabled = false;
      }
    });
    loadCDriveAIExclusions();
    $('cdrive-clean-success-close')?.addEventListener('click', closeCDriveCleanupSuccess);
    $('cdrive-clean-success')?.addEventListener('click', event => { if (event.target.id === 'cdrive-clean-success') closeCDriveCleanupSuccess(); });
    $('cdrive-success-continue')?.addEventListener('click', () => { closeCDriveCleanupSuccess(); startCDriveScan(); });
    $('cdrive-success-migrate')?.addEventListener('click', () => {
      closeCDriveCleanupSuccess();
      if (typeof window.showPane === 'function') window.showPane('filemanager');
      window.fileManagerOpenTab?.('migrate');
    });
    window.addEventListener('keydown', event => { if (event.key === 'Escape' && !$('cdrive-clean-success')?.hidden) closeCDriveCleanupSuccess(); });
  }

  window.LxPerformanceUI = {
    initialize,
  };
}());
