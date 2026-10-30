(function () {
  const $ = id => document.getElementById(id);
  const api = () => window.pywebview?.api;
  const escape = value => window.escapeHtml ? window.escapeHtml(value ?? '—') : String(value ?? '—');
  const bytes = value => window.fmtBytes ? window.fmtBytes(value) : '—';
  let benchmarkTimer = null;
  let benchmarkElapsedTimer = null;
  let benchmarkStartedAt = null;
  let previousBenchmarkState = null;

  function inventoryMarkup(data) {
    const rows = [
      ['操作系统', [data.os?.name, data.os?.architecture, data.os?.version ? `版本 ${data.os.version}` : null].filter(Boolean).join(' · ')],
      ['处理器', [data.cpu?.name, data.cpu?.cores != null ? `${data.cpu.cores} 核 / ${data.cpu.threads} 线程` : null,
        data.cpu?.baseClockMHz ? `基础频率 ${data.cpu.baseClockMHz} MHz` : null].filter(Boolean).join(' · ')],
      ['显卡', [data.gpu?.name, data.gpu?.vramMiB ? `${(data.gpu.vramMiB / 1024).toFixed(1)} GB 显存` : null,
        data.gpu?.driverVersion ? `驱动 ${data.gpu.driverVersion}` : null].filter(Boolean).join(' · ')],
      ['主板', [data.board?.manufacturer, data.board?.model, data.board?.chipset].filter(Boolean).join(' · ')],
      ['内存', [data.memory?.totalBytes ? bytes(data.memory.totalBytes) : null,
        data.memory?.frequencyMHz ? `${data.memory.frequencyMHz} MHz` : null,
        data.memory?.moduleCount ? `${data.memory.moduleCount} 条` : null,
        ...(data.memory?.modules || []).map(item => [escape(item.manufacturer), escape(item.partNumber),
          item.capacityBytes ? bytes(item.capacityBytes) : null,
          item.speedMHz ? `${item.speedMHz} MHz` : null].filter(Boolean).join(' / '))].filter(Boolean).join(' · ')],
      ['存储设备', (data.storage || []).map(item => `${escape(item.model)} · ${escape(bytes(item.sizeBytes))} · ${escape(item.type)}`).join('<br>')],
      ['显示器', [data.display?.name, data.display?.resolution,
        data.display?.refreshRateHz ? `${data.display.refreshRateHz} Hz` : null,
        data.display?.physicalSizeInches ? `${data.display.physicalSizeInches} 英寸` : null].filter(Boolean).join(' · ')]
    ];
    return rows.map(([name, value]) => `<div class="hardware-row"><strong>${escape(name)}</strong><span>${value ? (name === '存储设备' ? value : escape(value)) : '系统未报告'}</span></div>`).join('');
  }

  async function refreshHardware(force) {
    if (!api()) return;
    const button = $('hardware-refresh');
    if (button) button.disabled = true;
    const status = $('hardware-status');
    if (status) status.textContent = '正在读取本机硬件清单…';
    const progress = $('overview-scan-progress');
    if (progress) {
      progress.hidden = false;
      progress.setAttribute('aria-busy', 'true');
      $('overview-scan-status').textContent = '正在读取 Windows 与硬件信息…';
    }
    try {
      const result = await api().hardware_overview(!!force);
      if (!result?.ok) throw new Error(result?.error || '读取失败');
      $('hardware-inventory').innerHTML = inventoryMarkup(result);
      $('hardware-scanned-at').textContent = `读取于 ${new Date(result.scannedAt).toLocaleString()}`;
      const systemInfo = $('overview-system-info');
      const systemDetail = $('overview-system-detail');
      if (systemInfo) systemInfo.textContent = [result.os?.name,
        result.os?.version ? `版本 ${result.os.version}` : null,
        result.os?.architecture].filter(Boolean).join(' · ') || 'Windows 信息未报告';
      if (systemDetail) systemDetail.textContent = [
        result.cpu?.name ? `处理器 ${result.cpu.name}` : '处理器未报告',
        result.gpu?.name ? `显卡 ${result.gpu.name}` : '显卡未报告'
      ].join(' · ');
      if (status) status.textContent = '已读取 Windows 实际报告的硬件信息；无法识别的项目会如实标为未报告。';
    } catch (error) {
      if (status) status.textContent = '硬件清单读取失败。请检查 Windows 管理信息服务后重试。';
      if ($('overview-system-info')) $('overview-system-info').textContent = '系统与硬件信息读取失败。';
      if ($('overview-scan-status')) $('overview-scan-status').textContent = '读取失败，请检查 Windows 管理信息服务后重试。';
    } finally {
      if (button) button.disabled = false;
      if (progress) {
        progress.setAttribute('aria-busy', 'false');
        progress.hidden = true;
      }
    }
  }

  function renderScores(scores, state) {
    const target = $('benchmark-results');
    if (!target) return;
    if (!scores) {
      target.innerHTML = '';
      if ($('benchmark-verdict')) $('benchmark-verdict').hidden = true;
      return;
    }
    const fields = [['综合', 'WinSPRLevel'], ['处理器', 'CPUScore'], ['内存', 'MemoryScore'],
      ['存储', 'DiskScore'], ['图形', 'GraphicsScore'], ['Direct3D', 'D3DScore']];
    target.innerHTML = fields.map(([label, key]) => {
      const value = scores[key] == null ? NaN : Number(scores[key]);
      return `<div class="benchmark-score"><span>${label}</span><strong>${Number.isFinite(value) ? value.toFixed(1) : '—'}</strong></div>`;
    }).join('');
    const verdict = $('benchmark-verdict');
    if (verdict) {
      const subscoreFields = fields.filter(([, key]) => key !== 'WinSPRLevel');
      const subscores = subscoreFields.map(([label, key]) => ({
        label, value: Number(scores[key])
      })).filter(item => Number.isFinite(item.value) && item.value > 0);
      const averageSubscore = subscores.length
        ? subscores.reduce((sum, item) => sum + item.value, 0) / subscores.length
        : null;
      const bestSubscore = subscores.reduce((best, item) => !best || item.value > best.value ? item : best, null);
      const composite = Number(scores.WinSPRLevel);
      const score = averageSubscore ?? composite;
      const complete = ['complete', 'saved'].includes(state);
      verdict.hidden = !complete || !Number.isFinite(score) || score <= 0;
      if (!verdict.hidden) {
        const tier = score >= 8 ? '高性能' : score >= 6 ? '主流性能' : '基础性能';
        const detail = bestSubscore
          ? `常规综合 ${score.toFixed(1)} · 当前最高 ${bestSubscore.label} ${bestSubscore.value.toFixed(1)}`
          : `Windows 综合评分 ${score.toFixed(1)}`;
        verdict.innerHTML = `<span class="benchmark-verdict-brand" aria-hidden="true"><img src="assets/lxultra-mark.png" alt=""></span><div class="benchmark-verdict-copy"><strong>${tier}</strong><small>${detail}</small></div>`;
      }
    }
  }

  function updateBenchmarkElapsed() {
    if (!benchmarkStartedAt) return;
    const elapsed = Math.floor((Date.now() - benchmarkStartedAt) / 1000);
    const minutes = String(Math.floor(elapsed / 60)).padStart(2, '0');
    const seconds = String(elapsed % 60).padStart(2, '0');
    const target = $('benchmark-elapsed');
    if (target) target.textContent = `已运行 ${minutes}:${seconds}`;
    const estimated = Math.min(95, Math.max(1, Math.floor(elapsed / 90 * 95)));
    const progress = $('benchmark-progress');
    const bar = $('benchmark-progress-bar');
    const value = $('benchmark-progress-value');
    if (progress) {
      progress.setAttribute('aria-valuenow', String(estimated));
      progress.setAttribute('aria-valuetext', `估算进度 ${estimated}%，根据已用时间估算`);
    }
    if (bar) bar.style.width = `${estimated}%`;
    if (value) value.textContent = `${estimated}%`;
    const activityText = $('benchmark-activity-text');
    if (activityText) activityText.textContent = `跑分进度约 ${estimated}% · 时间估算，非 Windows 实时百分比`;
  }

  function renderBenchmark(state) {
    if (!state) return;
    const running = state.state === 'running';
    $('benchmark-status').textContent = state.message || '评测状态未知。';
    $('benchmark-start').disabled = running;
    $('benchmark-cancel').disabled = !running;
    $('benchmark-panel')?.classList.toggle('is-running', running);
    $('benchmark-panel')?.classList.toggle('is-finished', state.state === 'complete' && previousBenchmarkState === 'running');
    $('benchmark-panel')?.setAttribute('aria-busy', String(running));
    $('benchmark-progress').hidden = !running;
    $('benchmark-elapsed').hidden = !running;
    const activity = $('benchmark-activity');
    if (activity) activity.hidden = !running;
    $('benchmark-progress')?.setAttribute('aria-valuetext', running ? 'Windows 正在评测，百分比为用时估算' : '评测已停止');
    renderScores(state.scores, state.state);
    if (running) {
      if (!benchmarkStartedAt) benchmarkStartedAt = Date.now();
      updateBenchmarkElapsed();
      if (!benchmarkElapsedTimer) benchmarkElapsedTimer = window.setInterval(updateBenchmarkElapsed, 1000);
      if (!benchmarkTimer) benchmarkTimer = window.setInterval(pollBenchmark, 1500);
    } else {
      benchmarkStartedAt = null;
      if (benchmarkElapsedTimer) { clearInterval(benchmarkElapsedTimer); benchmarkElapsedTimer = null; }
      if (benchmarkTimer) { clearInterval(benchmarkTimer); benchmarkTimer = null; }
    }
    previousBenchmarkState = state.state;
  }

  async function pollBenchmark() {
    try { renderBenchmark(await api().benchmark_status()); }
    catch { $('benchmark-status').textContent = '无法读取评测状态，请稍后重试。'; }
  }

  async function startBenchmark() {
    if (!api()) return;
    const button = $('benchmark-start'); button.disabled = true;
    try { renderBenchmark(await api().benchmark_start()); }
    catch { $('benchmark-status').textContent = '无法启动 Windows 评测，请确认 WinSAT 可用。'; button.disabled = false; }
  }

  function initialize() {
    $('hardware-refresh')?.addEventListener('click', () => refreshHardware(true));
    $('overview-scan-devices')?.addEventListener('click', () => refreshHardware(true));
    $('benchmark-start')?.addEventListener('click', startBenchmark);
    $('benchmark-cancel')?.addEventListener('click', async () => {
      try { renderBenchmark(await api().benchmark_cancel()); }
      catch { $('benchmark-status').textContent = '停止评测的请求未能完成。'; }
    });
    if (api()) { refreshHardware(false); pollBenchmark(); }
  }

  window.LxHardwareUI = { initialize, refresh: refreshHardware };
}());
