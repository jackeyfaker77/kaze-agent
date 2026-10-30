/* Isolated renderer fixture: no backend, model, channel, or production configuration is loaded. */
const { contextBridge } = require('electron');
const listeners = new Set();
const calls = [];
const errors = [];
const mode = process.argv.includes('--fixture-remote') ? 'remote' : 'local';
const sessions = {
  'desktop:a': { session_key: 'desktop:a', title: 'Fixture A', messages: [
    { id: 'old-user', role: 'user', content: '历史问题' },
    { id: 'old-answer', role: 'assistant', content: '历史正文完整', reasoning_content: '历史思考', metadata: { request_id: 'old', turn_metrics: { thinking_duration_ms: 12500 } },
      tool_chain: [{ text: '历史检查', calls: [{ call_id: 'old-call', name: 'read', arguments: { path: 'history.txt' }, final_arguments: { path: 'history.txt' }, status: 'success', result: '历史预览' }] }] },
  ] },
  'desktop:b': { session_key: 'desktop:b', title: 'Fixture B', messages: [{ id: 'b', role: 'assistant', content: '另一个会话' }] },
};
let pending, sequence = 0, heldGet, holdGet = false;
let startupFailure = mode === 'remote';
const clone = value => JSON.parse(JSON.stringify(value));
const publish = (method, payload = {}, id = pending?.id || 'fixture') => {
  for (const listener of listeners) listener({ id, type: 'event', method, payload: { session_key: pending?.key || 'desktop:a', ...payload } });
};
const response = (id, method, payload) => ({ id, method, type: 'response', payload, error: null });
function saveAnswer(interrupted = false) {
  const p = pending;
  const assistant = { id: p.id, role: 'assistant', content: interrupted ? '已保存的部分回复' : '最终正文只出现一次', reasoning_content: '第二轮思考',
    metadata: { request_id: p.id, execution_request_id: p.attempt, interrupted_reply: interrupted, turn_metrics: { thinking_duration_ms: 1200 } },
    tool_chain: [{ reasoning_content: '第一轮思考', text: '我先检查文件', calls: [{ call_id: 'c1', name: 'read', arguments: { path: 'fixture.txt' }, final_arguments: { path: 'fixture.txt' }, status: interrupted ? 'unknown' : 'success', result: interrupted ? '' : '检查通过' }] }] };
  if (!sessions[p.key].messages.some(m => m.id === p.id)) sessions[p.key].messages.push(assistant);
  return clone(sessions[p.key]);
}
const api = {
  bridgeStatus: async () => ({ mode }),
  readSettings: async () => ({ formData: { models: { registrations: [] } } }),
  onEvent: fn => { listeners.add(fn); return () => listeners.delete(fn); },
  onVoiceState: () => () => {},
  reportRendererDiagnostic: value => errors.push(value),
  openExternal: async () => {},
  invoke: async ({ method, payload }) => {
    calls.push(method);
    const id = `rpc-${++sequence}`;
    if (method === 'health' && startupFailure) {
      startupFailure = false;
      return { id, method, type: 'response', payload: {}, error: { code: 'remote_unavailable', message: 'fixture: Host not ready yet' } };
    }
    if (method === 'sessions.list') return response(id, method, { sessions: Object.values(sessions).map(s => ({ key: s.session_key, metadata: { title: s.title }, message_count: s.messages.length })) });
    if (method === 'session.get') {
      const value = response(id, method, clone(sessions[payload.session_key] || { session_key: payload.session_key, title: '', messages: [] }));
      if (holdGet) { holdGet = false; return new Promise(resolve => { heldGet = () => resolve(value); }); }
      return value;
    }
    if (method === 'chat.send') return new Promise(resolve => {
      const key = payload.session_key;
      const streamId = mode === 'remote' ? `host-client:${id}` : id;
      sessions[key] ||= { session_key: key, title: 'Fixture draft', messages: [] };
      sessions[key].messages.push({ id: `${id}:user`, role: 'user', content: payload.content, metadata: { request_id: streamId, execution_request_id: payload.execution_request_id } });
      pending = { id: streamId, rpcId: id, key, attempt: payload.execution_request_id, resolve };
    });
    if (method === 'chat.cancel') {
      const p = pending;
      p.resolve(response(p.rpcId, 'chat.send', { cancelled: true, session: saveAnswer(true) }));
      pending = undefined;
      return response(id, method, { cancelled: true });
    }
    return response(id, method, {});
  },
};
contextBridge.exposeInMainWorld('miraDesktop', api);
contextBridge.exposeInMainWorld('conversationFixture', {
  emit: (method, payload = {}, id) => publish(method, payload, id),
  commit: () => publish('chat.done', { session: saveAnswer() }),
  resolve: () => { const p = pending; p.resolve(response(p.rpcId, 'chat.send', { session: clone(sessions[p.key]) })); pending = undefined; },
  holdSnapshot: () => { holdGet = true; },
  releaseSnapshot: () => { heldGet?.(); heldGet = undefined; },
  audit: () => ({ calls, errors, pending: pending?.id, snapshotHeld: Boolean(heldGet) }),
});
