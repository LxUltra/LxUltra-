/* User-controlled settings for the LxUltra desktop app. */
(() => {
  const $ = id => document.getElementById(id);
  let context = null;
  let weatherPlaces = [];
  let lastWeatherAt = 0;

  function renderSensors() {
    const names = {cpu:'CPU',gpu:'\u663e\u5361',memory:'\u5185\u5b58',disk:'\u786c\u76d8',motherboard:'\u4e3b\u677f'};
    const metrics = context.metrics() || {};
    const telemetry = metrics.temperatures || {};
    const stage = telemetry.sensorStage || 'initializing';
    let availableCount = 0;
    for (const [key, name] of Object.entries(names)) {
      const output = $(`settings-sensor-current-${key}`);
      if (!output) continue;
      const reading = telemetry[key] || {};
      const fallback = key === 'cpu' ? metrics.cpu : key === 'gpu' ? metrics.gpu : null;
      const value = reading.temperature ?? fallback?.temperature;
      const source = reading.name || (fallback?.available && fallback.temperature != null
        ? `${fallback.name || name} \u6e29\u5ea6` : '');
      const hasReading = value != null && Number.isFinite(Number(value));
      if (source) availableCount += 1;
      output.dataset.available = String(!!source);
      output.textContent = source
        ? `${source}${hasReading ? ` · ${Number(value)} °C` : ''}`
        : stage === 'initializing' || stage === 'expanded' ? '\u81ea\u52a8\u8bc6\u522b\u4e2d…'
          : stage === 'failed' ? '\u4f20\u611f\u5668\u8bfb\u53d6\u5931\u8d25'
          : '\u672a\u68c0\u6d4b\u5230\u6e29\u5ea6\u4f20\u611f\u5668';
      output.title = output.textContent;
    }
    const status = $('settings-sensor-status');
    if (status) {
      status.textContent = stage === 'initializing' ? '\u6b63\u5728\u81ea\u52a8\u68c0\u6d4b\u4f20\u611f\u5668…' :
        stage === 'expanded' ? '\u6b63\u5728\u8865\u9f50\u5176\u4ed6\u786c\u4ef6\u8bfb\u6570…' :
        stage === 'failed' ? (availableCount
          ? '\u4f20\u611f\u5668\u8bfb\u53d6\u5931\u8d25\uff1b\u5df2\u4fdd\u7559\u81ea\u52a8\u5339\u914d\u5230\u7684\u53ef\u7528\u8bfb\u6570\u3002'
          : '\u4f20\u611f\u5668\u8bfb\u53d6\u5931\u8d25\uff0c\u8bf7\u91cd\u542f\u8f6f\u4ef6\u540e\u91cd\u8bd5\u3002') :
        availableCount ? `\u5df2\u81ea\u52a8\u5339\u914d ${availableCount} \u4e2a\u53ef\u7528\u6e29\u5ea6\u6765\u6e90\u3002` :
          '\u8bbe\u5907\u672a\u62a5\u544a\u53ef\u7528\u6e29\u5ea6\u4f20\u611f\u5668\u3002';
    }
  }

  async function savePreference(key, value) {
    const previous = context.config[key];
    context.config[key] = value;
    try {
      const result = await context.api()?.save_config(context.config);
      if (!result?.ok) throw new Error(result?.error || '保存失败');
      return true;
    } catch (error) {
      context.config[key] = previous;
      context.toast(error.message || '设置保存失败，请重试。', 'warn');
      return false;
    }
  }

  async function loadGeneralSettings() {
    if (!context?.api()) return;
    try {
      const startup = await context.api().app_startup_status();
      $('settings-startup').checked = !!startup.enabled;
      context.setText('settings-startup-status', startup.conflict
        ? '发现同名启动项指向其他程序，未做修改。' : startup.error || '');
    } catch {
      context.setText('settings-startup-status', '暂时无法读取开机自启状态。');
    }
    const config = context.config;
    $('settings-dark-mode').checked = (config.appearanceMode || 'light') === 'dark';
    $('settings-weather-enabled').checked = !!config.weatherEnabled;
    $('settings-abnormal-reports').checked = !!config.abnormalShutdownReports;
    $('settings-experience-program').checked = !!config.experienceProgram;
    $('settings-run-shutdown-report').disabled = !config.abnormalShutdownReports;
    $('settings-weather-location').value = config.weatherLocation?.name || '';
    context.setText('settings-experience-status', config.experienceProgram
      ? '已加入：仅在本机保存功能页面访问次数，不上传。'
      : '当前未加入；不会收集体验数据，也不会上传。');
    renderSensors();
    if (config.weatherEnabled && config.weatherLocation && Date.now() - lastWeatherAt > 30 * 60 * 1000) {
      refreshWeather(config.weatherLocation);
    }
  }

  function showSection(section) {
    const selected = section === 'version' ? 'version' : 'general';
    document.querySelectorAll('[data-settings-panel]').forEach(panel => {
      panel.hidden = panel.dataset.settingsPanel !== selected;
    });
    document.querySelectorAll('[data-settings-section]').forEach(button => {
      const active = button.dataset.settingsSection === selected;
      button.classList.toggle('active', active);
      button.setAttribute('aria-pressed', String(active));
    });
    if (selected === 'general') loadGeneralSettings();
    else context.setText('settings-current-version', context.version || '本机版本');
  }

  async function checkUpdates() {
    const button = $('settings-check-updates');
    const progress = $('settings-update-progress');
    const bar = $('settings-update-progress-bar');
    button.disabled = true;
    progress.hidden = false;
    progress.setAttribute('aria-valuenow', '0');
    bar.style.width = '8%';
    context.setText('settings-version-status', '正在检查更新源…');
    try {
      const result = await context.api()?.check_updates();
      context.setText('settings-current-version', result?.currentVersion || context.version || '本机版本');
      if (result?.available) {
        context.setText('settings-version-status', result.message || '发现新版本，正在准备下载…');
        context.toast(`发现新版本 ${result.latestVersion}，开始自动下载。`, 'good');
        const started = await context.api()?.start_update_install();
        if (!started?.ok) throw new Error(started?.message || '无法开始更新。');
        while (true) {
          await new Promise(resolve => setTimeout(resolve, 500));
          const status = await context.api()?.get_update_status();
          if (!status) throw new Error('无法读取更新进度。');
          const percent = Math.max(0, Math.min(100, Number(status.percent) || 0));
          progress.setAttribute('aria-valuenow', String(percent));
          bar.style.width = `${Math.max(8, percent)}%`;
          context.setText('settings-version-status', status.message || '正在更新…');
          context.setText('update-status', status.message || '正在更新…');
          if (status.state === 'failed') throw new Error(status.message || '更新失败，请重试。');
          if (status.state === 'up-to-date') break;
          // The verified installer takes over and closes this window during installation.
        }
        return;
      }
      const message = result?.message || '暂时无法检查更新，请稍后重试。';
      context.setText('settings-version-status', message);
      context.setText('update-status', message);
      context.toast(message, result?.ok ? 'good' : 'warn');
      if (!result?.ok) throw new Error(message);
    } catch (error) {
      const message = error?.message || '暂时无法检查更新，请稍后重试。';
      context.setText('settings-version-status', message);
      context.setText('update-status', message);
      context.toast(message, 'warn');
    } finally {
      button.disabled = false;
      const bridge = context.api();
      const status = bridge ? await bridge.get_update_status().catch(() => null) : null;
      if (!status || status.state === 'failed' || status.state === 'up-to-date') progress.hidden = true;
    }
  }

  async function searchWeather() {
    const query = $('settings-weather-location').value.trim();
    if (!query) {
      context.setText('settings-weather-current', '请输入城市或地区名称。');
      return;
    }
    const button = $('settings-weather-search');
    button.disabled = true;
    context.setText('settings-weather-current', '正在搜索天气位置…');
    try {
      const result = await context.api()?.weather_search(query);
      weatherPlaces = result?.locations || [];
      const list = $('settings-weather-places');
      list.replaceChildren();
      weatherPlaces.forEach((place, index) => {
        const choice = document.createElement('button');
        choice.type = 'button';
        choice.className = 'btn small';
        choice.dataset.weatherPlace = String(index);
        choice.textContent = [place.name, place.admin1, place.country].filter(Boolean).join(' · ');
        list.append(choice);
      });
      if (!result?.ok) context.setText('settings-weather-current', result?.error || '天气位置查询失败。');
      else if (!weatherPlaces.length) context.setText('settings-weather-current', '没有找到位置，请换个名称试试。');
    } catch {
      context.setText('settings-weather-current', '天气位置查询失败，请检查网络后重试。');
    } finally {
      button.disabled = false;
    }
  }

  async function refreshWeather(place = context.config.weatherLocation) {
    if (!place || !context.api()) return;
    context.setText('settings-weather-current', `正在读取 ${place.name} 的天气…`);
    try {
      const result = await context.api().weather_current(place);
      if (!result?.ok) throw new Error(result?.error || '天气信息暂不可用。');
      lastWeatherAt = Date.now();
      const summary = `${result.location} · ${result.weather} · ${result.temperature ?? '—'} °C`;
      context.setText('settings-weather-current', `${summary} · 湿度 ${result.humidity ?? '—'}% · 风速 ${result.windSpeed ?? '—'} km/h`);
      context.setText('widget-weather', summary);
      $('widget-weather').hidden = !context.config.weatherEnabled;
    } catch (error) {
      context.setText('settings-weather-current', error.message || '天气信息暂不可用，请稍后重试。');
      context.setText('widget-weather', '天气暂不可用');
      $('widget-weather').hidden = !context.config.weatherEnabled;
    }
  }

  function bind() {
    document.querySelectorAll('[data-settings-section]').forEach(button => {
      button.addEventListener('click', () => showSection(button.dataset.settingsSection));
    });
    $('settings-check-updates').addEventListener('click', checkUpdates);
    $('settings-startup').addEventListener('change', async event => {
      const enabled = event.target.checked;
      const result = await context.api()?.set_app_startup(enabled);
      if (!result?.ok) {
        event.target.checked = !enabled;
        context.toast(result?.error || '开机自启设置失败。', 'warn');
      } else context.setText('settings-startup-status', enabled ? '已加入当前用户开机启动。' : '已关闭开机自启。');
    });
    $('settings-dark-mode').addEventListener('change', async event => {
      const previous = context.config.appearanceMode || 'light';
      const previousExplicit = !!context.config.appearanceModeExplicit;
      const mode = event.target.checked ? 'dark' : 'light';
      context.config.appearanceMode = mode;
      context.config.appearanceModeExplicit = true;
      context.applyTheme();
      if (!(await savePreference('appearanceMode', mode))) {
        context.config.appearanceMode = previous;
        context.config.appearanceModeExplicit = previousExplicit;
        event.target.checked = previous === 'dark';
        context.applyTheme();
      }
    });
    $('settings-weather-enabled').addEventListener('change', async event => {
      const enabled = event.target.checked;
      if (!(await savePreference('weatherEnabled', enabled))) {
        event.target.checked = !enabled;
        return;
      }
      if (enabled && context.config.weatherLocation) refreshWeather();
      else if (!enabled) {
        $('widget-weather').hidden = true;
        context.setText('settings-weather-current', '天气显示已关闭。');
      }
    });
    $('settings-weather-search').addEventListener('click', searchWeather);
    $('settings-weather-refresh').addEventListener('click', () => refreshWeather());
    $('settings-weather-location').addEventListener('keydown', event => {
      if (event.key === 'Enter') {
        event.preventDefault();
        searchWeather();
      }
    });
    $('settings-weather-places').addEventListener('click', async event => {
      const button = event.target.closest('[data-weather-place]');
      if (!button) return;
      const place = weatherPlaces[Number(button.dataset.weatherPlace)];
      if (!place) return;
      const result = await context.api()?.save_config({...context.config, weatherLocation: place});
      if (!result?.ok) {
        context.setText('settings-weather-current', result?.error || '天气位置保存失败。');
        return;
      }
      context.config.weatherLocation = place;
      $('settings-weather-location').value = place.name;
      $('settings-weather-places').replaceChildren();
      refreshWeather(place);
    });
    $('settings-abnormal-reports').addEventListener('change', async event => {
      const enabled = event.target.checked;
      if (!(await savePreference('abnormalShutdownReports', enabled))) {
        event.target.checked = !enabled;
        return;
      }
      $('settings-run-shutdown-report').disabled = !enabled;
      context.setText('settings-shutdown-status', enabled ? '已启用本地异常关机报告。' : '异常关机报告已关闭。');
    });
    $('settings-run-shutdown-report').addEventListener('click', async event => {
      const button = event.currentTarget;
      button.disabled = true;
      context.setText('settings-shutdown-status', '正在读取本机最近 30 天系统日志…');
      $('settings-shutdown-events').replaceChildren();
      try {
        const result = await context.api()?.abnormal_shutdown_report();
        if (!result?.ok) throw new Error(result?.error || '读取报告失败。');
        const events = result.events || [];
        context.setText('settings-shutdown-status', events.length
          ? `最近 ${result.days} 天发现 ${events.length} 条异常关机记录（仅来自本机系统日志）。`
          : `最近 ${result.days} 天没有发现异常关机记录。`);
        for (const row of events) {
          const item = document.createElement('li');
          item.textContent = `事件 ${Number(row.id)} · ${row.time}`;
          $('settings-shutdown-events').append(item);
        }
      } catch (error) {
        context.setText('settings-shutdown-status', error.message || '读取报告失败。');
      } finally {
        button.disabled = !context.config.abnormalShutdownReports;
      }
    });
    $('settings-experience-program').addEventListener('change', async event => {
      const enabled = event.target.checked;
      if (!(await savePreference('experienceProgram', enabled))) {
        event.target.checked = !enabled;
        return;
      }
      context.setText('settings-experience-status', enabled
        ? '已加入：仅在本机保存功能页面访问次数，不上传。'
        : '已退出：后续不再记录体验数据。');
    });

  }

  window.LxGeneralSettings = {
    initialize(value) { context = value; bind(); },
    enter() { showSection('general'); },
    updateSensors() { renderSensors(); },
    recordPage(page) {
      if (context?.config.experienceProgram) context.api()?.record_experience(page);
    }
  };
})();
