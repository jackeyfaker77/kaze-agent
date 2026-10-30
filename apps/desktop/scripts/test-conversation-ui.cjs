/* Run after build:renderer. Uses real React/Electron with a fake bridge and isolated userData. */
const path = require('node:path');
const fs = require('node:fs');
const os = require('node:os');
if (!process.versions.electron) {
  const { spawn } = require('node:child_process');
  const env = { ...process.env }; delete env.ELECTRON_RUN_AS_NODE;
  const child = spawn(require('electron'), [__filename], { env, windowsHide: true, stdio: 'inherit' });
  child.on('error', error => { console.error(error); process.exitCode = 1; });
  child.on('exit', code => { process.exitCode = code ?? 1; });
} else {
  const assert = require('node:assert/strict');
  const { app, BrowserWindow } = require('electron');
  const root = path.resolve(__dirname, '../../..');
  const scratch = fs.mkdtempSync(path.join(os.tmpdir(), 'kaze-conversation-ui-'));
  app.setPath('userData', scratch);
  app.disableHardwareAcceleration();
  app.on('window-all-closed', () => {});
  const timeout = setTimeout(() => { console.error('conversation UI timeout'); app.exit(1); }, 60000);
  app.whenReady().then(async () => {
    for (const mode of ['local', 'remote']) {
      const win = new BrowserWindow({ show: false, width: 1100, height: 850, webPreferences: {
        preload: path.join(__dirname, 'conversation-ui-fixture.cjs'), contextIsolation: true, nodeIntegration: false, backgroundThrottling: false,
        additionalArguments: mode === 'remote' ? ['--fixture-remote'] : [],
      } });
      const run = code => win.webContents.executeJavaScript(code);
      const until = async (code, label) => {
        for (let i = 0; i < 100; i++) {
          if (await run(code)) return;
          await new Promise(resolve => setTimeout(resolve, 30));
        }
        throw new Error(label + ': ' + await run('document.body.innerText'));
      };
      const click = selector => run(`document.querySelector(${JSON.stringify(selector)}).click()`);
      const send = async () => {
        await run(`(() => { const input=document.querySelector('textarea'); Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(input,'测试分块'); input.dispatchEvent(new Event('input',{bubbles:true})); })()`);
        await until(`!document.querySelector('[aria-label="发送消息"]').disabled`, 'send enabled');
        await click('[aria-label="发送消息"]');
        await until('Boolean(window.conversationFixture.audit().pending)', 'send started');
      };
      const stream = async () => {
        const count = await run(`document.querySelectorAll('.message-process').length`);
        await run(`window.conversationFixture.emit('chat.delta',{thinking_delta:'第一轮思考',content_delta:'我先检查文件'})`);
        await run(`window.conversationFixture.emit('chat.tool.started',{iteration:1,call_id:'c1',tool_name:'read',arguments:{path:'fixture.txt'}})`);
        await until(`document.querySelectorAll('.message-process').length===${count + 1}`, 'live process');
      };
      await win.loadFile(path.join(root, 'apps/desktop/renderer-dist/index.html'));
      if (mode === 'remote') {
        await until(`document.body.innerText.includes('fixture: Host not ready yet')`, 'startup error visible');
        await run(`window.conversationFixture.emit('bridge.connected',{})`);
        await until(`!document.body.innerText.includes('fixture: Host not ready yet')`, 'recovered connection clears startup error');
      }
      await until(`document.body.innerText.includes('${mode === 'local' ? '本地' : '远程'}服务已连接')`, 'connected');
      await run(`Array.from(document.querySelectorAll('.conversation-list button')).find(e=>e.textContent.includes('Fixture A')).click()`);
      await until(`document.body.innerText.includes('历史正文完整')`, 'history');
      assert.equal(await run(`document.querySelector('.message-process button').getAttribute('aria-expanded')`), 'false');
      await click('.message-process button');
      await until(`document.querySelector('.message-thinking button')!==null`, 'history details');
      await run(`document.querySelector('.message-thinking button').focus()`);
      win.webContents.debugger.attach('1.3');
      await win.webContents.debugger.sendCommand('Emulation.setFocusEmulationEnabled', { enabled: true });
      await run(`document.querySelector('.message-thinking button').focus()`);
      await win.webContents.debugger.sendCommand('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Enter', code: 'Enter', text: '\r', unmodifiedText: '\r', windowsVirtualKeyCode: 13 });
      await win.webContents.debugger.sendCommand('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13 });
      await until(`document.querySelector('.message-thinking button').getAttribute('aria-expanded')==='true'`, 'keyboard disclosure');
      assert.equal(await run(`Array.from(document.querySelectorAll('.message-disclosure .chat-tool-call-content>div')).some(e=>e.scrollTop>0)`), false, 'focus must not scroll a clipped disclosure container');
      await send(); await stream();
      // Explicit reader choice must survive completion and the durable handoff.
      await run(`(() => { const p=Array.from(document.querySelectorAll('.message-process')).at(-1); const b=p.querySelector('button'); if(b.getAttribute('aria-expanded')==='true') b.click(); })()`);
      await until(`Array.from(document.querySelectorAll('.message-process')).at(-1).querySelector('button').getAttribute('aria-expanded')==='false'`, 'reader closes process');
      await run(`Array.from(document.querySelectorAll('.message-process')).at(-1).querySelector('button').click()`);
      await until(`Array.from(document.querySelectorAll('.message-process')).at(-1).querySelector('.message-tool button')!==null`, 'tool visible');
      await run(`Array.from(document.querySelectorAll('.message-process')).at(-1).querySelector('.message-tool button').click()`);
      await run(`window.conversationFixture.emit('chat.tool.completed',{iteration:1,call_id:'c1',tool_name:'read',arguments:{path:'fixture.txt'},final_arguments:{path:'fixture.txt'},status:'success',result_preview:'检查通过'})`);
      await run(`window.conversationFixture.emit('chat.delta',{thinking_delta:'第二轮思考',content_delta:'最终正文只出现一次'})`);
      await until(`document.querySelectorAll('.message-answer').length===2`, 'stream answer');
      await run(`window.conversationFixture.holdSnapshot();window.conversationFixture.emit('sessions.changed')`);
      await until(`window.conversationFixture.audit().snapshotHeld`, 'stale snapshot captured');
      await run(`window.conversationFixture.commit()`);
      await until(`document.querySelectorAll('.message-answer').length===2 && !document.body.innerText.includes('正在等待回复')`, 'commit no duplicate');
      assert.equal(await run(`Array.from(document.querySelectorAll('.message-process')).at(-1).querySelector('.message-tool button').getAttribute('aria-expanded')`), 'true');
      await run(`window.conversationFixture.releaseSnapshot();window.conversationFixture.resolve()`);
      await until(`Boolean(document.querySelector('[aria-label="发送消息"]'))`, 'RPC settled');
      assert.equal(await run(`document.querySelectorAll('.message-answer').length`), 2);
      if (mode === 'remote') {
        await click('[aria-label="切换墨纸主题"]');
        await until(`document.querySelector('.session-app').dataset.theme==='dark'`, 'dark theme');
      }
      await win.webContents.capturePage(); // Request a paint from the hidden window before visual capture.
      await new Promise(resolve => setTimeout(resolve, 400));
      await run(`document.querySelector('.message-scroll').scrollTop=document.querySelector('.message-scroll').scrollHeight`);
      await new Promise(resolve => setTimeout(resolve, 100));
      fs.mkdirSync(path.join(root, '.tmp'), { recursive: true });
      fs.writeFileSync(path.join(root, '.tmp', `conversation-ui-${mode}.png`), (await win.webContents.capturePage()).toPNG());
      await send(); await stream();
      await click('[aria-label="停止回复"]');
      await until(`document.body.innerText.includes('已保存的部分回复') && !document.querySelector('[aria-label="停止回复"]')`, 'cancel snapshot without done event');
      assert.equal(await run(`document.querySelectorAll('.message-answer').length`), 3);
      await send(); await stream();
      await run(`window.conversationFixture.emit('bridge.exit');window.conversationFixture.emit('bridge.connected')`);
      await until(`document.body.innerText.includes('等待历史同步')`, 'disconnect retains visible evidence');
      await run(`window.conversationFixture.emit('chat.delta',{content_delta:'不得重复追加'})`);
      assert.equal(await run(`document.body.innerText.includes('不得重复追加')`), false);
      await run(`window.conversationFixture.commit();window.conversationFixture.resolve()`);
      await until(`!document.body.innerText.includes('等待历史同步') && !document.querySelector('[aria-label="停止回复"]')`, 'reconnect handoff');
      await run(`Array.from(document.querySelectorAll('.conversation-list button')).find(e=>e.textContent.includes('Fixture B')).click()`);
      await until(`document.body.innerText.includes('另一个会话')`, 'session switch');
      assert.equal(await run(`document.body.innerText.includes('最终正文只出现一次')`), false);
      await run(`Array.from(document.querySelectorAll('.conversation-list button')).find(e=>e.textContent.includes('Fixture A')).click()`);
      await until(`document.querySelectorAll('.message-answer').length===4`, 'reopen history');
      const audit = await run('window.conversationFixture.audit()');
      assert.deepEqual(audit.errors, []);
      assert.equal(audit.calls.filter(call => call === 'chat.send').length, 3);
      assert.equal(audit.calls.filter(call => call === 'chat.cancel').length, 1);
      console.log(`PASS conversation UI ${mode}: history, keyboard, ordered blocks, stable handoff, stale snapshot, cancel, reconnect, session isolation; sends=3 cancel=1`);
      win.destroy();
    }
    clearTimeout(timeout); app.exit(0);
  }).catch(error => { console.error(error); clearTimeout(timeout); app.exit(1); });
}
