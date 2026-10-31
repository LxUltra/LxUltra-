/* LxUltra 电脑管家 · 分区助手前端
 * 对接后端 API(包装器注入): partition_disks / partition_health /
 * partition_job_submit / partition_job_status / partition_admin / partition_vhd_list
 * 设计:待执行操作队列 + 提交(AOMEI 交互),破坏性操作双重确认,系统盘前端同步禁用
 */
(function () {
  'use strict';

  var S = {
    disks: [], admin: false, selDisk: null, selPart: null,
    pending: [], job: null, timer: null, health: {}, loadedAt: 0,
    loading: false, submitting: false, menu: null, status: '', undo: [], redo: [], jobId: null, pollFailures: 0,
    progressPhaseKey: '', progressPhaseStartedAt: 0, modalBusyStartedAt: 0,
    modalBusyTimer: null, migrationPreviewSeq: 0, submittingTimer: null, healthRequestSeq: 0
  };
  var clonePick = null;
  var healthGeneration = 0;
  var rootEl = null;
  var modalResolve = null;
  var modalCancelCallback = null;

  function root() {
    if (!rootEl) rootEl = document.getElementById('partition-root');
    return rootEl;
  }
  function api() {
    return (window.pywebview && window.pywebview.api) || null;
  }
  function esc(v) {
    return String(v == null ? '' : v).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function fmtSize(n) {
    if (n == null || isNaN(n)) return '—';
    var gb = n / 1e9;
    if (gb >= 1000) return (gb / 1000).toFixed(2) + ' TB';
    if (gb >= 1) return gb.toFixed(2) + ' GB';
    return (n / 1e6).toFixed(0) + ' MB';
  }
  function fmtElapsed(ms) {
    var total = Math.max(0, Math.floor(ms / 1000));
    var hours = Math.floor(total / 3600);
    var minutes = Math.floor((total % 3600) / 60);
    var seconds = total % 60;
    function two(n) { return n < 10 ? '0' + n : String(n); }
    return (hours ? two(hours) + ':' : '') + two(minutes) + ':' + two(seconds);
  }
  function pctOf(p) {
    if (!p || !p.size) return 0;
    return Math.min(100, Math.max(0, (p.used / p.size) * 100));
  }
  function diskOf(n) {
    for (var i = 0; i < S.disks.length; i++) if (S.disks[i].number === n) return S.disks[i];
    return null;
  }
  function partOf(diskNum, partNum) {
    var d = diskOf(diskNum);
    if (!d) return null;
    for (var i = 0; i < d.partitions.length; i++) if (d.partitions[i].number === partNum) return d.partitions[i];
    return null;
  }
  function curDisk() { return S.selDisk == null ? null : diskOf(S.selDisk); }
  function curPart() { return (S.selDisk == null || S.selPart == null) ? null : partOf(S.selDisk, S.selPart); }
  function isProtectedPart(d, p) {
    if (!d || !p) return false;
    return !!(p.isBoot || p.isSystem || p.isActive || p.isEFI || p.isMSR || p.isRecovery || p.active);
  }
  function setStatus(text, bad) {
    S.status = text || '';
    var el = document.getElementById('pt-status');
    if (el) {
      el.textContent = S.status;
      el.style.color = bad ? 'var(--bad)' : 'var(--muted)';
    }
  }

  /* ---------------- 渲染 ---------------- */
  function shell() {
    return '' +
      '<div class="pt-root pt-wrap">' +
        '<div id="pt-toolbar-host"></div>' +
        '<div id="pt-progress-host"></div>' +
        '<div class="pt-layout">' +
          '<div class="pt-left-pane">' +
            '<section class="pt-card pt-map-pane"><div class="pt-section-head"><div><h2>磁盘图</h2><p>按容量比例显示分区；条形填充表示已用空间。</p></div><div class="pt-legend"><span><i class="sys"></i>系统</span><span><i class="data"></i>数据</span><span><i class="meta"></i>保留</span><span><i class="free"></i>未分配</span></div></div><div id="pt-disks-host" class="pt-disks"></div></section>' +
            '<section class="pt-card pt-table-pane"><div class="pt-section-head"><div><h2>磁盘与分区</h2><p>可先点上方功能，再在弹窗中选盘；点击磁盘可查看详情。</p></div><span id="pt-load-state"></span></div><div id="pt-table-host"></div></section>' +
          '</div>' +
          '<aside class="pt-side-pane"><div class="pt-card" id="pt-side-host"></div></aside>' +
        '</div>' +
        '<div id="pt-pending-host"></div>' +
        '<p class="pt-status" id="pt-status" role="status" aria-live="polite"></p>' +
      '</div>' +
      '<div class="pt-modal-mask" id="pt-mask"><div class="pt-modal" role="dialog" aria-modal="true"><div id="pt-modal-body"></div></div></div>';
  }

  function renderToolbar() {
    var host = document.getElementById('pt-toolbar-host');
    if (!host) return;
    var submitted = !S.pending.length && !!S.job;
    var submitLabel = S.submitting ? '提交中' : (submitted ? '已提交' : '提交');
    var menus = [
      ['clone', '克隆', '▣', [['migrate-system', '系统克隆（迁移系统）'], ['clone-data', '数据克隆（文件镜像）']]],
      ['convert', '转换', '⇄', [['convert-gpt', '转换为 GPT'], ['convert-mbr', '转换为 MBR']]],
      ['recover', '恢复', '↶', [['op-mbr', '重建 MBR'], ['op-scanfix', '检查并修复分区']]],
      ['erase', '擦除', '▤', [['op-wipe', '擦除硬盘']]],
      ['test', '测试', '⌁', [['op-bench', '磁盘速度测试'], ['op-health', '健康检查 (SMART)']]],
      ['tools', '工具', '▦', [['op-defrag', '碎片整理'], ['op-refresh', '刷新磁盘信息'], ['op-copy', '复制磁盘信息']]],
      ['vhd', '虚拟机管理', '▱', [['vhd-manage', 'VHD 虚拟磁盘管理']]]
    ];
    var h = '<div class="pt-ribbon" role="toolbar" aria-label="分区操作工具栏">' +
      '<div class="pt-ribbon-leading"><button type="button" class="pt-ribbon-submit" data-act="submit"' +
      (!S.pending.length || S.submitting || (S.job && S.job.state === 'running') ? ' disabled' : '') +
      ' aria-label="' + (S.submitting ? '正在提交操作' : (submitted ? '任务已提交' : '提交待执行操作')) + '"><span aria-hidden="true">✓</span><b>' + submitLabel + '</b><small>' + (S.pending.length ? S.pending.length + ' 项' : '') + '</small></button>' +
      '<div class="pt-history"><button type="button" class="pt-icon-btn" data-act="undo" title="撤销 (Ctrl+Z)" aria-label="撤销"' +
      (!S.undo.length ? ' disabled' : '') + '>↶</button><button type="button" class="pt-icon-btn" data-act="redo" title="重做 (Ctrl+Y)" aria-label="重做"' +
      (!S.redo.length ? ' disabled' : '') + '>↷</button></div></div><span class="pt-ribbon-divider"></span><div class="pt-ribbon-groups">';
    menus.forEach(function (m) {
      h += '<div class="pt-menu' + (S.menu === m[0] ? ' open' : '') + '" data-menu="' + m[0] + '">' +
        '<button type="button" class="pt-ribbon-btn" data-act="menu" data-menu="' + m[0] + '" aria-haspopup="menu" aria-expanded="' +
        (S.menu === m[0] ? 'true' : 'false') + '"><span class="pt-ribbon-icon" aria-hidden="true">' + m[2] +
        '</span><span>' + m[1] + '</span><span class="pt-chevron" aria-hidden="true">▾</span></button><div class="pt-menu-pop" role="menu">';
      m[3].forEach(function (it) { h += '<button type="button" role="menuitem" data-act="' + it[0] + '">' + it[1] + '</button>'; });
      h += '</div></div>';
    });
    h += '</div><div class="pt-ribbon-trailing"><button type="button" class="pt-refresh" data-act="op-refresh" title="重新读取磁盘信息">⟳ <span>刷新</span></button></div></div>';
    if (clonePick) h += '<div class="pt-clone-prompt" role="status">已锁定源' + (clonePick.kind === 'disk' ? '硬盘 ' + clonePick.disk : '分区 ' + esc(clonePick.letter + ':')) + '，请在磁盘图或表格中点击目标' + (clonePick.kind === 'disk' ? '硬盘' : '分区') + '。<button type="button" class="btn" data-act="cancel-clone">取消选择</button></div>';
    var pickedDisk = curDisk(), pickedPart = curPart();
    var selection = pickedDisk
      ? '当前选择：硬盘 ' + pickedDisk.number + ' · ' + esc(pickedDisk.name || '磁盘') +
        (pickedPart ? ' / 分区 ' + pickedPart.number + ' · ' + esc((pickedPart.letter ? pickedPart.letter + ': ' : '') + (pickedPart.label || pickedPart.typeName || '')) : '（整盘）')
      : '请选择磁盘或分区，再选择操作。';
    h += '<div class="pt-selection-context" role="status">' + selection + '</div>';
    host.innerHTML = h;
  }

  function renderTable() {
    var host = document.getElementById('pt-table-host');
    if (!host) return;
    if (S.loading) { host.innerHTML = '<div class="pt-loading">正在读取磁盘结构…</div>'; return; }
    if (!S.disks.length) { host.innerHTML = '<div class="pt-loading">未检测到磁盘</div>'; return; }
    var h = '<div class="pt-table-wrap"><table class="pt-table"><thead><tr>' +
      '<th>名称</th><th>文件系统</th><th>容量</th><th>已使用</th><th>分区类型</th><th>状态</th><th>对齐</th>' +
      '</tr></thead><tbody>';
    for (var i = 0; i < S.disks.length; i++) {
      var d = S.disks[i];
      h += '<tr class="pt-disk-row" data-act="pick-disk" data-disk="' + d.number + '"><td colspan="7">' +
        '硬盘 ' + d.number + ' · ' + esc(d.name || '磁盘') + ' · ' + fmtSize(d.size) + ' · ' +
        (d.style === 'GPT' ? '基本 GPT' : (d.style === 'MBR' ? '基本 MBR' : esc(d.style))) +
        (d.isSystemDisk ? ' · <span class="pt-tag sys">系统盘</span>' : '') + '</td></tr>';
      for (var k = 0; k < d.partitions.length; k++) {
        var p = d.partitions[k];
        var sel = (S.selDisk === d.number && S.selPart === p.number) ? ' sel' : '';
        var name = (p.letter ? p.letter + ': ' : '*: ') + (p.label || (p.typeName || ''));
        var statusTags = '';
        if (p.isSystem) statusTags += '<span class="pt-tag sys">系统</span>';
        if (p.isBoot) statusTags += '<span class="pt-tag boot">启动</span>';
        if (p.isActive) statusTags += '<span class="pt-tag boot">活动</span>';
        if (p.isEFI) statusTags += '<span class="pt-tag">EFI</span>';
        if (p.isMSR) statusTags += '<span class="pt-tag">MSR</span>';
        if (p.isRecovery) statusTags += '<span class="pt-tag">恢复</span>';
        if (p.hidden) statusTags += '<span class="pt-tag">隐藏</span>';
        if (!statusTags) statusTags = '<span class="pt-muted">无</span>';
        h += '<tr class="pt-part-row' + sel + '" data-act="pick-part" data-disk="' + d.number + '" data-part="' + p.number + '">' +
          '<td>' + esc(name) + '</td>' +
          '<td>' + esc(p.fs || '—') + '</td>' +
          '<td>' + fmtSize(p.size) + '</td>' +
          '<td>' + (p.fs ? (fmtSize(p.used) + ' · ' + pctOf(p).toFixed(0) + '%') : '—') + '</td>' +
          '<td>' + esc(p.typeName || '—') + '</td>' +
          '<td>' + statusTags + '</td>' +
          '<td>' + (p.aligned ? '是' : '否') + '</td>' +
          '</tr>';
      }
    }
    h += '</tbody></table></div>';
    host.innerHTML = h;
  }

  function renderDisks() {
    var host = document.getElementById('pt-disks-host');
    if (!host) return;
    if (!S.disks.length) { host.innerHTML = ''; return; }
    var h = '';
    for (var i = 0; i < S.disks.length; i++) {
      var d = S.disks[i];
      var diskTitle = '硬盘 ' + d.number + (d.name ? ' · ' + d.name : '');
      h += '<div class="pt-map-row' + (S.selDisk === d.number ? ' sel' : '') + '">' +
        '<button type="button" class="pt-map-disk" data-act="pick-disk" data-disk="' + d.number + '" aria-label="选择' + esc(diskTitle) + '">' +
        '<span class="pt-drive-icon" aria-hidden="true">▰</span><strong>' + esc(diskTitle) + '</strong>' +
        '<span>' + fmtSize(d.size) + '</span><small>' + esc(d.style || '未知') + (d.isSystemDisk ? ' · 系统盘' : '') + '</small></button>' +
        '<div class="pt-map-track" role="group" aria-label="' + esc(diskTitle) + '的分区布局">';
      var cursor = 0;
      var partitions = (d.partitions || []).slice().sort(function (a, b) { return (+a.offset || 0) - (+b.offset || 0); });
      function mapGap(bytes) {
        if (!(bytes > 0)) return;
        h += '<div class="pt-map-segment pt-free" data-kind="free" style="flex-grow:' + Math.max(1, bytes / 1048576).toFixed(4) + '" title="未分配 · ' + fmtSize(bytes) + '"><span class="pt-seg-name">未分配</span><span class="pt-seg-size">' + fmtSize(bytes) + '</span></div>';
      }
      partitions.forEach(function (p) {
        var start = Math.max(cursor, Math.max(0, +p.offset || cursor));
        if (d.size && start > cursor) mapGap(start - cursor);
        var partSize = Math.max(0, +p.size || 0);
        var kind = (p.isSystem || p.isBoot || p.isActive || p.active) ? 'sys' : (p.isEFI || p.isMSR || p.isRecovery ? 'meta' : 'data');
        var label = p.letter ? p.letter + ':' : (p.isEFI ? 'EFI' : p.isMSR ? 'MSR' : p.isRecovery ? '恢复' : '*');
        var title = label + ' ' + (p.label || p.typeName || '') + ' · ' + (p.fs || '未知文件系统') + ' · ' + fmtSize(partSize) +
          (p.fs ? ' · 已用 ' + fmtSize(p.used) + ' (' + pctOf(p).toFixed(0) + '%)' : '');
        var selected = S.selDisk === d.number && S.selPart === p.number;
        h += '<button type="button" class="pt-map-segment' + (selected ? ' sel' : '') + '" data-act="pick-part" data-map-segment="1" data-disk="' + d.number + '" data-part="' + p.number + '" data-kind="' + kind + '"' +
          ' style="flex-grow:' + Math.max(1, partSize / 1048576).toFixed(4) + '" title="' + esc(title) + '" aria-label="选择' + esc(title) + '">' +
          '<span class="pt-map-usage" style="width:' + (p.fs ? pctOf(p).toFixed(2) : '0') + '%"></span>' +
          '<span class="pt-seg-name">' + esc(label + (p.label ? ' ' + p.label : '')) + '</span>' +
          '<span class="pt-seg-size">' + (p.fs ? fmtSize(p.used) + ' 已用 / ' : '') + fmtSize(partSize) + '</span>' +
          '<span class="pt-seg-fs">' + esc(p.fs || p.typeName || '未格式化') + '</span></button>';
        cursor = Math.max(cursor, start + partSize);
      });
      if (d.size && cursor < d.size) mapGap(d.size - cursor);
      h += '</div></div>';
    }
    host.innerHTML = h;
  }

  function renderSide() {
    var host = document.getElementById('pt-side-host');
    if (!host) return;
    var d = curDisk();
    if (!d) {
      host.innerHTML = '<h3>磁盘操作</h3><p class="pt-hint">点击上方表格或磁盘条选中一个磁盘,再选择操作。</p>' +
        '<div class="pt-ops">' +
        '<button class="btn" data-act="op-refresh">刷新磁盘列表</button>' +
        '<button class="btn" data-act="vhd-manage">VHD 虚拟磁盘管理</button>' +
        '</div>';
      return;
    }
    var p = curPart();
    var h = '<h3>硬盘 ' + d.number + '</h3><dl class="pt-kv">' +
      '<dt>型号</dt><dd>' + esc(d.name || '—') + '</dd>' +
      '<dt>序列号</dt><dd>' + esc(d.serial || '—') + '</dd>' +
      '<dt>总线</dt><dd>' + esc(d.bus || '—') + '</dd>' +
      '<dt>容量</dt><dd>' + fmtSize(d.size) + '</dd>' +
      '<dt>分区表</dt><dd>' + esc(d.style || '—') + '</dd>' +
      '<dt>状态</dt><dd>' + esc(d.operational || '—') + (d.readonly ? ' · 只读' : '') + '</dd>' +
      '</dl>' +
      '<div class="pt-ops" style="margin-top:10px">' +
      '<button class="btn" data-act="op-health">健康状态 · ' + esc(({Healthy:'正常',Warning:'警告',Unhealthy:'异常',Unknown:'未知'})[d.health] || d.health || '未知') + '</button>' +
      '</div>';
    var hh = S.health[d.number];
    var wear = hh && hh.wear != null ? Number(hh.wear) : NaN;
    h += '<p class="pt-hint">健康度：' + (Number.isFinite(wear) && wear >= 0 && wear <= 100 ? (100 - wear) + '%（按设备磨损估算）' : (S.healthLoading && S.healthLoading[d.number] ? '正在读取…' : '设备未提供百分比')) + '</p>';
    if (hh && !hh.error) {
      h += '<p class="pt-hint" style="margin-top:8px">温度 ' + (hh.temperature == null ? '—' : hh.temperature + ' °C') +
        ' · 通电 ' + (hh.powerOnHours == null ? '—' : hh.powerOnHours + ' 小时') +
        ' · 磨损 ' + (hh.wear == null ? '—' : hh.wear + '%') + '</p>';
    }

    var sysBlock = d.isSystemDisk;
    var cloneDataDisabled = sysBlock || d.readonly || (p ?
      (isProtectedPart(d, p) || !p.letter || !/NTFS|exFAT|FAT32/i.test(p.fs || '')) :
      !dataPartitions(d).length);
    h += '<h3 style="margin-top:16px">磁盘操作</h3><div class="pt-ops">' +
      '<button class="btn" data-act="clone-data" ' + (cloneDataDisabled ? 'disabled title="选择非系统、可写的数据磁盘或数据分区"' : '') + '>数据克隆（文件镜像）</button>' +
      '<button class="btn" data-act="op-quick" ' + (sysBlock ? 'disabled title="系统盘不允许快速分区"' : '') + '>快速分区</button>' +
      '<button class="btn" data-act="op-mbr" ' + (sysBlock || d.style !== 'MBR' ? 'disabled title="仅支持非系统 MBR 数据盘"' : '') + '>重建 MBR 引导</button>' +
      '<button class="btn danger" data-act="op-delall" ' + (sysBlock ? 'disabled title="系统盘不允许删除所有分区"' : '') + '>删除所有分区</button>' +
      '<button class="btn danger" data-act="op-wipe" ' + (sysBlock ? 'disabled title="系统盘不允许擦除"' : '') + '>擦除硬盘</button>' +
      '<button class="btn" data-act="op-convert" ' + (sysBlock ? 'disabled title="系统盘不允许转换分区表"' : '') + '>转换分区表(GPT/MBR)</button>' +
      '</div>';

    if (p) {
      var prot = isProtectedPart(d, p);
      var protTip = prot ? ' disabled title="系统盘的保护分区(系统/启动/活动/EFI/MSR/恢复)不允许此操作"' : '';
      h += '<h3 style="margin-top:16px">分区操作 · ' + esc((p.letter ? p.letter + ':' : '*:') + ' ' + (p.label || p.typeName || '')) + '</h3>' +
        '<div class="pt-ops">' +
        '<button class="btn" data-act="p-format"' + protTip + '>格式化</button>' +
        '<button class="btn" data-act="p-letter"' + protTip + '>更改盘符</button>' +
        '<button class="btn" data-act="p-label"' + (prot || !p.letter || !/NTFS|exFAT|FAT32/i.test(p.fs || '') ? ' disabled title="受保护分区或该分区没有可修改的卷标"' : '') + '>修改卷标</button>' +
        '<button class="btn" data-act="p-extend"' + protTip + '>扩展分区</button>' +
        '<button class="btn" data-act="p-shrink"' + (prot || !p.letter ? ' disabled title="受保护分区或无盘符分区不支持缩容"' : '') + '>缩容分区</button>' +
        '<button class="btn" data-act="p-scan"' + (p.letter ? '' : ' disabled title="无盘符分区无法在线检查"') + '>检查分区</button>' +
        '<button class="btn" data-act="p-defrag"' + (p.letter ? '' : ' disabled title="无盘符分区无法整理"') + '>碎片整理</button>' +
        '<button class="btn danger" data-act="p-del"' + protTip + '>删除分区</button>' +
        '</div>';
      h += '<p class="pt-hint">选中分区时，数据克隆只镜像这个分区；只选中硬盘时，会按顺序镜像该盘的全部数据卷。</p>';
    } else {
      h += '<p class="pt-hint" style="margin-top:12px">选中具体分区后可执行:格式化 / 改盘符 / 改卷标 / 扩展 / 缩容 / 检查 / 整理 / 删除。</p>';
    }
    host.innerHTML = h;
  }

  function opLabel(op) {
    var d = op.disk;
    var p = op.part;
    switch (op.type) {
      case 'format': return '格式化 盘' + d + ' 分区' + p + ' → ' + (op.fs || 'NTFS') + (op.label ? ' · ' + op.label : '');
      case 'delete_partition': return '删除 盘' + d + ' 分区' + p;
      case 'delete_all': return '删除 盘' + d + ' 的全部分区';
      case 'quick_partition': return '快速分区 盘' + d + ' · ' + (op.style || 'GPT') + ' · ' + (op.layout || []).length + ' 个分区';
      case 'convert': return '盘' + d + ' 转换为 ' + (op.to || '');
      case 'wipe': return '擦除 盘' + d + '(整盘清零,耗时较长)';
      case 'rebuild_mbr': return '重建盘' + d + ' MBR 引导';
      case 'set_letter': return '盘' + d + ' 分区' + p + ' 盘符改为 ' + op.letter + ':';
      case 'set_label': return '分区 ' + op.letter + ': 卷标改为 ' + (op.label || '空');
      case 'extend': return '扩展 盘' + d + ' 分区' + p + (op.size_mb ? ' +' + op.size_mb + 'MB' : '(用尽可用空间)');
      case 'shrink': return '缩容 盘' + d + ' 分区' + p + ' -' + op.size_mb + 'MB';
      case 'scan': return '检查分区 ' + op.letter + ':' + (op.fix ? '(含修复)' : '');
      case 'defrag': return '碎片整理 ' + op.letter + ':';
      case 'benchmark': return '测试 ' + op.letter + ': 速度';
      case 'clone_disk': return '克隆 盘' + op.source + ' → 盘' + d;
      case 'clone_partition': return '克隆分区 盘' + op.source_disk + ' 分区' + op.source_part + ' → 盘' + d + ' 分区' + p;
      case 'migrate_system':
        var targetKnown = d !== undefined && d !== null && p !== undefined && p !== null;
        var targetText = targetKnown ? '硬盘 ' + d + ' 分区' + p : '目标信息不可用';
        return '系统迁移 C: → ' + (op.letter ? op.letter + ': · ' : '') + targetText + '（仅清除此目标分区）';
      case 'vhd_mount': return '挂载 VHD: ' + (op.path || '');
      case 'vhd_dismount': return '卸载 VHD: ' + (op.path || '');
      default: return op.type;
    }
  }

  function renderPending() {
    var host = document.getElementById('pt-pending-host');
    if (!host) return;
    if (!S.pending.length) { host.innerHTML = ''; return; }
    var h = '<div class="pt-pending"><div class="pt-pending-head">待执行操作 · ' + S.pending.length + ' 项' +
      '<span class="pt-spacer"></span>' +
      '<button class="btn small" data-act="clear-queue">清空</button>' +
      '<button class="btn primary small" data-act="submit">提交执行</button></div>';
    for (var i = 0; i < S.pending.length; i++) {
      h += '<div class="pt-opchip"><span>' + esc(opLabel(S.pending[i])) + '</span>' +
        '<button class="pt-op-del" data-act="del-op" data-idx="' + i + '" title="移除">✕</button></div>';
    }
    h += '</div>';
    host.innerHTML = h;
  }

  function renderProgress(job) {
    var host = document.getElementById('pt-progress-host');
    if (!host) return;
    if (!job) { host.innerHTML = ''; return; }
    var isMigration = !!(job.migration || job.phase);
    var stageNames = {
      preflight: '迁移预检', hydrate_cloud: '下载云端文件', snapshot: '创建系统快照',
      capture: '创建系统映像', verify_image: '校验系统映像', revalidate: '复核目标磁盘',
      format_targets: '重建目标分区', apply: '写入系统映像', boot: '写入启动文件',
      inspect: '检查启动项', complete: '已完成'
    };
    var activeTask = job.state === 'running';
    var measuredGeneric = !isMigration && job.percent != null && isFinite(Number(job.percent));
    var rawPct = isMigration ? Number(job.percent) :
      (activeTask ? (measuredGeneric ? Number(job.percent) : 0) :
        (job.total ? Math.round((job.current / job.total) * 100) : 0));
    var pct = isFinite(rawPct) ? Math.max(0, Math.min(100, Math.round(rawPct))) : 0;
    var activeMigration = isMigration && activeTask;
    var phaseKey = activeTask ? (isMigration ? (job.phase || 'preparing') :
      'operation-' + job.current + '-' + (job.phase || 'running')) : '';
    if (activeTask) {
      if (S.progressPhaseKey !== phaseKey) {
        S.progressPhaseKey = phaseKey;
        S.progressPhaseStartedAt = Date.now();
      }
    } else {
      S.progressPhaseKey = '';
      S.progressPhaseStartedAt = 0;
    }
    var phaseElapsedMs = activeTask ? Math.max(0, Date.now() - S.progressPhaseStartedAt) : 0;
    var phaseName = stageNames[job.phase] || (isMigration ? '正在准备' : '正在执行');
    var displayCurrent = Number(job.current) || 0;
    if (isMigration && job.state === 'running') displayCurrent = Math.max(1, displayCurrent);
    var heading = isMigration ? '执行进度 · 系统迁移 · ' +
      phaseName + ' ' + pct + '%' + (job.estimated ? '（估算）' : '') :
      '执行进度 ' + job.current + '/' + job.total +
        (activeTask ? (measuredGeneric ? ' · ' + pct + '%' : ' · 正在处理') : '');
    var activityText = job.cancel_requested ? '正在安全停止当前操作' :
      (job.phase ? '当前阶段处理中' :
        (isMigration ? (S.submitting ? '正在启动系统迁移' : '正在检查磁盘信息') : '当前操作处理中'));
    var detailText = isMigration ? (job.detail || (job.phase ? phaseName + ' · ' + pct + '%' + (job.estimated ? '（估算）' : '') :
      (S.submitting ? '正在启动系统迁移' : '正在读取并复核磁盘信息'))) : (job.progress || '');
    var cancelControl = '';
    if (activeMigration) {
      if (job.cancel_requested) {
        cancelControl = '<button class="btn small pt-progress-cancel" disabled>正在取消…</button>';
      } else if (job.cancelable) {
        cancelControl = '<button class="btn small pt-progress-cancel" data-act="cancel-job">取消迁移</button>';
      } else {
        cancelControl = '<button class="btn small pt-progress-cancel" disabled title="为避免目标盘处于半完成状态，此阶段不能中断">当前阶段不能取消</button>';
      }
    }
    var h = '<div class="pt-card pt-progress"><div class="pt-pending-head"><span class="pt-progress-title">' + heading + '</span>' +
      (isMigration ? '<span class="pt-progress-count">任务 ' + displayCurrent + '/' + job.total + '</span>' : '') +
      cancelControl +
      (job.state === 'poll-error' ? '<span class="pt-spacer"></span><button class="btn small" data-act="retry-job">重试读取</button>' : '') + '</div>' +
      '<div class="pt-progress-track" role="progressbar" aria-label="任务执行进度" aria-valuemin="0" aria-valuemax="100" aria-valuenow="' + pct + '" aria-valuetext="' + esc(activeTask && !isMigration && !measuredGeneric ? '正在处理' : phaseName + ' · ' + pct + '%' + (job.estimated ? '（估算）' : '')) + '"><div class="pt-progress-bar" style="width:' + pct + '%"></div></div>' +
      (activeTask ? '<div class="pt-progress-activity"><span class="pt-progress-spinner" aria-hidden="true">↻</span>' +
        '<span>' + activityText + ' · 已用时 <b class="pt-progress-elapsed">' + fmtElapsed(phaseElapsedMs) + '</b></span></div>' +
        '<div class="pt-progress-activity-track" aria-hidden="true"><i class="pt-phase-sweep" style="animation-delay:-' +
        ((phaseElapsedMs % 1600) / 1000).toFixed(3) + 's"></i></div>' : '') +
      '<div class="pt-progress-text">' + esc(detailText) + '</div>';
    if (job.results && job.results.length) {
      for (var i = 0; i < job.results.length; i++) {
        var r = job.results[i];
        h += '<div class="pt-result ' + (r.cancelled ? 'cancelled' : (r.ok ? 'ok' : 'bad')) + '">' +
          (r.cancelled ? 'Ⅱ ' : (r.ok ? '✓ ' : '✕ ')) + esc(opLabel(r.op || { type: r.type })) + ' — ' + esc(r.message || '') + '</div>';
      }
    }
    h += '</div>';
    host.innerHTML = h;
  }

  function renderAll() {
    renderToolbar();
    renderTable();
    renderDisks();
    renderSide();
    renderPending();
    renderProgress(S.job);
    document.querySelectorAll('.pt-menu').forEach(function (m) {
      m.classList.toggle('open', m.getAttribute('data-menu') === S.menu);
    });
  }

  /* ---------------- 弹窗 ---------------- */
  function openModal(html, handlers) {
    if (modalResolve) closeModal(null);
    var mask = document.getElementById('pt-mask');
    var body = document.getElementById('pt-modal-body');
    if (!mask || !body) return Promise.resolve(null);
    handlers = handlers || {};
    body.innerHTML = html;
    mask.classList.add('open');
    modalCancelCallback = typeof handlers.onCancel === 'function' ? handlers.onCancel : null;
    return new Promise(function (resolve) {
      modalResolve = resolve;
      var ok = body.querySelector('[data-act="modal-ok"]');
      var cancel = body.querySelector('[data-act="modal-cancel"]');
      if (ok) ok.onclick = function () {
        if (ok.disabled) return;
        var values = collectForm();
        if (typeof handlers.onConfirm === 'function') handlers.onConfirm(values);
        else closeModal(values);
      };
      if (cancel) cancel.onclick = function () { closeModal(null); };
    });
  }
  function closeModal(value) {
    var result = value === undefined ? null : value;
    var onCancel = modalCancelCallback;
    modalCancelCallback = null;
    if (result === null && onCancel) onCancel();
    clearModalBusy();
    var mask = document.getElementById('pt-mask');
    if (mask) mask.classList.remove('open');
    var resolve = modalResolve;
    modalResolve = null;
    if (resolve) resolve(result);
  }
  function setModalFeedback(message, kind) {
    var body = document.getElementById('pt-modal-body');
    if (!body) return;
    var feedback = body.querySelector('[data-modal-feedback]');
    if (!feedback) {
      feedback = document.createElement('div');
      feedback.setAttribute('data-modal-feedback', '');
      var actions = body.querySelector('.pt-actions');
      body.insertBefore(feedback, actions || null);
    }
    if (!message) {
      feedback.className = 'pt-modal-feedback';
      feedback.textContent = '';
      return;
    }
    if (kind === 'busy') {
      feedback.className = 'pt-modal-feedback is-busy';
      feedback.innerHTML = '<span class="pt-modal-spinner" aria-hidden="true">↻</span><span>' + esc(message) +
        ' · 已用时 <b class="pt-modal-elapsed">00:00</b></span>';
    } else {
      feedback.className = 'pt-modal-feedback' + (kind === 'error' ? ' is-error' : '');
      feedback.textContent = message;
    }
  }
  function clearModalBusy(message, isError) {
    if (S.modalBusyTimer) { clearInterval(S.modalBusyTimer); S.modalBusyTimer = null; }
    S.modalBusyStartedAt = 0;
    var body = document.getElementById('pt-modal-body');
    if (!body) return;
    var ok = body.querySelector('[data-act="modal-ok"]');
    if (ok) {
      ok.removeAttribute('aria-busy');
      var idle = ok.getAttribute('data-idle-label');
      if (idle != null) { ok.textContent = idle; ok.removeAttribute('data-idle-label'); }
      ok.disabled = false;
    }
    body.querySelectorAll('[data-field]').forEach(function (field) { field.disabled = false; });
    setModalFeedback(message || '', isError ? 'error' : '');
  }
  function setModalBusy(buttonText, message) {
    var body = document.getElementById('pt-modal-body');
    if (!body) return;
    var ok = body.querySelector('[data-act="modal-ok"]');
    if (ok) {
      if (!ok.hasAttribute('data-idle-label')) ok.setAttribute('data-idle-label', ok.textContent.trim());
      ok.disabled = true;
      ok.setAttribute('aria-busy', 'true');
      ok.innerHTML = '<span class="pt-modal-spinner" aria-hidden="true">↻</span><span>' + esc(buttonText) + '</span>';
    }
    body.querySelectorAll('[data-field]').forEach(function (field) { field.disabled = true; });
    S.modalBusyStartedAt = Date.now();
    setModalFeedback(message, 'busy');
    if (S.modalBusyTimer) clearInterval(S.modalBusyTimer);
    S.modalBusyTimer = setInterval(function () {
      var elapsed = body.querySelector('.pt-modal-elapsed');
      if (elapsed) elapsed.textContent = fmtElapsed(Date.now() - S.modalBusyStartedAt);
    }, 1000);
  }
  function modalActions(confirmText, danger) {
    return '<div class="pt-actions"><button class="btn" data-act="modal-cancel">取消</button>' +
      '<button class="btn ' + (danger ? 'danger' : 'primary') + '" data-act="modal-ok">' + esc(confirmText || '确认') + '</button></div>';
  }
  function collectForm() {
    var out = {};
    document.querySelectorAll('#pt-modal-body [data-field]').forEach(function (el) {
      out[el.getAttribute('data-field')] = (el.type === 'checkbox') ? el.checked : el.value;
    });
    return out;
  }

  function loadDiskHealth(disks) {
    var generation = ++healthGeneration;
    S.health = {}; S.healthLoading = {};
    disks.forEach(function (d) { S.healthLoading[d.number] = true; });
    function next(i) {
      if (generation !== healthGeneration || i >= disks.length) return;
      var number = disks[i].number;
      Promise.resolve().then(function () { return api().partition_health(number); }).then(function (r) {
        if (generation !== healthGeneration) return;
        S.health[number] = r && r.ok && r.health ? r.health : {error: true};
      }).catch(function () {
        if (generation === healthGeneration) S.health[number] = {error: true};
      }).finally(function () {
        if (generation !== healthGeneration) return;
        delete S.healthLoading[number];
        renderSide(); next(i + 1);
      });
    }
    next(0);
  }

  /* ---------------- 数据加载 ---------------- */
  function load(force) {
    if (!api()) { setStatus('后端尚未就绪,正在等待连接…'); return; }
    if (S.loading) return;
    if (!force && S.loadedAt && Date.now() - S.loadedAt < 15000 && S.disks.length) { renderAll(); return; }
    S.loading = true;
    renderTable();
    setStatus('正在读取磁盘结构…');
    api().partition_disks().then(function (r) {
      S.loading = false;
      if (!r || !r.ok) {
        setStatus('读取失败:' + ((r && r.error) || '未知错误'), true);
        renderTable();
        return;
      }
      clonePick = null;
      S.disks = r.disks || [];
      S.admin = !!r.admin;
      S.loadedAt = Date.now();
      if (S.selDisk == null && S.disks.length) {
        var sysd = S.disks.filter(function (x) { return x.isSystemDisk; })[0] || S.disks[0];
        S.selDisk = sysd.number;
        S.selPart = null;
      } else if (S.selDisk != null) {
        var d = diskOf(S.selDisk);
        if (!d) { S.selDisk = null; S.selPart = null; }
        else if (S.selPart != null && !partOf(S.selDisk, S.selPart)) S.selPart = null;
      }
      setStatus('已加载 ' + S.disks.length + ' 块磁盘' + (S.admin ? '' : ' · 非管理员(破坏性操作将被拒绝)'));
      renderAll();
      loadDiskHealth(S.disks.slice());
    }).catch(function (e) {
      S.loading = false;
      setStatus('读取异常:' + (e && e.message ? e.message : e), true);
      renderTable();
    });
  }

  /* ---------------- 操作构造 ---------------- */
  function queue(op) {
    S.undo.push(JSON.parse(JSON.stringify(S.pending)));
    if (S.undo.length > 50) S.undo.shift();
    S.pending.push(op);
    S.redo = [];
    renderAll();
    setStatus('已加入待执行:' + opLabel(op));
  }

  function changePending(next, action) {
    S.undo.push(JSON.parse(JSON.stringify(S.pending)));
    if (S.undo.length > 50) S.undo.shift();
    S.pending = next;
    S.redo = [];
    renderAll();
    setStatus(action);
  }

  function undoQueue() {
    if (!S.undo.length) return;
    S.redo.push(JSON.parse(JSON.stringify(S.pending)));
    S.pending = S.undo.pop();
    renderAll();
    setStatus('已撤销上一步排队操作');
  }

  function redoQueue() {
    if (!S.redo.length) return;
    S.undo.push(JSON.parse(JSON.stringify(S.pending)));
    S.pending = S.redo.pop();
    renderAll();
    setStatus('已恢复已撤销的操作');
  }

  function requirePart() {
    var d = curDisk(), p = curPart();
    if (!d || !p) { setStatus('请先在表格或磁盘条中选中一个分区', true); return null; }
    return { d: d, p: p };
  }

  function actFormat() {
    var t = requirePart(); if (!t) return;
    if (isProtectedPart(t.d, t.p)) { setStatus('系统盘的保护分区不允许格式化', true); return; }
    openModal('<h3>格式化分区</h3><p class="pt-modal-desc">盘' + t.d.number + ' 分区' + t.p.number +
      ' · ' + fmtSize(t.p.size) + ' · 当前 ' + esc(t.p.fs || '未知') + '</p>' +
      '<div class="pt-danger-box">格式化会清除该分区上的全部数据,且无法通过本工具恢复。</div>' +
      '<div class="pt-field"><label>文件系统</label><select data-field="fs"><option value="NTFS">NTFS</option><option value="exFAT">exFAT</option><option value="FAT32">FAT32</option></select></div>' +
      '<div class="pt-field"><label>卷标(可选)</label><input data-field="label" maxlength="32" placeholder="例如 数据盘"></div>' +
      modalActions('加入待执行', true)).then(function (v) {
        if (!v) return;
        queue({ type: 'format', disk: t.d.number, part: t.p.number, fs: v.fs || 'NTFS', label: v.label || '' });
      });
  }

  function actDeletePart() {
    var t = requirePart(); if (!t) return;
    if (isProtectedPart(t.d, t.p)) { setStatus('系统盘的保护分区不允许删除', true); return; }
    openModal('<h3>删除分区</h3><p class="pt-modal-desc">盘' + t.d.number + ' 分区' + t.p.number +
      ' · ' + fmtSize(t.p.size) + ' · ' + esc(t.p.label || t.p.typeName || '') + '</p>' +
      '<div class="pt-danger-box">删除后该分区上的数据全部丢失,空间变为未分配。确认要继续吗?</div>' +
      modalActions('加入待执行', true)).then(function (v) {
        if (!v) return;
        queue({ type: 'delete_partition', disk: t.d.number, part: t.p.number });
      });
  }

  function actLetter() {
    var t = requirePart(); if (!t) return;
    if (isProtectedPart(t.d, t.p)) { setStatus('系统盘的保护分区不允许改盘符', true); return; }
    var used = {};
    S.disks.forEach(function (d) { d.partitions.forEach(function (p) { if (p.letter) used[p.letter] = 1; }); });
    var opts = '';
    for (var c = 68; c <= 90; c++) { // D..Z
      var L = String.fromCharCode(c);
      if (used[L] && L !== (t.p.letter || '')) continue;
      opts += '<option value="' + L + '"' + (L === t.p.letter ? ' selected' : '') + '>' + L + ':</option>';
    }
    openModal('<h3>更改盘符</h3><p class="pt-modal-desc">当前:' + esc(t.p.letter ? t.p.letter + ':' : '无') + '</p>' +
      '<div class="pt-field"><label>新盘符</label><select data-field="letter">' + opts + '</select></div>' +
      modalActions('加入待执行')).then(function (v) {
        if (!v || !v.letter) return;
        queue({ type: 'set_letter', disk: t.d.number, part: t.p.number, letter: v.letter });
      });
  }

  function actLabel() {
    var t = requirePart(); if (!t) return;
    if (!t.p.letter) { setStatus('该分区没有盘符,无法修改卷标', true); return; }
    openModal('<h3>修改卷标</h3><p class="pt-modal-desc">分区 ' + t.p.letter + ':</p>' +
      '<div class="pt-field"><label>新卷标</label><input data-field="label" maxlength="32" value="' + esc(t.p.label || '') + '"></div>' +
      modalActions('加入待执行')).then(function (v) {
        if (!v) return;
        queue({ type: 'set_label', disk: t.d.number, part: t.p.number, letter: t.p.letter, label: v.label || '' });
      });
  }

  function actExtend() {
    var t = requirePart(); if (!t) return;
    openModal('<h3>扩展分区</h3><p class="pt-modal-desc">盘' + t.d.number + ' 分区' + t.p.number + ' · ' + fmtSize(t.p.size) + '</p>' +
      '<div class="pt-note">留空表示扩展到该分区右侧可用空间的最大值;只有紧邻右侧有未分配空间时才能扩展。</div>' +
      '<div class="pt-field"><label>增加容量(MB,可选)</label><input data-field="size_mb" type="number" min="1" placeholder="留空=用尽可用空间"></div>' +
      modalActions('加入待执行')).then(function (v) {
        if (!v) return;
        var op = { type: 'extend', disk: t.d.number, part: t.p.number };
        if (v.size_mb) op.size_mb = parseInt(v.size_mb, 10);
        queue(op);
      });
  }

  function actShrink() {
    var t = requirePart(); if (!t) return;
    openModal('<h3>缩容分区</h3><p class="pt-modal-desc">盘' + t.d.number + ' 分区' + t.p.number + ' · ' + fmtSize(t.p.size) + ' · 可用 ' + fmtSize(t.p.free) + '</p>' +
      '<div class="pt-field"><label>减少容量(MB)</label><input data-field="size_mb" type="number" min="1" placeholder="例如 1024"></div>' +
      modalActions('加入待执行')).then(function (v) {
        if (!v || !v.size_mb) return;
        queue({ type: 'shrink', disk: t.d.number, part: t.p.number, size_mb: parseInt(v.size_mb, 10) });
      });
  }

  function actQuick() {
    var d = curDisk(); if (!d) { setStatus('请先选中磁盘', true); return; }
    if (d.isSystemDisk) { setStatus('系统盘不允许快速分区', true); return; }
    openModal('<h3>快速分区 · 盘' + d.number + '</h3><p class="pt-modal-desc">' + esc(d.name || '') + ' · ' + fmtSize(d.size) + '</p>' +
      '<div class="pt-danger-box">快速分区会擦除该磁盘上的全部分区与数据,然后按下方布局重新创建。此操作不可撤销。</div>' +
      '<div class="pt-row2">' +
      '<div class="pt-field"><label>分区表</label><select data-field="style"><option value="GPT">GPT</option><option value="MBR">MBR</option></select></div>' +
      '<div class="pt-field"><label>分区数量</label><select data-field="count"><option value="1">1 个</option><option value="2">2 个</option><option value="3">3 个</option><option value="4">4 个</option></select></div>' +
      '</div>' +
      '<div class="pt-field"><label>第一个分区大小(MB,留空=剩余全部)</label><input data-field="size1" type="number" min="1" placeholder="例如 512000"></div>' +
      '<div class="pt-field"><label>第二个分区大小(MB)</label><input data-field="size2" type="number" min="1" placeholder="留空=剩余全部"></div>' +
      '<div class="pt-field"><label>第三个分区大小(MB)</label><input data-field="size3" type="number" min="1" placeholder="留空=剩余全部"></div>' +
      '<div class="pt-field"><label>第四个分区大小(MB)</label><input data-field="size4" type="number" min="1" placeholder="留空=剩余全部"></div>' +
      '<div class="pt-field"><label>文件系统</label><select data-field="fs"><option value="NTFS">NTFS</option><option value="exFAT">exFAT</option></select></div>' +
      modalActions('加入待执行', true)).then(function (v) {
        if (!v) return;
        var count = parseInt(v.count, 10) || 1;
        var layout = [];
        for (var i = 1; i <= count; i++) {
          var s = v['size' + i];
          layout.push({ size_mb: s ? parseInt(s, 10) : (i === count ? 'rest' : null), fs: v.fs || 'NTFS', label: '' });
        }
        // 非最后一项必须有大小
        for (var j = 0; j < layout.length - 1; j++) {
          if (!layout[j].size_mb) { setStatus('第 ' + (j + 1) + ' 个分区必须填写大小(只有最后一个可用剩余空间)', true); return; }
        }
        if (layout.length === 1 && !layout[0].size_mb) layout[0].size_mb = 'rest';
        queue({ type: 'quick_partition', disk: d.number, style: v.style || 'GPT', layout: layout, boot_style: false });
      });
  }

  function actDelAll() {
    var d = curDisk(); if (!d) return;
    if (d.isSystemDisk) { setStatus('系统盘不允许删除所有分区', true); return; }
    openModal('<h3>删除磁盘 ' + d.number + ' 的全部分区</h3><p class="pt-modal-desc">' + esc(d.name || '') + ' · ' + fmtSize(d.size) + '</p>' +
      '<div class="pt-danger-box">该磁盘上的 ' + d.partitions.length + ' 个分区及全部数据将被删除,磁盘变为未初始化状态。</div>' +
      '(输入 DELETE 继续)<input class="pt-input" data-field="confirm" style="width:100%;margin:8px 0;background:var(--panel2);border:1px solid var(--line);color:var(--text);border-radius:12px;padding:8px 10px" placeholder="DELETE">' +
      modalActions('加入待执行', true)).then(function (v) {
        if (!v) return;
        if ((v.confirm || '').trim().toUpperCase() !== 'DELETE') { setStatus('确认词不匹配,已取消', true); return; }
        queue({ type: 'delete_all', disk: d.number });
      });
  }

  function actWipe() {
    var d = curDisk(); if (!d) return;
    if (d.isSystemDisk) { setStatus('系统盘不允许擦除', true); return; }
    openModal('<h3>擦除硬盘 ' + d.number + '</h3><p class="pt-modal-desc">' + esc(d.name || '') + ' · ' + fmtSize(d.size) + '</p>' +
      '<div class="pt-danger-box">擦除会把整块磁盘清零(clean all),数据不可恢复,耗时可能从几十分钟到数小时。</div>' +
      '(输入 ERASE 继续)<input data-field="confirm" style="width:100%;margin:8px 0;background:var(--panel2);border:1px solid var(--line);color:var(--text);border-radius:12px;padding:8px 10px" placeholder="ERASE">' +
      modalActions('加入待执行', true)).then(function (v) {
        if (!v) return;
        if ((v.confirm || '').trim().toUpperCase() !== 'ERASE') { setStatus('确认词不匹配,已取消', true); return; }
        queue({ type: 'wipe', disk: d.number });
      });
  }

  function actConvert(requested) {
    var d = curDisk(); if (!d) return;
    if (d.isSystemDisk) { setStatus('系统盘不允许转换分区表', true); return; }
    var to = requested || (d.style === 'GPT' ? 'MBR' : 'GPT');
    if (String(d.style).toUpperCase() === String(to).toUpperCase()) { setStatus('该磁盘已经是 ' + to + ' 分区表', true); return; }
    openModal('<h3>转换分区表 → ' + to + '</h3><p class="pt-modal-desc">盘' + d.number + ' 当前 ' + esc(d.style) + ',将转换为 ' + to + '</p>' +
      '<div class="pt-danger-box">本工具的分区表转换需要清空磁盘(分区与数据会全部丢失),请先备份。</div>' +
      modalActions('加入待执行', true)).then(function (v) {
        if (!v) return;
        queue({ type: 'convert', disk: d.number, to: to });
      });
  }

  function actMbr() {
    var d = curDisk(); if (!d) return;
    if (d.isSystemDisk || d.style !== 'MBR') { setStatus('仅允许对非系统 MBR 数据盘重建引导', true); return; }
    openModal('<h3>重建 MBR 引导</h3><p class="pt-modal-desc">目标：硬盘 ' + d.number + ' · ' + esc(d.name || '') + ' · ' + fmtSize(d.size) + '</p>' +
      '<div class="pt-danger-box">将重写此非系统 MBR 磁盘的引导代码。不会更改分区表或文件，但错误操作可能导致该盘无法启动。</div>' +
      '<div class="pt-note">系统盘和 GPT 磁盘会被拒绝。操作前请确认目标磁盘编号。</div>' +
      modalActions('加入待执行')).then(function (v) {
        if (!v) return;
        queue({ type: 'rebuild_mbr', disk: d.number });
      });
  }

  function dataPartitions(d) {
    return (d.partitions || []).filter(function (p) {
      return p.letter && !isProtectedPart(d, p) && /NTFS|exFAT|FAT32/i.test(p.fs || '');
    });
  }

  function chooseOperationItem(title, description, choices, buttonText) {
    if (!choices.length) {
      return openModal('<h3>' + esc(title) + '</h3><p class="pt-modal-desc">' +
        esc(description) + '</p><p class="pt-note">当前没有符合条件的磁盘或分区。</p>' +
        '<div class="pt-actions"><button class="btn" data-act="modal-cancel">关闭</button></div>').then(function () { return null; });
    }
    var options = choices.map(function (item) {
      return '<option value="' + esc(item.value) + '">' + esc(item.label) + '</option>';
    }).join('');
    var pending = openModal('<h3>' + esc(title) + '</h3><p class="pt-modal-desc">' +
      esc(description) + '</p><div class="pt-field"><label>选择对象</label><select data-field="pick">' +
      '<option value="">请选择磁盘或分区</option>' + options + '</select></div>' +
      modalActions(buttonText || '下一步'));
    var select = document.querySelector('#pt-modal-body [data-field="pick"]');
    var ok = document.querySelector('#pt-modal-body [data-act="modal-ok"]');
    if (select && ok) {
      ok.disabled = true;
      select.onchange = function () { ok.disabled = !select.value; };
    }
    return pending.then(function (v) {
      if (!v || !v.pick) return null;
      return choices.filter(function (item) { return item.value === v.pick; })[0] || null;
    });
  }

  function diskChoice(d) {
    return {value:'disk:' + d.number, disk:d.number,
      label:'硬盘 ' + d.number + ' · ' + (d.name || '磁盘') + ' · ' + fmtSize(d.size) +
        (d.isSystemDisk ? ' · 系统盘' : '')};
  }
  function partChoice(d, p) {
    return {value:'part:' + d.number + ':' + p.number, disk:d.number, part:p.number,
      label:'硬盘 ' + d.number + ' · ' + (p.letter ? p.letter + ': ' : '分区 ' + p.number + ' · ') +
        (p.label || p.typeName || '') + ' · ' + fmtSize(p.size)};
  }

  function chooseCloneFromToolbar(system) {
    if (system && S.pending.length) {
      setStatus('系统迁移需单独提交，请先处理或清空其他待执行操作', true); return;
    }
    var sources = [];
    if (system) {
      S.disks.forEach(function (d) {
        (d.partitions || []).forEach(function (p) {
          if (d.isSystemDisk && p.isBoot && String(p.letter).toUpperCase() === 'C')
            sources.push({kind:'system', disk:d.number, part:p.number, letter:'C'});
        });
      });
      if (sources.length !== 1) { setStatus('无法唯一确定当前运行的 Windows C: 分区', true); return; }
    } else {
      S.disks.forEach(function (d) {
        if (d.isSystemDisk || d.readonly) return;
        if (dataPartitions(d).length) sources.push({kind:'disk', disk:d.number});
        dataPartitions(d).forEach(function (p) {
          sources.push({kind:'part', disk:d.number, part:p.number, letter:p.letter});
        });
      });
    }
    var pickSource = system ? Promise.resolve(sources[0]) :
      chooseOperationItem('数据克隆 · 选择源', '选择要复制的硬盘或数据分区。',
        sources.map(function (s) {
          var choice = s.kind === 'disk' ? diskChoice(diskOf(s.disk)) :
            partChoice(diskOf(s.disk), partOf(s.disk, s.part));
          choice.source = s;
          return choice;
        }), '选择目标').then(function (item) { return item && item.source; });
    pickSource.then(function (source) {
      if (!source) return;
      var sourceDisk = diskOf(source.disk);
      var targets = [];
      S.disks.forEach(function (d) {
        if (d.isSystemDisk || d.readonly) return;
        if (source.kind === 'disk') {
          var srcParts = dataPartitions(sourceDisk), dstParts = dataPartitions(d);
          if (d.number !== source.disk && dstParts.length && srcParts.length === dstParts.length &&
              srcParts.every(function (p, i) { return dstParts[i].size >= (p.used || 0); }))
            targets.push(diskChoice(d));
          return;
        }
        (d.partitions || []).forEach(function (p) {
          if (source.kind === 'system') {
            if (d.number !== source.disk && !isProtectedPart(d, p) && p.letter &&
                String(p.fs).toUpperCase() === 'NTFS') targets.push(partChoice(d, p));
          } else if (!isProtectedPart(d, p) && p.letter &&
                     /NTFS|exFAT|FAT32/i.test(p.fs || '') &&
                     p.size >= (partOf(source.disk, source.part).used || 0) &&
                     !(d.number === source.disk && p.number === source.part)) {
            targets.push(partChoice(d, p));
          }
        });
      });
      var description = source.kind === 'system' ?
        '源：当前 Windows C:（硬盘 ' + source.disk + '）。请选择另一块磁盘上的目标 NTFS 分区。' :
        '源：' + (source.kind === 'disk' ? '硬盘 ' + source.disk :
          '硬盘 ' + source.disk + ' 的 ' + source.letter + ':') + '。请选择目标。';
      return chooseOperationItem(source.kind === 'system' ? '系统迁移 · 选择目标' : '数据克隆 · 选择目标',
        description, targets, '继续').then(function (target) {
          if (!target) return;
          clonePick = source;
          chooseCloneTarget(target.disk, target.part == null ? null : target.part);
        });
    }).catch(function (e) { setStatus('选择克隆对象失败：' + (e && e.message ? e.message : e), true); });
  }

  function chooseSingleTargetFromToolbar(action) {
    var diskActions = ['convert-gpt', 'convert-mbr', 'op-mbr', 'op-wipe', 'op-health'];
    var isDisk = diskActions.indexOf(action) >= 0;
    var labels = {
      'convert-gpt':'转换为 GPT', 'convert-mbr':'转换为 MBR', 'op-mbr':'重建 MBR',
      'op-wipe':'擦除硬盘', 'op-health':'健康检查', 'op-bench':'磁盘速度测试',
      'op-defrag':'碎片整理', 'op-scanfix':'检查并修复分区'
    };
    var choices = [];
    S.disks.forEach(function (d) {
      if (isDisk) {
        if (action === 'op-health' ||
            (!d.isSystemDisk && !d.readonly &&
             (action !== 'op-mbr' || d.style === 'MBR') &&
             (action !== 'convert-gpt' || d.style !== 'GPT') &&
             (action !== 'convert-mbr' || d.style !== 'MBR'))) choices.push(diskChoice(d));
      } else {
        (d.partitions || []).forEach(function (p) {
          if (p.letter && (action !== 'op-bench' || (!d.isSystemDisk && !isProtectedPart(d, p))))
            choices.push(partChoice(d, p));
        });
      }
    });
    chooseOperationItem(labels[action] + ' · 选择' + (isDisk ? '磁盘' : '分区'),
      '请选择本次操作的' + (isDisk ? '磁盘' : '分区') + '。', choices, '继续')
      .then(function (item) {
        if (!item) return;
        S.selDisk = item.disk; S.selPart = item.part == null ? null : item.part;
        renderAll();
        if (action === 'convert-gpt') actConvert('GPT');
        else if (action === 'convert-mbr') actConvert('MBR');
        else if (action === 'op-mbr') actMbr();
        else if (action === 'op-wipe') actWipe();
        else if (action === 'op-health') actHealth();
        else if (action === 'op-bench') actBench();
        else if (action === 'op-defrag') actDefrag();
        else if (action === 'op-scanfix') actScan(partOf(item.disk, item.part).letter, true);
      });
  }

  function actClonePart() {
    var t = requirePart(); if (!t) return;
    if (t.d.isSystemDisk || isProtectedPart(t.d, t.p) || !t.p.letter || !/NTFS|exFAT|FAT32/i.test(t.p.fs || '')) {
      setStatus('克隆仅支持非系统、非保护且带盘符的数据分区', true); return;
    }
    clonePick = {kind:'part', disk:t.d.number, part:t.p.number, letter:t.p.letter};
    renderAll(); setStatus('已锁定源分区 ' + t.p.letter + ':，请点击目标分区');
  }

  function actCloneDisk() {
    var source = curDisk();
    if (!source || source.isSystemDisk || !dataPartitions(source).length) {
      setStatus('请先选择包含可用数据卷的非系统源磁盘', true); return;
    }
    clonePick = {kind:'disk', disk:source.number};
    renderAll(); setStatus('已锁定源硬盘 ' + source.number + '，请点击目标硬盘');
  }

  function actCloneData() {
    if (curPart()) actClonePart();
    else actCloneDisk();
  }

  function actMigrateSystem() {
    var t = requirePart();
    if (!t || !t.d.isSystemDisk || !t.p.isBoot || String(t.p.letter).toUpperCase() !== 'C') {
      setStatus('请先选中当前运行的 Windows C: 分区，再选择系统克隆', true); return;
    }
    if (S.pending.length) {
      setStatus('系统迁移需单独提交，请先处理或清空其他待执行操作', true); return;
    }
    clonePick = {kind:'system', disk:t.d.number, part:t.p.number, letter:'C'};
    renderAll(); setStatus('已锁定运行中的 C:，请点击另一块磁盘上的目标 NTFS 分区');
  }

  function chooseCloneTarget(diskNumber, partNumber) {
    var pick = clonePick, source = pick && diskOf(pick.disk), dest = diskOf(diskNumber);
    if (!pick || !source || !dest) { clonePick = null; renderAll(); setStatus('磁盘信息已变化，请重新选择', true); return; }
    if (pick.kind === 'system') {
      var selected = partNumber == null ? null : partOf(diskNumber, partNumber);
      if (!selected || dest.number === source.number || dest.isSystemDisk || dest.readonly ||
          isProtectedPart(dest, selected) || !selected.letter || String(selected.fs).toUpperCase() !== 'NTFS') {
        setStatus('请选择另一物理磁盘上的可写 NTFS 数据分区作为系统迁移目标', true); return;
      }
      var scratch = S.disks.filter(function(d) { return d.number !== source.number && d.number !== dest.number; })
        .reduce(function(a,d) { return a.concat((d.partitions || []).filter(function(p) {
          return p.letter && String(p.fs).toUpperCase() === 'NTFS' && !d.readonly;
        }).map(function(p) {return {disk:d, part:p};})); }, []);
      if (!scratch.length) { setStatus('需要第三块物理磁盘上的 NTFS 暂存卷，当前未找到', true); return; }
      S.selDisk = dest.number; S.selPart = selected.number;
      clonePick = null; renderAll();
      var options = scratch.map(function(x) { return '<option value="' + esc(x.part.letter) + '">' +
        esc(x.part.letter) + ': · 硬盘 ' + x.disk.number + ' · 可用 ' + fmtSize(x.part.free) + '</option>'; }).join('');
      var migrationPlanResponse = null;
      openModal('<h3>迁移系统 · 选择暂存卷</h3><p class="pt-modal-desc">源：C: · 硬盘 ' + source.number +
        '<br>目标：' + esc(selected.letter) + ': · 硬盘 ' + dest.number + ' 分区 ' + selected.number +
        ' · ' + fmtSize(selected.size) + '</p><div class="pt-field"><label>映像暂存卷（不会格式化）</label>' +
        '<select data-field="scratch">' + options + '</select></div>' +
        '<p class="pt-note">下一步先读取实际磁盘身份、容量和加密状态，不会立即修改分区。</p>' +
        '<div class="pt-modal-feedback" data-modal-feedback></div>' + modalActions('检查并预览'), {
        onConfirm: function(v) {
          if (!v) return;
          if (!v.scratch) { setModalFeedback('请选择映像暂存卷', 'error'); return; }
          var requestId = ++S.migrationPreviewSeq;
          setModalBusy('正在检查…', '正在读取磁盘状态并扫描用户目录中的文件名；文件较多时约需半分钟');
          setStatus('正在验证源系统、目标分区与暂存空间…');
          Promise.resolve().then(function() {
            return api().partition_migration_plan(dest.number, selected.number, v.scratch);
          }).then(function(r) {
            if (requestId !== S.migrationPreviewSeq) return;
            if (!r || !r.ok) {
              var message = '无法迁移：' + ((r && r.error) || '预检失败');
              clearModalBusy(message, true); setStatus(message, true); return;
            }
            migrationPlanResponse = r;
            clearModalBusy();
            closeModal(v);
          }).catch(function(e) {
            if (requestId !== S.migrationPreviewSeq) return;
            var message = '系统迁移预检异常：' + (e && e.message ? e.message : e);
            clearModalBusy(message, true); setStatus(message, true);
          });
        },
        onCancel: function() { S.migrationPreviewSeq++; }
      }).then(function(v) {
          if (!v) return;
          var r = migrationPlanResponse;
          if (!r) return;
            var p = r.plan, t = p.target;
            var cloud = r.cloud_files || {count:0,total_bytes:0,manual_count:0,samples:[]};
            var cloudCount = Math.max(0, Number(cloud.count) || 0);
            var manualCloudCount = Math.max(0, Number(cloud.manual_count) || 0);
            var cloudHtml = '';
            if (manualCloudCount) {
              cloudHtml = '<div class="pt-danger-box">检测到 ' + manualCloudCount + ' 个 OneDrive 在线文件夹。程序无法安全地自动下载在线文件夹。请取消此窗口，在资源管理器中对这些文件夹选择“始终保留在此设备”，等待下载完成后重新预览。样例：' +
                (cloud.samples || []).slice(0, 5).map(esc).join('；') + '</div>';
            } else if (cloudCount) {
              cloudHtml = '<div class="pt-cloud-box"><b>发现 ' + cloudCount + ' 个 OneDrive 在线文件</b> · 预计下载 ' + fmtSize(cloud.total_bytes || 0) +
                '<br>克隆前需先下载这些文件，可能使用网络流量和 C: 磁盘空间。样例：' +
                (cloud.samples || []).slice(0, 5).map(esc).join('；') + '</div>' +
                '<label class="pt-check-confirm"><input type="checkbox" data-field="confirm_cloud_download">' +
                '<span>我允许在克隆前下载这 ' + cloudCount + ' 个 OneDrive 文件</span></label>';
            }
            var reservedNames = Array.isArray(r.reserved_names) ? r.reserved_names : [];
            var reservedCount = reservedNames.length;
            var reservedHtml = '';
            if (reservedCount) {
              var reservedList = reservedNames.slice(0, 8).map(function(item) {
                var label = item.directory ? '（文件夹及其内容）' : '（文件，' + fmtSize(item.size || 0) + '）';
                return '<li>' + esc(item.path || item.relative || '') + ' ' + label + '</li>';
              }).join('');
              if (reservedCount > 8) reservedList += '<li>另有 ' + (reservedCount - 8) + ' 个条目</li>';
              reservedHtml = '<div class="pt-danger-box"><b>发现 ' + reservedCount + ' 个 Windows 保留名条目</b>。例如 NUL、CON 等名称不能作为普通文件写入 Windows。迁移时会从新映像中跳过下列项目，C 盘源文件保持原样：<ul>' +
                reservedList + '</ul></div>' +
                '<label class="pt-check-confirm"><input type="checkbox" data-field="confirm_reserved_names">' +
                '<span>我确认跳过这些 Windows 保留名条目</span></label>';
            }
            setStatus('预检通过，请核对目标分区并勾选清空确认');
            openModal('<h3>确认系统迁移</h3><p class="pt-modal-desc">源：硬盘 ' + p.source.disk + ' 的 C:（保持原样）<br>' +
              '目标：硬盘 ' + t.disk + ' 分区 ' + t.part + ' · ' + esc(t.letter) + ': · ' + fmtSize(t.size) +
              '<br>磁盘标识：' + esc(t.disk_unique_id) + '<br>分区标识：' + esc(t.part_guid) +
              '<br>范围：' + t.offset + '–' + (t.offset + t.size) + ' 字节</p>' +
              '<div class="pt-danger-box">执行时只会删除上述目标分区及其全部数据，在原位置新建 ' +
              fmtSize(p.new_efi_size) + ' EFI 启动分区和 Windows 分区。目标硬盘上的其他分区保持原位。' +
              '暂存映像位于 ' + esc(p.scratch.letter) + ':。操作不可撤销；独立启动还需重启验证。</div>' +
              cloudHtml +
              reservedHtml +
              '<label class="pt-check-confirm"><input type="checkbox" data-field="confirm_target">' +
              '<span>我确认清空 <b>' + esc(t.letter) + ':</b>（硬盘 ' + t.disk + ' 分区 ' + t.part +
              '）的全部数据</span></label>' +
              modalActions('加入待执行', true)).then(function(c) {
                if (!c) return;
                if (c.confirm_target !== true) {
                  setStatus('请先确认清空所选目标分区', true); return;
                }
                if (cloudCount && !manualCloudCount && c.confirm_cloud_download !== true) {
                  setStatus('请先确认下载 OneDrive 在线文件', true); return;
                }
                if (reservedCount && c.confirm_reserved_names !== true) {
                  setStatus('请先确认跳过 Windows 保留名条目', true); return;
                }
                queue({type:'migrate_system', disk:t.disk, part:t.part, token:r.token, confirmed:true,
                  download_cloud_files: cloudCount > 0 && manualCloudCount === 0 && c.confirm_cloud_download === true,
                  skip_reserved_names: reservedCount > 0 && c.confirm_reserved_names === true});
              });
            var check = document.querySelector('#pt-modal-body [data-field="confirm_target"]');
            var cloudCheck = document.querySelector('#pt-modal-body [data-field="confirm_cloud_download"]');
            var reservedCheck = document.querySelector('#pt-modal-body [data-field="confirm_reserved_names"]');
            var add = document.querySelector('#pt-modal-body [data-act="modal-ok"]');
            if (check && add) {
              add.disabled = true;
              function updateConfirmState() {
                add.disabled = !check.checked || (cloudCount > 0 && !manualCloudCount &&
                  (!cloudCheck || !cloudCheck.checked)) || manualCloudCount > 0 ||
                  (reservedCount > 0 && (!reservedCheck || !reservedCheck.checked));
              }
              check.onchange = updateConfirmState;
              if (cloudCheck) cloudCheck.onchange = updateConfirmState;
              if (reservedCheck) reservedCheck.onchange = updateConfirmState;
            }
      }).catch(function(e) { setStatus('系统迁移预检异常：' + (e && e.message ? e.message : e), true); });
      return;
    }
    if (pick.kind === 'disk') {
      var srcParts = dataPartitions(source), dstParts = dataPartitions(dest);
      if (dest.number === source.number || dest.isSystemDisk || dest.readonly || !dstParts.length || srcParts.length !== dstParts.length || !srcParts.every(function(p,i){return dstParts[i].size >= (p.used || 0);})) {
        setStatus('目标必须是另一块可写的非系统数据盘，数据卷数量相同且容量足够；请重新选择目标', true); return;
      }
      S.selDisk = dest.number; S.selPart = null;
      clonePick = null; renderAll();
      openModal('<h3>确认克隆硬盘 · 文件镜像</h3><p class="pt-modal-desc">源：硬盘 ' + source.number + ' · ' + esc(source.name || '') + '<br>目标：硬盘 ' + dest.number + ' · ' + esc(dest.name || '') + '</p>' +
        '<div class="pt-danger-box">提交后会按分区顺序镜像数据卷，目标端多余文件会被删除。不会复制分区表或启动能力。</div>' + modalActions('加入待执行', true)).then(function(v) {
          if (v) queue({type:'clone_disk', source:source.number, disk:dest.number, confirmed:true});
        });
      return;
    }
    var src = partOf(pick.disk, pick.part), target = partNumber == null ? null : partOf(diskNumber, partNumber);
    if (!src || !target || dest.isSystemDisk || dest.readonly || isProtectedPart(dest,target) || !target.letter || !/NTFS|exFAT|FAT32/i.test(target.fs || '') || target.size < (src.used || 0) || (source.number === dest.number && src.number === target.number)) {
      setStatus('请点击另一个可写、容量足够的非系统数据分区作为目标', true); return;
    }
    S.selDisk = dest.number; S.selPart = target.number;
    clonePick = null; renderAll();
    openModal('<h3>确认克隆分区 · 文件镜像</h3><p class="pt-modal-desc">源：盘' + source.number + ' · ' + esc(src.letter + ': ' + (src.label || '')) + '<br>目标：盘' + dest.number + ' · ' + esc(target.letter + ': ' + (target.label || '')) + '</p>' +
      '<div class="pt-danger-box">提交后会镜像源分区文件，并删除目标端多余文件。不会复制分区表或启动能力。</div>' + modalActions('加入待执行', true)).then(function(v) {
        if (v) queue({type:'clone_partition', source_disk:source.number, source_part:src.number, source_letter:src.letter, disk:dest.number, part:target.number, dest_letter:target.letter, confirmed:true});
      });
  }

  function actHealth() {
    var d = curDisk(); if (!d) return;
    var requestId = ++S.healthRequestSeq;
    openModal('<h3>硬盘 ' + d.number + ' · 健康检查</h3>' +
      '<p class="pt-modal-desc">' + esc(d.name || '磁盘') + ' · ' + fmtSize(d.size) + '</p>' +
      '<div data-health-request="' + requestId + '" class="pt-modal-feedback is-busy">' +
      '<span class="pt-modal-spinner" aria-hidden="true">↻</span>正在读取设备健康信息…</div>' +
      '<div class="pt-actions"><button class="btn" data-act="modal-cancel">关闭</button></div>');
    S.healthLoading = S.healthLoading || {};
    S.healthLoading[d.number] = true;
    renderSide();
    setStatus('正在读取 盘' + d.number + ' 健康信息…');
    Promise.resolve().then(function () { return api().partition_health(d.number); }).then(function (r) {
      var panel = document.querySelector('#pt-modal-body [data-health-request="' + requestId + '"]');
      if (!r || !r.ok || !r.health) {
        var message = (r && r.error) || '设备未返回健康信息';
        if (panel) { panel.className = 'pt-modal-feedback is-error'; panel.textContent = message; }
        setStatus('健康读取失败：' + message, true);
        return;
      }
      var h = r.health;
      S.health[d.number] = h;
      var wear = h.wear == null ? NaN : Number(h.wear);
      var score = Number.isFinite(wear) && wear >= 0 && wear <= 100 ?
        (100 - wear) + '%（按设备磨损估算）' : '设备未提供';
      var state = ({Healthy:'正常', Warning:'警告', Unhealthy:'异常', Unknown:'未知'})[h.health] || h.health || '未知';
      if (panel) {
        panel.className = 'pt-health-result';
        panel.innerHTML = '<dl class="pt-kv"><dt>健康状态</dt><dd>' + esc(state) +
          '</dd><dt>健康度</dt><dd>' + esc(score) +
          '</dd><dt>温度</dt><dd>' + (h.temperature == null ? '设备未提供' : esc(h.temperature) + ' °C') +
          '</dd><dt>通电时间</dt><dd>' + (h.powerOnHours == null ? '设备未提供' : esc(h.powerOnHours) + ' 小时') +
          '</dd><dt>读取错误</dt><dd>' + (h.readErrors == null ? '设备未提供' : esc(h.readErrors)) +
          '</dd><dt>写入错误</dt><dd>' + (h.writeErrors == null ? '设备未提供' : esc(h.writeErrors)) +
          '</dd></dl><p class="pt-note">部分硬盘或控制器不提供完整的 SMART 项目。</p>';
      }
      setStatus('硬盘 ' + d.number + ' 健康检查完成');
    }).catch(function (e) {
      var message = e && e.message ? e.message : String(e);
      var panel = document.querySelector('#pt-modal-body [data-health-request="' + requestId + '"]');
      if (panel) { panel.className = 'pt-modal-feedback is-error'; panel.textContent = message; }
      setStatus('健康读取异常：' + message, true);
    }).finally(function () {
      delete S.healthLoading[d.number];
      renderSide();
    });
  }

  function actBench() {
    var t = requirePart();
    if (!t || !t.p.letter) { setStatus('请选中一个带盘符的分区再测速', true); return; }
    queue({ type: 'benchmark', letter: t.p.letter });
    setStatus('已加入待执行:测试 ' + t.p.letter + ':(64MB 顺序读写)');
  }

  function actScan(letter, fix) {
    queue({ type: 'scan', letter: letter, fix: !!fix });
  }

  function actDefrag() {
    var t = requirePart();
    if (!t || !t.p.letter) { setStatus('请选中带盘符的分区', true); return; }
    queue({ type: 'defrag', letter: t.p.letter });
  }

  function actVhd() {
    api().partition_vhd_list().then(function (r) {
      if (!r || !r.ok) {
        setStatus('读取 VHD 列表失败:' + ((r && r.error) || '未知错误'), true);
        return;
      }
      var list = (r && r.vhds) || [];
      var h = '<h3>VHD 虚拟磁盘管理</h3><p class="pt-modal-desc">列出已挂载的虚拟磁盘;可挂载新的 .vhd 或卸载已有 VHD。</p>';
      if (!list.length) h += '<p class="pt-note">当前没有已挂载的 VHD。</p>';
      else {
        h += '<div style="margin-bottom:10px">';
        list.forEach(function (v) {
          h += '<div class="pt-opchip"><span>' + esc(v.Path || '') + ' · ' + (v.SizeGB || '?') + ' GB · ' + (v.Attached ? '已挂载' : '未挂载') + '</span>' +
            '<button class="pt-op-del" data-act="vhd-dismount" data-path="' + esc(v.Path || '') + '" title="卸载">✕</button></div>';
        });
        h += '</div>';
      }
      h += '<div class="pt-field"><label>挂载 VHD 文件路径(.vhd/.vhdx)</label><input data-field="path" placeholder="D:\\vm\\disk.vhdx"></div>' +
        '<div class="pt-actions"><button class="btn" data-act="modal-cancel">关闭</button>' +
        '<button class="btn primary" data-act="vhd-mount">挂载</button></div>';
      openModal(h).catch(function (e) { setStatus('打开 VHD 管理失败:' + e, true); });
      var cancel = document.querySelector('#pt-modal-body [data-act="modal-cancel"]');
      if (cancel) cancel.onclick = function () { closeModal(null); };
      var mk = document.querySelector('#pt-modal-body [data-act="vhd-mount"]');
      if (mk) mk.onclick = function () {
        var inp = document.querySelector('#pt-modal-body [data-field="path"]');
        var p = inp ? inp.value.trim() : '';
        if (!p) { setStatus('请填写 VHD 路径', true); return; }
        closeModal(null);
        queue({ type: 'vhd_mount', path: p });
      };
    }).catch(function (e) { setStatus('读取 VHD 列表异常:' + (e && e.message ? e.message : e), true); });
  }

  /* ---------------- 提交执行 ---------------- */
  function submit() {
    if (!S.pending.length) { setStatus('没有待执行操作', true); return; }
    if (S.submitting) { setStatus('正在提交，请稍候'); return; }
    if (S.pending.some(function(o) {return o.type === 'migrate_system';}) && S.pending.length !== 1) {
      setStatus('系统迁移必须单独提交，请移除其他待执行操作', true); return;
    }
    if (S.job && S.job.state === 'running') { setStatus('已有任务在执行,请等待完成', true); return; }
    openModal('<h3>确认提交 ' + S.pending.length + ' 项操作</h3>' +
      '<p class="pt-modal-desc">提交后将按顺序执行,破坏性操作会立即生效且不可撤销。</p>' +
      S.pending.map(function (op) { return '<div class="pt-opchip">' + esc(opLabel(op)) + '</div>'; }).join('') +
      modalActions('确认执行', true)).then(function (v) {
        if (!v) return;
        var ops = JSON.parse(JSON.stringify(S.pending));
        var previousJob = S.job;
        var isMigration = ops.some(function(op) { return op.type === 'migrate_system'; });
        S.submitting = true;
        setStatus('正在提交…');
        renderToolbar();
        if (isMigration) {
          S.progressPhaseKey = ''; S.progressPhaseStartedAt = 0;
          S.job = { state: 'running', current: 0, total: ops.length, progress: '正在启动迁移任务', results: [],
            migration: true, percent: 0 };
          renderProgress(S.job);
          if (S.submittingTimer) clearInterval(S.submittingTimer);
          S.submittingTimer = setInterval(function () {
            if (S.submitting && S.job) renderProgress(S.job);
          }, 1000);
        }
        Promise.resolve().then(function () { return api().partition_job_submit(ops); }).then(function (r) {
          if (S.submittingTimer) { clearInterval(S.submittingTimer); S.submittingTimer = null; }
          S.submitting = false;
          if (!r || !r.ok) {
            S.job = previousJob;
            renderToolbar(); renderProgress(S.job);
            setStatus('提交失败:' + ((r && r.error) || ''), true); return;
          }
          S.pending = [];
          S.undo = []; S.redo = [];
          S.job = { state: 'running', current: 0, total: ops.length, progress: isMigration ? '正在读取并复核磁盘信息' : '任务已创建', results: [],
            migration: isMigration, percent: 0 };
          S.jobId = r.job_id; S.pollFailures = 0;
          renderToolbar();
          renderPending();
          renderProgress(S.job);
          pollJob(r.job_id);
        }).catch(function (e) {
          if (S.submittingTimer) { clearInterval(S.submittingTimer); S.submittingTimer = null; }
          S.submitting = false; S.job = previousJob;
          renderToolbar(); renderProgress(S.job); setStatus('提交异常:' + e, true);
        });
      });
  }

  function pollJob(jobId) {
    if (S.timer) clearInterval(S.timer);
    setStatus('任务执行中…');
    var polling = false;
    function pollOnce() {
      if (polling) return;
      polling = true;
      Promise.resolve().then(function () { return api().partition_job_status(jobId); }).then(function (st) {
        if (!st || !st.ok) throw new Error((st && st.error) || '任务状态读取失败');
        S.pollFailures = 0;
        S.job = st;
        renderProgress(st);
        if (st.state === 'done' || st.state === 'error' || st.state === 'cancelled') {
          clearInterval(S.timer); S.timer = null;
          var bad = (st.results || []).filter(function (x) { return !x.ok; }).length;
          var migrated = (st.results || []).some(function (x) { return x.type === 'migrate_system' && x.ok; });
          setStatus(st.state === 'cancelled' ? (st.progress || '已取消系统迁移，目标分区未改动') :
            (st.state === 'error' ? ('任务异常:' + (st.progress || '')) :
            (migrated ? '系统文件与目标引导已写入；请独立启动验证' :
              '执行完成:' + (st.results.length - bad) + ' 项成功' + (bad ? ',' + bad + ' 项失败' : ''))),
            bad > 0 || st.state === 'error');
          load(true);
        }
      }).catch(function (e) {
        S.pollFailures++;
        if (S.pollFailures >= 4) {
          clearInterval(S.timer); S.timer = null;
          S.job = S.job || { current: 0, total: 0, results: [] };
          S.job.state = 'poll-error'; S.job.progress = '连续读取任务状态失败：' + (e && e.message ? e.message : e);
          renderProgress(S.job);
          setStatus('任务仍可能在后台运行；状态读取失败，可重试查询。', true);
        }
      }).then(function () { polling = false; }, function () { polling = false; });
    }
    S.timer = setInterval(pollOnce, 800);
    pollOnce();
  }

  /* ---------------- 事件 ---------------- */
  function onRootClick(ev) {
    var el = ev.target.closest('[data-act]');
    if (!el) {
      if (!ev.target.closest('.pt-menu') && S.menu) { S.menu = null; renderAll(); }
      return;
    }
    var act = el.getAttribute('data-act');
    var fromToolbarMenu = !!el.closest('.pt-menu-pop');
    if (act === 'menu') {
      var m = el.getAttribute('data-menu');
      S.menu = (S.menu === m) ? null : m;
      renderAll();
      return;
    }
    if (S.menu && ['migrate-system', 'clone-data', 'convert-gpt', 'convert-mbr', 'op-mbr', 'op-scanfix', 'op-wipe', 'op-bench', 'op-health', 'op-defrag', 'op-refresh', 'op-copy', 'vhd-manage'].indexOf(act) >= 0) {
      S.menu = null;
      renderAll();
    }
    if (el.disabled) return;
    if (fromToolbarMenu && (act === 'migrate-system' || act === 'clone-data')) {
      chooseCloneFromToolbar(act === 'migrate-system');
      return;
    }
    if (fromToolbarMenu && ['convert-gpt', 'convert-mbr', 'op-mbr', 'op-wipe',
        'op-health', 'op-bench', 'op-defrag', 'op-scanfix'].indexOf(act) >= 0) {
      chooseSingleTargetFromToolbar(act);
      return;
    }
    switch (act) {
      case 'cancel-clone': clonePick = null; renderAll(); setStatus('已取消克隆目标选择'); break;
      case 'pick-disk':
        if (clonePick) { chooseCloneTarget(parseInt(el.getAttribute('data-disk'), 10), null); break; }
        S.selDisk = parseInt(el.getAttribute('data-disk'), 10);
        S.selPart = null;
        renderAll(); break;
      case 'pick-part':
        if (clonePick) { chooseCloneTarget(parseInt(el.getAttribute('data-disk'), 10), parseInt(el.getAttribute('data-part'), 10)); break; }
        S.selDisk = parseInt(el.getAttribute('data-disk'), 10);
        S.selPart = parseInt(el.getAttribute('data-part'), 10);
        renderAll(); break;
      case 'undo': undoQueue(); break;
      case 'redo': redoQueue(); break;
      case 'op-refresh': load(true); break;
      case 'op-quick': actQuick(); break;
      case 'op-delall': actDelAll(); break;
      case 'op-wipe': actWipe(); break;
      case 'op-mbr': actMbr(); break;
      case 'op-convert':
        actConvert(); break;
      case 'convert-gpt': actConvert('GPT'); break;
      case 'convert-mbr': actConvert('MBR'); break;
      case 'op-health': actHealth(); break;
      case 'op-bench': actBench(); break;
      case 'op-defrag': actDefrag(); break;
      case 'op-scanfix':
        var t = requirePart();
        if (!t || !t.p.letter) { setStatus('请选中带盘符的分区', true); break; }
        actScan(t.p.letter, true); break;
      case 'clone-disk': actCloneDisk(); break;
      case 'clone-part': actClonePart(); break;
      case 'clone-data': actCloneData(); break;
      case 'migrate-system': actMigrateSystem(); break;
      case 'op-copy':
        var txt = JSON.stringify({ disks: S.disks }, null, 2);
        if (navigator.clipboard) navigator.clipboard.writeText(txt).then(function () { setStatus('磁盘信息已复制到剪贴板'); })
          .catch(function (e) { setStatus('复制失败:' + (e && e.message ? e.message : e), true); });
        else setStatus('当前环境不支持剪贴板', true);
        break;
      case 'vhd-manage': actVhd(); break;
      case 'p-format': actFormat(); break;
      case 'p-del': actDeletePart(); break;
      case 'p-letter': actLetter(); break;
      case 'p-label': actLabel(); break;
      case 'p-extend': actExtend(); break;
      case 'p-shrink': actShrink(); break;
      case 'p-scan':
        var tp = requirePart();
        if (tp && tp.p.letter) actScan(tp.p.letter, false); break;
      case 'p-defrag': actDefrag(); break;
      case 'submit': submit(); break;
      case 'cancel-job':
        if (!S.jobId || !S.job || !S.job.cancelable || S.job.cancel_requested) break;
        S.job.cancel_requested = true;
        S.job.cancelable = false;
        S.job.detail = '正在停止当前阶段并清理暂存映像；不会改动目标分区。';
        renderProgress(S.job);
        setStatus('正在安全取消系统迁移…');
        Promise.resolve().then(function () { return api().partition_job_cancel(S.jobId); }).then(function (r) {
          if (!r || !r.ok) {
            S.job.cancel_requested = false;
            S.job.detail = '';
            renderProgress(S.job);
            setStatus((r && r.error) || '当前阶段无法取消', true);
            pollJob(S.jobId);
          }
        }).catch(function (e) {
          S.job.cancel_requested = false;
          S.job.detail = '';
          renderProgress(S.job);
          setStatus('取消请求未送达，任务可能仍在运行：' + (e && e.message ? e.message : e), true);
          pollJob(S.jobId);
        });
        break;
      case 'retry-job': if (S.jobId) { S.pollFailures = 0; pollJob(S.jobId); } break;
      case 'clear-queue':
        if (S.pending.length) changePending([], '已清空待执行队列');
        break;
      case 'del-op':
        var idx = parseInt(el.getAttribute('data-idx'), 10);
        if (idx >= 0 && idx < S.pending.length) {
          var next = S.pending.slice(); next.splice(idx, 1);
          changePending(next, '已移除队列中的操作');
        }
        break;
      case 'modal-cancel': closeModal(); break;
      case 'vhd-dismount':
        var pth = el.getAttribute('data-path');
        closeModal();
        queue({ type: 'vhd_dismount', path: pth });
        break;
    }
  }

  function ensureShell() {
    if (!root()) return false;
    if (!root().querySelector('.pt-wrap')) {
      root().innerHTML = shell();
      root().addEventListener('click', onRootClick);
      var mask = document.getElementById('pt-mask');
      if (mask) mask.addEventListener('click', function (e) { if (e.target === mask) closeModal(); });
      document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape') {
          if (modalResolve) closeModal(null);
          if (clonePick) { clonePick = null; renderAll(); setStatus('已取消克隆目标选择'); }
          if (S.menu) { S.menu = null; renderAll(); }
          return;
        }
        var target = e.target;
        var editing = target && (target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName));
        if (editing || !(e.ctrlKey || e.metaKey) || e.altKey) return;
        if (e.key.toLowerCase() === 'z') {
          e.preventDefault();
          if (e.shiftKey) redoQueue(); else undoQueue();
        } else if (e.key.toLowerCase() === 'y') {
          e.preventDefault(); redoQueue();
        }
      });
    }
    return true;
  }

  window.LxPartition = {
    enter: function () {
      if (!ensureShell()) return;
      if (!api()) {
        setStatus('后端初始化中…');
        window.addEventListener('pywebviewready', function () { load(true); }, { once: true });
        return;
      }
      if (!S.disks.length || !S.loadedAt) { load(true); }
      else renderAll();
    },
    refresh: function () { load(true); }
  };
})();
