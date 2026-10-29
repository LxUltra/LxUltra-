(() => {
  'use strict';

  const DEFAULTS = Object.freeze({
    focusMinutes: 25,
    shortBreakMinutes: 5,
    longBreakMinutes: 15,
    longBreakEvery: 4,
  });
  const OPTIONS = Object.freeze({
    focusMinutes: [15, 20, 25, 30, 45, 50],
    shortBreakMinutes: [3, 5, 10],
    longBreakMinutes: [10, 15, 20, 30],
    longBreakEvery: [3, 4, 5, 6],
  });

  function normalizeSettings(input = {}) {
    const source = input && typeof input === 'object' ? input : {};
    const result = {};
    for (const key of Object.keys(DEFAULTS)) {
      const value = Number(source[key]);
      result[key] = OPTIONS[key].includes(value) ? value : DEFAULTS[key];
    }
    return result;
  }

  function durationSeconds(phase, settings) {
    const config = normalizeSettings(settings);
    if (phase === 'focus') return config.focusMinutes * 60;
    if (phase === 'shortBreak') return config.shortBreakMinutes * 60;
    if (phase === 'longBreak') return config.longBreakMinutes * 60;
    throw new RangeError('Unknown Pomodoro phase');
  }

  function createState(settings = DEFAULTS, completedFocus = 0) {
    const count = Number.isSafeInteger(Number(completedFocus)) && Number(completedFocus) >= 0
      ? Number(completedFocus)
      : 0;
    return {
      phase: 'focus',
      remainingSeconds: durationSeconds('focus', settings),
      completedFocus: count,
      running: false,
      deadlineMs: 0,
    };
  }

  function restoreState(settings, saved = {}, now = Date.now()) {
    const state = createState(settings, saved && saved.completedFocus);
    if (!saved || typeof saved !== 'object' || Array.isArray(saved)) return state;

    const durations = {
      focus: durationSeconds('focus', settings),
      shortBreak: durationSeconds('shortBreak', settings),
      longBreak: durationSeconds('longBreak', settings),
    };
    const phase = saved.phase;
    const remaining = Number(saved.remainingSeconds);
    if (!Object.hasOwn(durations, phase) || !Number.isSafeInteger(remaining) ||
        remaining < 1 || remaining > durations[phase]) {
      return state;
    }

    state.phase = phase;
    state.remainingSeconds = remaining;
    if (saved.running === true) {
      const deadline = Number(saved.deadlineMs);
      if (!Number.isFinite(deadline) || !Number.isFinite(now)) return createState(settings, state.completedFocus);
      state.running = true;
      state.deadlineMs = deadline;
      state.remainingSeconds = remainingSeconds(state, now);
      if (state.remainingSeconds === 0) tick(state, settings, now);
    }
    return state;
  }

  function start(state, now = Date.now()) {
    if (!state.running && Number.isFinite(now)) {
      state.deadlineMs = now + Math.max(1, state.remainingSeconds) * 1000;
      state.running = true;
    }
    return state;
  }

  function pause(state, now = Date.now()) {
    if (state.running) {
      state.remainingSeconds = Math.max(1, Math.ceil((state.deadlineMs - now) / 1000));
      state.deadlineMs = 0;
      state.running = false;
    }
    return state;
  }

  function remainingSeconds(state, now = Date.now()) {
    if (!state.running) return Math.max(0, state.remainingSeconds);
    return Math.max(0, Math.ceil((state.deadlineMs - now) / 1000));
  }

  function tick(state, settings, now = Date.now()) {
    if (!state.running) return null;
    state.remainingSeconds = remainingSeconds(state, now);
    if (state.remainingSeconds > 0) return null;

    const completedPhase = state.phase;
    state.running = false;
    state.deadlineMs = 0;
    if (completedPhase === 'focus') {
      state.completedFocus += 1;
      const config = normalizeSettings(settings);
      state.phase = state.completedFocus % config.longBreakEvery === 0
        ? 'longBreak'
        : 'shortBreak';
    } else {
      state.phase = 'focus';
    }
    state.remainingSeconds = durationSeconds(state.phase, settings);
    return {
      completedPhase,
      nextPhase: state.phase,
      completedFocus: state.completedFocus,
    };
  }

  function reset(state, settings) {
    state.phase = 'focus';
    state.remainingSeconds = durationSeconds('focus', settings);
    state.completedFocus = 0;
    state.running = false;
    state.deadlineMs = 0;
    return state;
  }

  const api = Object.freeze({
    DEFAULTS,
    normalizeSettings,
    durationSeconds,
    createState,
    restoreState,
    start,
    pause,
    remainingSeconds,
    tick,
    reset,
  });

  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (typeof window !== 'undefined') window.LxPomodoro = api;
})();
