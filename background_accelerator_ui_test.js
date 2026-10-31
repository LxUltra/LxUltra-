const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync(__dirname + '/background_accelerator_ui.js', 'utf8');
const sandbox = {
  window: {},
  document: { getElementById: () => null },
  console,
};
vm.runInNewContext(source, sandbox, { filename: 'background_accelerator_ui.js' });

const normalize = sandbox.window.LxBackgroundAccelerator?.normalizeSnapshot;
assert.equal(typeof normalize, 'function', 'scanner exposes its safe snapshot normalizer');

const rows = [
  { pid: 101, started: 1.25, name: 'large-bg.exe', exe: 'C:/Apps/large-bg.exe', memory: 200 * 1024 * 1024, cpu: 0, protected: false },
  { pid: 102, started: 1.5, name: 'busy-bg.exe', exe: 'C:/Apps/busy-bg.exe', memory: 80 * 1024 * 1024, cpu: 8, protected: false },
  { pid: 103, started: 1.75, name: 'visible.exe', exe: 'C:/Apps/visible.exe', memory: 300 * 1024 * 1024, cpu: 20, protected: false },
  { pid: 107, started: 2.25, name: 'visible-helper.exe', exe: 'C:/Apps/visible.exe', memory: 300 * 1024 * 1024, cpu: 20, protected: false },
  { pid: 104, started: 2, name: 'system.exe', exe: 'C:/Windows/system.exe', memory: 300 * 1024 * 1024, cpu: 20, protected: true },
  { pid: 108, started: 2.75, name: 'LxUltra_综合优化版_修复版.exe', exe: 'D:/LxUltra/LxUltra_综合优化版_修复版.exe', memory: 300 * 1024 * 1024, cpu: 20, protected: false },
  { pid: 105, started: null, name: 'unknown-identity.exe', exe: 'C:/Apps/unknown.exe', memory: 300 * 1024 * 1024, cpu: 20, protected: false },
  { pid: 106, started: 2.5, name: 'partial.exe', exe: 'C:/Apps/partial.exe', memory: null, cpu: 20, protected: false },
];
const result = normalize(rows, [103]);
assert.deepEqual(Array.from(result.items, item => item.pid), [101, 102]);
assert.equal(result.items.every(item => item.selected === false), true, 'scan suggestions require an explicit selection before acceleration');
assert.equal(result.skipped, 2, 'malformed records are skipped without stopping the scan');
assert.equal(result.protected, 2, 'protected rows and all LxUltra instances are never recommended');
assert.equal(result.visible, 2, 'visible-window rows and their same-executable helpers are never recommended');

const html = fs.readFileSync(__dirname + '/index.html', 'utf8');
assert.match(html, /widget-accelerate'\)\.onclick=\(\)=>\{[^}]*openWidgetAction\('accelerate'\)[^}]*scanWidget\(\)/,
  'desktop-widget acceleration starts a scan immediately');

console.log('PASS background scan filters malformed rows and preserves protected/window exclusions');
