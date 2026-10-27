// Real renderer -> preload -> RPC -> Python authority, isolated workspace.
const { app, BrowserWindow, ipcMain } = require('electron');
const assert = require('node:assert/strict');
const { mkdirSync, mkdtempSync, existsSync, readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const { pathToFileURL } = require('node:url');
const { setTimeout: delay } = require('node:timers/promises');
mkdirSync(resolve('.tmp'), { recursive: true });
const workspace = mkdtempSync(resolve('.tmp/continuation-ui-'));
app.disableHardwareAcceleration();
app.setPath('userData', resolve(workspace, 'profile'));
let client;
const deadline = setTimeout(() => { console.error('Continuation UI timeout'); app.exit(1); }, 60000);
app.whenReady().then(async () => {
  const { DesktopBridgeClient } = await import(pathToFileURL(resolve('apps/desktop/dist/bridge/bridgeClient.js')).href);
  client = new DesktopBridgeClient({ executable: resolve('.venv/Scripts/python.exe'), args: [resolve('tests/e2e/continuation_workbench_backend.py'), workspace], cwd: process.cwd() });
  await client.start();
  ipcMain.handle('desktop:invoke', async (_event, request) => ({ value: await client.invoke(request), assets: [] }));
  ipcMain.handle('desktop:settings-read', () => ({ formData: { models: { registrations: [] } } }));
  ipcMain.handle('desktop:bridge-status', () => ({ mode: 'local', running: true }));
  const window = new BrowserWindow({ width: 1100, height: 850, show: false, webPreferences: { preload: resolve('apps/desktop/dist/preload.js'), contextIsolation: true, nodeIntegration: false, sandbox: false, backgroundThrottling: false } });
  client.on('event', event => window.webContents.send('desktop:event', { value: event, assets: [] }));
  await window.loadFile(resolve('apps/desktop/renderer-dist/index.html'));
  const evaluate = expression => window.webContents.executeJavaScript(expression);
  async function until(expression) {
    for (let n = 0; n < 300; n++) { if (await evaluate(expression)) return; await delay(30); }
    throw new Error('UI condition timed out: ' + expression);
  }
  async function click(text) {
    await evaluate(`Array.from(document.querySelectorAll('button')).find(b => b.textContent.trim() === ${JSON.stringify(text)}).click()`);
  }
  await until("document.body.innerText.includes('本地服务已连接')");
  await click('工作台'); await click('执行记录');
  await until("document.querySelectorAll('.execution-list button').length === 1");
  await evaluate("document.querySelector('.execution-list button').click()");
  await until("document.querySelector('.execution-detail')?.textContent.includes('interrupted')");
  await click('查看恢复计划');
  await until("document.body.innerText.includes('确认创建续跑')");
  assert.equal(existsSync(resolve(workspace, 'new-model.txt')), false);
  await click('取消续跑');
  assert.equal(existsSync(resolve(workspace, 'new-model.txt')), false);
  await click('查看恢复计划');
  await until("document.body.innerText.includes('确认创建续跑')");
  // Native rapid double click is fenced in UI and by the persistent claim.
  await evaluate("{const b=Array.from(document.querySelectorAll('button')).find(b=>b.textContent==='确认创建续跑'); b.click();b.click();}");
  await until("document.querySelector('.execution-detail')?.textContent.includes('completed')");
  assert.equal(readFileSync(resolve(workspace, 'new-model.txt'), 'utf8').trim(), 'call');
  await click('查看续跑来源');
  await until("document.body.innerText.includes('查看关联续跑')");
  await click('查看恢复计划');
  await until("document.body.innerText.includes('该边界已创建续跑')");
  assert.equal(await evaluate("Array.from(document.querySelectorAll('button')).some(b=>b.textContent==='确认创建续跑')"), false);
  await client.stop(); window.destroy(); clearTimeout(deadline);
  console.log(JSON.stringify({ok:true, previewReadOnly:true, cancelReadOnly:true, modelCalls:1, lineage:true}));
  app.exit(0);
}).catch(async error => { console.error(error); clearTimeout(deadline); if(client) await client.stop(); app.exit(1); });
