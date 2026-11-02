/* Focused model-page check with synthetic login; never opens a browser or calls OpenAI. */
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
  app.setPath('userData', fs.mkdtempSync(path.join(os.tmpdir(), 'kaze-codex-ui-')));
  app.disableHardwareAcceleration();
  app.on('window-all-closed', () => {});
  const timeout = setTimeout(() => app.exit(1), 30000);
  app.whenReady().then(async () => {
    for (const resident of [false, true]) {
      const win = new BrowserWindow({ show:false, webPreferences: {
        preload:path.join(__dirname, 'conversation-ui-fixture.cjs'), contextIsolation:true,
        nodeIntegration:false, backgroundThrottling:false, additionalArguments:resident ? ['--fixture-resident'] : [],
      } });
      const run = code => win.webContents.executeJavaScript(code);
      const until = async code => {
        for (let i = 0; i < 150; i++) {
          if (await run(code)) return;
          await new Promise(resolve => setTimeout(resolve, 30));
        }
        throw new Error(code + ': ' + await run('document.body.innerText'));
      };
      const clickText = text => run(`Array.from(document.querySelectorAll('button')).find(b=>b.textContent.trim()===${JSON.stringify(text)}).click()`);
      await win.loadFile(path.resolve(__dirname, '../renderer-dist/index.html'));
      if (resident) {
        await until(`document.body.innerText.includes('fixture: Host not ready yet')`);
        await run(`window.conversationFixture.emit('bridge.connected',{})`);
      }
      await until(`document.body.innerText.includes('${resident ? '本机常驻' : '本地'}服务已连接')`);
      await clickText('模型');
      await until(`Array.from(document.querySelectorAll('.connection-row')).some(b=>b.textContent.includes('Codex') && !b.disabled)`);
      await run(`Array.from(document.querySelectorAll('.connection-row')).find(b=>b.textContent.includes('Codex')).click()`);
      await until(`Boolean(document.querySelector('.codex-login-button:not(:disabled)'))`);
      await clickText('登录 ChatGPT');
      await until(`document.body.innerText.includes('请在浏览器中完成 ChatGPT 登录和授权。')`);
      await until(`document.body.innerText.includes('已保存 ChatGPT 登录授权')`);
      assert.equal(await run(`document.querySelector('.codex-connection select').value`), 'fixture-codex');
      await clickText('保存连接');
      await until(`!document.querySelector('.codex-connection') && document.body.innerText.includes('fixture-codex')`);
      // Reopening must retain the OAuth form in resident mode as well.
      await run(`Array.from(document.querySelectorAll('.connection-row')).find(b=>b.textContent.includes('fixture-codex')).click()`);
      await until(`document.body.innerText.includes('已保存 ChatGPT 登录授权')`);
      assert.equal(await run(`document.querySelector('.codex-connection input[type=password]')`), null);
      const audit = await run('window.conversationFixture.audit()');
      assert(audit.calls.some(c=>c.startsWith('external:https://auth.openai.com/oauth/authorize')));
      assert(audit.calls.includes('codex.models') && audit.calls.includes('settings.save'));
      assert.deepEqual(audit.errors, []);
      win.destroy();
      console.log(`Codex model login UI passed: ${resident ? 'resident' : 'local'}`);
    }
    clearTimeout(timeout); app.exit(0);
  }).catch(error => { console.error(error); clearTimeout(timeout); app.exit(1); });
}
