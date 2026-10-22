// Run with apps/desktop/node_modules/electron/cli.js after pnpm run build.
// Real Electron window/preload/renderer; RPC data is a fixture in an isolated profile.
const { app, ipcMain, session } = require('electron');
const assert = require('node:assert/strict');
const { mkdirSync, mkdtempSync, writeFileSync } = require('node:fs');
const { resolve } = require('node:path');
const { pathToFileURL } = require('node:url');
const { setTimeout: delay } = require('node:timers/promises');

mkdirSync(resolve('.tmp'), { recursive: true });
const profile = mkdtempSync(resolve('.tmp/startup-e2e-'));
app.setPath('userData', profile);
const mode = process.argv[2] || 'normal';
const diagnostics = [];
let releaseModule;
let window;
const deadline = setTimeout(() => { console.error('startup probe timed out'); app.exit(1); }, 30000);
async function until(check) {
  for (let n = 0; n < 200; n++) {
    if (await check()) return;
    await delay(50);
  }
  throw new Error(`startup condition timed out: ${mode}`);
}

app.whenReady().then(async () => {
  ipcMain.handle('desktop:invoke', (_event, request) => ({ value: {
    id: request.id || 'fixture', type: 'response', method: request.method, error: null,
    payload: request.method === 'sessions.list' ? { sessions: [] } : { ok: true },
  }, assets: [] }));
  ipcMain.handle('desktop:settings-read', () => ({ formData: { models: { registrations: [] } } }));
  ipcMain.on('desktop:renderer-diagnostic', (_event, payload) => diagnostics.push(payload));
  const { registerDesktopContentSecurityPolicy } = await import(pathToFileURL(resolve('apps/desktop/dist/windowSecurity.js')).href);
  registerDesktopContentSecurityPolicy(session.defaultSession.webRequest);
  session.defaultSession.webRequest.onBeforeRequest({ urls: ['file:///*'] }, (details, callback) => {
    if (/mountSessionApp-[^/]+\.js$/.test(details.url) && mode !== 'normal') {
      if (mode === 'module-error') callback({ cancel: true });
      else releaseModule = () => callback({});
    } else callback({});
  });
  const { createDesktopWindow } = await import(pathToFileURL(resolve('apps/desktop/dist/window.js')).href);
  window = createDesktopWindow({ openLocalAttachment: async () => {} });
  assert.equal(window.isVisible(), false, 'native empty window must not be shown before its first paint');
  let firstPaint = false;
  window.once('ready-to-show', () => { firstPaint = true; });
  window.webContents.debugger.attach('1.3');
  const evaluate = async expression => {
    const result = await window.webContents.debugger.sendCommand('Runtime.evaluate', { expression, returnByValue: true });
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.text);
    return result.result.value;
  };
  if (mode === 'delayed' || mode === 'render-error') {
    await until(async () => releaseModule && await evaluate("!!document.getElementById('startup-message')"));
    await delay(1000);
    assert.match(await evaluate("document.getElementById('startup-message').textContent"), /正在打开工作区/);
    assert.equal(await evaluate("document.getElementById('root').childElementCount"), 0);
    if (mode === 'render-error') {
      await evaluate("Storage.prototype.getItem = () => { throw new Error('injected render failure'); }");
    }
    releaseModule();
  }
  if (mode === 'module-error') {
    await until(async () => /加载失败/.test(await evaluate("document.getElementById('startup-message')?.textContent || ''")));
    assert.equal(await evaluate("!!document.querySelector('#startup-status a')"), true);
    await until(() => diagnostics.some(d => d.details?.phase === 'startup'));
  } else if (mode === 'render-error') {
    await until(async () => /界面暂时不可用/.test(await evaluate('document.body.innerText')));
    assert.equal(await evaluate("!!document.getElementById('startup-status')"), false);
    await until(() => diagnostics.some(d => d.kind === 'error-boundary'));
  } else {
    await until(async () => /本地服务已连接/.test(await evaluate('document.body.innerText')));
    assert.equal(await evaluate("!!document.getElementById('startup-status')"), false);
  }
  await until(() => firstPaint && window.isVisible());
  // A user-triggered reload must recover both import failure and render failure.
  if (mode.endsWith('error')) {
    session.defaultSession.webRequest.onBeforeRequest(null);
    await evaluate(mode === 'module-error'
      ? "document.querySelector('#startup-status a').click()"
      : "Array.from(document.querySelectorAll('button')).find(b => b.textContent === '重新加载').click()");
    await until(async () => /本地服务已连接/.test(await evaluate('document.body.innerText')));
  }
  const report = { mode, first_paint_before_show: true, final_visible: window.isVisible(), diagnostics: diagnostics.map(d => d.kind), reload_recovered: mode.endsWith('error'), profile };
  writeFileSync(resolve(`.tmp/desktop-startup-${mode}.json`), JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report));
  clearTimeout(deadline);
  app.exit(0);
}).catch(error => { console.error(error); clearTimeout(deadline); app.exit(1); });
