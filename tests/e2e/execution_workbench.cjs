// Hidden real Electron renderer/preload + Python journal/model boundary fixture.
// Run after build: electron tests/e2e/execution_workbench.cjs
const { app, BrowserWindow, ipcMain } = require('electron');
const assert = require('node:assert/strict');
const { mkdirSync, mkdtempSync, readFileSync, writeFileSync } = require('node:fs');
const { resolve } = require('node:path');
const { pathToFileURL } = require('node:url');
const { setTimeout: delay } = require('node:timers/promises');
mkdirSync(resolve('.tmp'), { recursive: true });
const workspace = mkdtempSync(resolve('.tmp/execution-ui-'));
app.disableHardwareAcceleration();
app.setPath('userData', resolve(workspace, 'profile'));
let client;
const deadline = setTimeout(() => { console.error('execution UI timeout'); app.exit(1); }, 60000);
app.whenReady().then(async () => {
  const { DesktopBridgeClient } = await import(pathToFileURL(resolve('apps/desktop/dist/bridge/bridgeClient.js')).href);
  client = new DesktopBridgeClient({ executable: resolve('.venv/Scripts/python.exe'), args: [resolve('tests/e2e/execution_disposition_backend.py'), workspace], cwd: process.cwd() });
  await client.start();
  const lostAttempts = new Map();
  ipcMain.handle('desktop:invoke', async (_event, request) => {
    let value = await client.invoke(request);
    if (request.method === 'chat.send' && request.payload.content.startsWith('UI lost')) {
      const content = request.payload.content;
      const ids = lostAttempts.get(content) ?? [];
      ids.push(request.payload.execution_request_id); lostAttempts.set(content, ids);
      if (ids.length === 1) value = { ...value, payload: {}, error: { code: content.endsWith('exit') ? 'bridge_exit' : 'bridge_timeout', message: 'UI lost response after commit' } };
    }
    return { value, assets: [] };
  });
  ipcMain.handle('desktop:settings-read', () => ({ formData: { models: { registrations: [] } } }));
  ipcMain.handle('desktop:bridge-status', () => ({ mode: 'local', running: true }));
  const attachment = resolve(workspace, 'fixture.txt');
  writeFileSync(attachment, 'UI attachment');
  ipcMain.handle('desktop:pick-chat-attachments', () => ({ value: [attachment], assets: [] }));
  const window = new BrowserWindow({ width: 1320, height: 900, show: false, webPreferences: { preload: resolve('apps/desktop/dist/preload.js'), contextIsolation: true, nodeIntegration: false, sandbox: false, backgroundThrottling: false } });
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
  await click('执行记录');
  await until("document.querySelectorAll('.execution-list button').length === 2");
  await evaluate("Array.from(document.querySelectorAll('.execution-list button')).find(b => b.textContent.includes('desktop:external')).click()");
  await until("document.querySelector('.execution-detail')?.textContent.includes('needs_review')");
  assert.equal(await evaluate("Array.from(document.querySelectorAll('button')).find(b => b.textContent === '按原输入重试').disabled"), true);
  // Keyboard focus and native form submission persist the review through real RPC.
  await evaluate("document.querySelector('.execution-detail form select').value = 'no_effect'; document.querySelector('.execution-detail form select').dispatchEvent(new Event('change', { bubbles: true }))");
  await evaluate("document.querySelector('.execution-detail textarea').focus()");
  window.webContents.insertText('初次核对未发现副作用');
  await click('保存处置结论');
  await until("document.querySelector('.execution-detail')?.textContent.includes('撤销尚未使用的重试许可')");
  assert.equal(await evaluate("Array.from(document.querySelectorAll('button')).find(b => b.textContent === '按原输入重试').disabled"), false);
  await evaluate("document.querySelector('.execution-detail textarea').focus()");
  window.webContents.insertText('发现延迟回执，撤销重试许可');
  await click('保存处置结论');
  await until("document.querySelector('.execution-detail')?.textContent.includes('核对结论：结束处置')");
  await evaluate("Array.from(document.querySelectorAll('.execution-list button')).find(b => b.textContent.includes('desktop:safe')).click()");
  await until("document.querySelector('.execution-detail')?.textContent.includes('failed')");
  await click('加载后续事件');
  await until("document.querySelector('.execution-detail')?.textContent.includes('run.ended')");
  await click('按原输入重试');
  await until("Array.from(document.querySelectorAll('button')).some(b => b.textContent === '查看关联重试')");
  assert.equal(readFileSync(resolve(workspace, 'model-calls.txt'), 'utf8').trim(), 'call');
  await click('查看关联重试');
  await until("document.querySelector('.execution-detail')?.textContent.includes('completed')");
  assert.equal(await evaluate("Array.from(document.querySelectorAll('button')).find(b => b.textContent === '按原输入重试').disabled"), true);
  await delay(400);
  writeFileSync(resolve('.tmp/execution-workbench-light.png'), (await window.webContents.capturePage()).toPNG());
  await evaluate("document.querySelector('[aria-label=\"切换墨纸主题\"]').click()");
  await until("document.querySelector('.session-app').dataset.theme === 'dark'");
  await delay(400);
  writeFileSync(resolve('.tmp/execution-workbench-dark.png'), (await window.webContents.capturePage()).toPNG());
  window.setSize(640, 900);
  await delay(100);
  assert.equal(await evaluate("document.documentElement.scrollWidth <= window.innerWidth"), true);
  window.setSize(1320, 900);
  await click('对话');
  await until("document.querySelector('.composer textarea') && !document.querySelector('.composer textarea').disabled");
  await evaluate("document.querySelector('.composer textarea').focus()");
  window.webContents.insertText('UI uncertain');
  await evaluate("document.querySelector('[aria-label=\"添加附件\"]').click()");
  await until("document.querySelector('.attachment-list')?.textContent.includes('fixture.txt')");
  await evaluate("document.querySelector('.composer').requestSubmit()");
  await until("document.body.innerText.includes('请勿直接重发') && document.querySelector('[aria-label=\"发送消息\"]')");
  assert.equal(await evaluate("document.querySelector('.composer textarea').value"), '');
  assert.equal(await evaluate("document.querySelector('.attachment-list')"), null);
  assert.equal(await evaluate("document.body.innerText.includes('输入和附件已恢复')"), false);
  assert.equal(readFileSync(resolve(workspace, 'uncertain-effects.txt'), 'utf8').trim(), 'effect');
  const rows = await client.invoke({ method: 'executions.list', payload: { status: 'needs_review' } });
  const uncertain = rows.payload.executions.find(row => row.input.content === 'UI uncertain');
  assert.ok(uncertain?.request_key);
  assert.equal(await evaluate(`document.body.innerText.includes(${JSON.stringify(uncertain.turn_id)})`), true);
  // A fresh identity from an old client must not bypass the UI's safe path.
  const resend = await client.invoke({ method: 'chat.send', payload: {
    session_key: uncertain.session_key, content: 'UI uncertain', media: [attachment], execution_request_id: 'fresh-ui-resend',
  } });
  assert.equal(resend.error?.code, 'execution_needs_review');
  assert.equal(readFileSync(resolve(workspace, 'uncertain-effects.txt'), 'utf8').trim(), 'effect');
  for (const mode of ['exit', 'timeout']) {
    await click('新会话');
    await until("document.querySelector('.message-scroll')?.classList.contains('is-empty')");
    const content = `UI lost ${mode}`;
    await evaluate("document.querySelector('.composer textarea').focus()");
    window.webContents.insertText(content);
    await evaluate("document.querySelector('.composer').requestSubmit()");
    await until("document.body.innerText.includes('UI lost response after commit') && document.querySelector('[aria-label=\"发送消息\"]')");
    assert.equal(await evaluate("document.querySelector('.composer textarea').value"), '');
    assert.equal(await evaluate("document.body.innerText.includes('输入和附件已恢复')"), false);
    // A manually re-entered pending attempt must reuse its identity.
    await evaluate("document.querySelector('.composer textarea').focus()");
    window.webContents.insertText(content);
    await evaluate("document.querySelector('.composer').requestSubmit()");
    await until("!document.body.innerText.includes('UI lost response after commit') && document.querySelector('[aria-label=\"发送消息\"]')");
    assert.equal(lostAttempts.get(content).length, 2);
    assert.equal(lostAttempts.get(content)[0], lostAttempts.get(content)[1]);
  }
  assert.equal(readFileSync(resolve(workspace, 'confirmed-effects.txt'), 'utf8').trim().split('\n').length, 2);
  await client.stop();
  writeFileSync(resolve('.tmp/execution-workbench.json'), JSON.stringify({ reviewed: true, unsafe_retry_blocked: true, safe_retry_model_calls: 1, event_pagination: true, narrow_reflow: true, uncertain_draft_not_restored: true, uncertain_attachments_not_restored: true, fresh_identity_resend_blocked: true, clearance_revocation: true, lost_response_reuses_identity: true, workspace }, null, 2));
  console.log('Execution workbench verified: review, retry, event pagination, narrow layout.');
  clearTimeout(deadline); app.exit(0);
}).catch(async error => { console.error(error); clearTimeout(deadline); if (client) await client.stop(); app.exit(1); });
