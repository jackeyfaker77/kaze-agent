// Hidden real Electron renderer/preload + Python journal/model boundary fixture.
// Run after build: electron tests/e2e/execution_workbench.cjs
const { app, BrowserWindow, ipcMain } = require('electron');
const assert = require('node:assert/strict');
const { mkdirSync, mkdtempSync, readFileSync, writeFileSync } = require('node:fs');
const { resolve } = require('node:path');
const { pathToFileURL } = require('node:url');
const { setTimeout: delay } = require('node:timers/promises');
mkdirSync(resolve('.tmp'), { recursive: true });
const workspace = mkdtempSync(resolve('.tmp/transport-ui-'));
app.disableHardwareAcceleration();
app.setPath('userData', resolve(workspace, 'profile'));
let client;
const deadline = setTimeout(() => { console.error('execution UI timeout'); app.exit(1); }, 60000);
app.whenReady().then(async () => {
  const { DesktopBridgeClient } = await import(pathToFileURL(resolve('apps/desktop/dist/bridge/bridgeClient.js')).href);
  const proactive = process.argv.includes('--proactive');
  client = new DesktopBridgeClient({ executable: resolve('.venv/Scripts/python.exe'), args: [resolve('tests/e2e/transport_review_backend.py'), workspace, ...(proactive ? ['--proactive'] : [])], cwd: process.cwd() });
  await client.start();
  ipcMain.handle('desktop:invoke', async (_event, request) => ({ value: await client.invoke(request), assets: [] }));
  ipcMain.handle('desktop:settings-read', () => ({ formData: { models: { registrations: [] } } }));
  ipcMain.handle('desktop:bridge-status', () => ({ mode: 'local', running: true }));
  const attachment = resolve(workspace, 'fixture.txt');
  writeFileSync(attachment, 'UI attachment');
  ipcMain.handle('desktop:pick-chat-attachments', () => ({ value: [attachment], assets: [] }));
  const window = new BrowserWindow({ width: 1320, height: 900, show: false, webPreferences: { preload: resolve('apps/desktop/dist/preload.js'), contextIsolation: true, nodeIntegration: false, sandbox: false, backgroundThrottling: false, offscreen: true } });
  client.on('event', event => window.webContents.send('desktop:event', { value: event, assets: [] }));
  await window.loadFile(resolve('apps/desktop/renderer-dist/index.html'));
  const evaluate = expression => window.webContents.executeJavaScript(expression);
  async function until(expression) {
    for (let n = 0; n < 300; n++) { if (await evaluate(expression)) return; await delay(30); }
    throw new Error(`UI condition timed out: ${expression}`);
  }
  async function click(text) {
    await evaluate(`Array.from(document.querySelectorAll('button')).find(b => b.textContent.trim() === ${JSON.stringify(text)}).click()`);
  }
  await until("document.body.innerText.includes('本地服务已连接')");
  await click('工作台');
  await click('收发队列');
  await until("document.querySelectorAll('.execution-list button').length === 1");
  await evaluate("document.querySelector('.execution-list button').click()");
  await until("document.querySelector('[aria-label=\"队列详情\"]')?.textContent.includes('ui-unknown')");
  const retryDisabled = "Array.from(document.querySelectorAll('button')).find(b => b.textContent === '确认核对并重新排队').disabled";
  assert.equal(await evaluate(retryDisabled), true);
  await evaluate("document.querySelector('.execution-detail textarea').focus()");
  window.webContents.insertText('核对原账户目标：第一段已送达，第二段未送达');
  assert.equal(await evaluate(retryDisabled), true);
  await evaluate("document.querySelector('.execution-detail input[type=checkbox]').click()");
  assert.equal(await evaluate(retryDisabled), false);
  await click('确认核对并重新排队');
  await until("document.querySelector('.execution-detail h3')?.textContent.includes('等待处理')");
  assert.equal(await evaluate("document.querySelector('.execution-detail').textContent.includes('已有成功回执')"), true);
  await evaluate("document.querySelector('.execution-detail textarea').focus()");
  window.webContents.insertText('撤回未执行的队列重试');
  await click('结束队列处置');
  await until("document.querySelector('.execution-detail h3')?.textContent.includes('已结束处置')");
  await delay(200);
  writeFileSync(resolve('.tmp/transport-workbench-light.png'), (await window.webContents.capturePage()).toPNG());
  await evaluate("Array.from(document.querySelectorAll('button')).find(b => b.textContent.startsWith('查看执行 ')).click()");
  await until("document.querySelector('.execution-detail')?.textContent.includes('needs_review')");
  if (proactive) {
    await until("document.querySelector('.execution-detail')?.textContent.includes('proactive.observed')");
    assert.equal(await evaluate("Array.from(document.querySelectorAll('button')).find(b => b.textContent === '按原输入重试').disabled"), true);
  }
  await click('查看关联收发队列');
  await until("document.querySelectorAll('.execution-list button').length === 1");
  await evaluate("document.querySelector('.execution-list button').click()");
  await until("document.querySelector('.execution-detail')?.textContent.includes('已结束处置')");
  await evaluate("document.querySelector('[aria-label=\"切换墨纸主题\"]').click()");
  await delay(200);
  writeFileSync(resolve('.tmp/transport-workbench-dark.png'), (await window.webContents.capturePage()).toPNG());
  window.setSize(640, 900); await delay(200);
  assert.equal(await evaluate("document.documentElement.scrollWidth <= window.innerWidth"), true);
  await client.stop();
  writeFileSync(resolve(proactive ? '.tmp/proactive-workbench.json' : '.tmp/transport-workbench.json'), JSON.stringify({ proactive_facts_visible: proactive, reviewed: true, partial_receipts_preserved: true, unknown_part_confirmation_required: true, retry_revoked_before_worker: true, execution_links: true, narrow_reflow: true, workspace }, null, 2));
  console.log('Transport workbench verified.');
  clearTimeout(deadline); app.exit(0);
}).catch(async error => { console.error(error); clearTimeout(deadline); if (client) await client.stop(); app.exit(1); });
