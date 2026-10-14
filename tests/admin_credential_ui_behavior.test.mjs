import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import vm from 'node:vm';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const identityPath = resolve(root, 'src/wenjin/file_management/admin/identity.js');
const identitySource = readFileSync(identityPath, 'utf8');
const FIXED_NOW = Date.parse('2026-10-14T12:00:00Z');
class FixedDate extends Date { static now() { return FIXED_NOW; } }

class Element {
  constructor() {
    this.children = []; this.listeners = new Map(); this.values = {};
    const classes = new Set();
    this.classList = {
      add(...names) { names.forEach((name) => classes.add(name)); },
      remove(...names) { names.forEach((name) => classes.delete(name)); },
      toggle(name, force) { const enabled = force === undefined ? !classes.has(name) : Boolean(force); if (enabled) classes.add(name); else classes.delete(name); return enabled; },
      contains(name) { return classes.has(name); },
    };
    this.elements = { username: { value: '' }, password: { value: '' }, old_password: { value: '' }, new_password: { value: '' } };
  }
  addEventListener(name, callback) { if (!this.listeners.has(name)) this.listeners.set(name, []); this.listeners.get(name).push(callback); }
  removeEventListener(name, callback) { this.listeners.set(name, (this.listeners.get(name) || []).filter((item) => item !== callback)); }
  dispatch(name, event = {}) { for (const callback of this.listeners.get(name) || []) callback(event); }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = [...items]; }
  setAttribute(name, value) { (this.attributes ||= {})[name] = String(value); }
  querySelector(selector) {
    const find = (node) => { for (const child of node.children || []) { if (selector === 'button' && child.tagName === 'button') return child; const nested = find(child); if (nested) return nested; } return null; };
    if (find(this)) return find(this);
    this._queries ||= new Map();
    if (!this._queries.has(selector)) { const child = new Element(); child.tagName = selector; this._queries.set(selector, child); }
    return this._queries.get(selector);
  }
  get lastElementChild() { return this.children.at(-1) || (this._lastElementChild ||= new Element()); }
  focus() { this.focused = true; }
  select() { this.selected = true; }
  showModal() { this.open = true; }
  close() { this.open = false; }
  reset() { this.values = {}; for (const input of Object.values(this.elements)) input.value = ''; }
}
function response(body, status = 200) { return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } }); }
function deferred() { let resolve; const promise = new Promise((done) => { resolve = done; }); return { promise, resolve }; }
async function until(predicate, message) {
  for (let i = 0; i < 40; i += 1) { if (predicate()) return; await new Promise((resolve) => setImmediate(resolve)); }
  assert.ok(predicate(), message);
}
function startIdentity(fetchHandler, { clipboard } = {}) {
  const elements = new Map(); const requests = []; const events = []; const confirms = [];
  const document = {
    querySelector(selector) { if (!elements.has(selector)) elements.set(selector, new Element()); return elements.get(selector); },
    querySelectorAll() { return []; },
    createElement(tag) { const node = new Element(); node.tagName = tag; return node; },
    createTextNode(text) { return { textContent: String(text) }; },
  };
  const window = {
    location: { origin: 'http://127.0.0.1' },
    navigator: clipboard ? { clipboard } : {},
    confirm(message) { confirms.push(message); return true; },
    dispatchEvent(event) { events.push(event); return true; }, addEventListener() {},
  };
  const fetch = async (path, options = {}) => { requests.push({ path: String(path), options }); return fetchHandler(String(path), options); };
  const context = {
    document, window, fetch, Headers, Response, Date: FixedDate, Intl, CustomEvent: globalThis.CustomEvent,
    FormData: class { constructor(form) { this.form = form; } get(name) { return this.form.values?.[name] ?? ''; } },
    Option: class Option { constructor(text, value) { this.text = text; this.value = value; } }, console,
  };
  vm.runInNewContext(identitySource, context, { filename: identityPath });
  return { identity: window.WenjinIdentity, elements, requests, events, confirms, window };
}
const base = ({ user, users = [user], onRequest = () => response({}), clipboard } = {}) => startIdentity(async (path, options) => {
  if (path === '/api/auth/mode') return response({ mode: 'accounts', session_policy: { idle_seconds: 1800, absolute_seconds: 43200 } });
  if (path === '/api/auth/me') return response({ user });
  if (path === '/api/auth/csrf') return response({ csrf_token: 'csrf-one' });
  if (path === '/api/auth/credentials' && (options.method || 'GET') === 'GET') return response({ credentials: [] });
  if (path === '/api/admin/users') return response({ users });
  if (path === '/api/admin/resources') return response({ resources: [], grants: [] });
  if (path === '/api/admin/identity-audit?limit=100') return response({ events: [] });
  return onRequest(path, options);
}, { clipboard });
const waitReady = async (ui) => { await ui.identity.ready; };
const cellText = (row) => row.children.map((cell) => cell.textContent).join(' ');

test('reader revokes its own credential through the personal route and reloads the revoked state', async () => {
  const reader = { user_id: 'reader-revoke', username: '普通读者', role: 'reader', active: true, must_change_password: false };
  const rows = [{ credential_id: 'own-key', label: '我的终端', token_prefix: 'safe_prefix', created_at: '2026-10-14T00:00:00Z', expires_at: null, revoked_at: null }];
  const deleted = [];
  const ui = startIdentity(async (path, options) => {
    if (path === '/api/auth/mode') return response({ mode: 'accounts' });
    if (path === '/api/auth/me') return response({ user: reader });
    if (path === '/api/auth/csrf') return response({ csrf_token: 'synthetic' });
    if (path === '/api/auth/credentials') return response({ credentials: rows });
    if (path === '/api/auth/credentials/own-key' && options.method === 'DELETE') {
      deleted.push(path); rows[0].revoked_at = '2026-10-14T12:00:00Z';
      return response({ ok: true });
    }
    throw new Error(`unexpected route ${path}`);
  });
  await waitReady(ui);
  await until(() => ui.elements.get('#my-credentials').children[0]?.children[1], 'personal credential list should load');
  const row = ui.elements.get('#my-credentials').children[0].children[1].children[0];
  await row.children.at(-1).children[0].onclick();
  assert.deepEqual(deleted, ['/api/auth/credentials/own-key']);
  assert.equal(ui.requests.some((request) => request.path.startsWith('/api/admin/')), false);
  const refreshed = ui.elements.get('#my-credentials').children[0].children[1].children[0];
  assert.match(cellText(refreshed), /已撤销/);
  assert.match(cellText(refreshed), /2026-10-14 20:00:00/);
});

test('admin credential list uses fixed Beijing time, classifies expiry, scopes API to selected user, and confirms revoke', async () => {
  const admin = { user_id: 'admin', username: '管理员', role: 'admin', active: true, must_change_password: false };
  const reader = { user_id: 'reader-a', username: '检索员甲', role: 'reader', active: true, must_change_password: false };
  const readerB = { user_id: 'reader-b', username: '检索员乙', role: 'reader', active: true, must_change_password: false };
  const rows = [
    { credential_id: 'valid', user_id: reader.user_id, label: '在线IDE', token_prefix: 'mcp_safe1', created_at: '2026-10-14T00:00:00Z', expires_at: '2026-10-15T00:00:00Z', revoked_at: null },
    { credential_id: 'expired', user_id: reader.user_id, label: '旧构建机', token_prefix: 'mcp_safe2', created_at: '2026-10-13T00:00:00Z', expires_at: '2026-10-14T03:59:59Z', revoked_at: null },
    { credential_id: 'revoked', user_id: reader.user_id, label: '退役机', token_prefix: 'mcp_safe3', created_at: '2026-10-12T00:00:00Z', expires_at: null, revoked_at: '2026-10-13T01:00:00Z' },
  ];
  const deleteCalls = [];
  const ui = base({ user: admin, users: [admin, reader, readerB], onRequest: async (path, options) => {
    if (path === `/api/admin/users/${admin.user_id}/credentials`) return response({ credentials: [] });
    if (path === `/api/admin/users/${reader.user_id}/credentials`) return response({ credentials: rows });
    if (path === `/api/admin/users/${readerB.user_id}/credentials`) return response({ credentials: [] });
    if (path === '/api/admin/credentials/valid' && options.method === 'DELETE') { deleteCalls.push(path); rows[0].revoked_at = '2026-10-14T13:00:00Z'; return response({ ok: true }); }
    throw new Error(`unexpected ${options.method || 'GET'} ${path}`);
  } });
  await waitReady(ui);
  assert.equal(ui.elements.get('#identity-self-panel').classList.contains('hidden'), true, 'admin has one account-scoped credential manager, not a duplicate personal panel');
  assert.equal(ui.elements.get('#identity-admin-panel').classList.contains('hidden'), false);
  assert.equal(ui.requests.some((request) => request.path === '/api/auth/credentials'), false, 'admin does not load a duplicate personal credential list');
  const select = ui.elements.get('#admin-credential-user'); select.value = admin.user_id; select.dispatch('change');
  await until(() => ui.requests.some((request) => request.path === `/api/admin/users/${admin.user_id}/credentials`), 'admin must be selectable as a credential owner');
  select.value = reader.user_id; select.dispatch('change');
  await until(() => ui.elements.get('#admin-user-credentials').children.length > 0, 'selected user credential list should render');
  const table = ui.elements.get('#admin-user-credentials').children[0];
  const body = table.children[1].children;
  const text = body.map(cellText).join('\n');
  assert.match(text, /2026-10-14 08:00:00/);
  assert.match(text, /有效/); assert.match(text, /已过期/); assert.match(text, /已撤销/);
  assert.doesNotMatch(text, /credential token|token_hash/i);
  assert.ok(ui.requests.some((request) => request.path === `/api/admin/users/${reader.user_id}/credentials`));
  assert.ok(ui.requests.every((request) => !JSON.stringify(request.options.body || '').includes('mcp_safe')));
  const revoke = body[0].children.at(-1).children[0]; await revoke.onclick();
  assert.deepEqual(deleteCalls, ['/api/admin/credentials/valid']);
  assert.match(ui.confirms[0], /在线IDE.*mcp_safe1/s);
  assert.ok(ui.requests.some((request) => request.path === `/api/admin/users/${reader.user_id}/credentials`));
});

test('reader can issue only own credential and a pending clipboard completion cannot restore status after account switch', async () => {
  const reader = { user_id: 'reader-own', username: '普通读者', role: 'reader', active: true, must_change_password: false };
  const admin = { user_id: 'admin-other', username: '管理员', role: 'admin', active: true, must_change_password: false };
  const clipboardWrite = deferred(); const copied = [];
  const ui = base({ user: reader, users: [reader], clipboard: { writeText(value) { copied.push(value); return clipboardWrite.promise; } }, onRequest: async (path, options) => {
    if (path === '/api/auth/credentials' && options.method === 'POST') return response({ credential: { credential_id: 'self-new', token: 'mcp_reader_secret', token_prefix: 'mcp_r1', created_at: '2026-10-14T00:00:00Z', expires_at: null } }, 201);
    if (path === '/api/auth/login') return response({ user: admin, csrf_token: 'csrf-admin', must_change_password: false });
    throw new Error(`unexpected ${options.method || 'GET'} ${path}`);
  } });
  await waitReady(ui);
  assert.equal(ui.elements.get('#identity-self-panel').classList.contains('hidden'), false, 'reader uses the personal credential panel');
  assert.equal(ui.elements.get('#identity-admin-panel').classList.contains('hidden'), true);
  assert.equal(ui.elements.get('#identity-session-panel').classList.contains('hidden'), false, 'web login policy is shown in its own session panel');
  const form = ui.elements.get('#credential-form'); form.values = { label: '我的 Agent', expires: '0' };
  await form.onsubmit({ preventDefault() {}, currentTarget: form });
  assert.ok(ui.requests.some((request) => request.path === '/api/auth/credentials' && request.options.method === 'POST'));
  assert.equal(ui.requests.some((request) => request.path.includes('/api/admin/users/')), false, 'reader never uses administrative credential endpoints');
  assert.equal(ui.elements.get('#credential-token').value, 'mcp_reader_secret');
  const copyPromise = ui.elements.get('#credential-copy').onclick();
  assert.deepEqual(copied, ['mcp_reader_secret']);
  const switchPromise = ui.identity.unauthorized();
  const login = ui.elements.get('#identity-login-form'); login.elements.username.value = admin.username; login.elements.password.value = 'synthetic';
  login.dispatch('submit', { preventDefault() {} });
  await switchPromise;
  clipboardWrite.resolve(); await copyPromise;
  assert.equal(ui.elements.get('#credential-token').value, '', 'account change clears the old full credential');
  assert.equal(ui.elements.get('#credential-copy-status').textContent, '', 'late clipboard completion cannot write into the new account UI');
  assert.equal(ui.identity.user.user_id, admin.user_id);
});

test('delayed admin list and issue responses never refill a newly selected user or account', async () => {
  const admin = { user_id: 'admin-race', username: '管理员', role: 'admin', active: true, must_change_password: false };
  const readerA = { user_id: 'reader-a', username: '甲', role: 'reader', active: true, must_change_password: false };
  const readerB = { user_id: 'reader-b', username: '乙', role: 'reader', active: true, must_change_password: false };
  const pendingAList = deferred(); const pendingIssue = deferred(); let issueCount = 0; const deletes = [];
  const ui = base({ user: admin, users: [admin, readerA, readerB], onRequest: async (path, options) => {
    if (path === `/api/admin/users/${readerA.user_id}/credentials` && (options.method || 'GET') === 'GET') return pendingAList.promise;
    if (path === `/api/admin/users/${readerB.user_id}/credentials` && (options.method || 'GET') === 'GET') return response({ credentials: [{ credential_id: 'for-b', user_id: readerB.user_id, label: '乙客户端', token_prefix: 'mcp_b2', created_at: '2026-10-14T00:00:00Z', expires_at: null, revoked_at: null }] });
    if (path === `/api/admin/users/${readerA.user_id}/credentials` && options.method === 'POST') { issueCount += 1; return pendingIssue.promise; }
    if (path.startsWith('/api/admin/credentials/') && options.method === 'DELETE') { deletes.push(path); return response({ ok: true }); }
    if (path === '/api/auth/login') return response({ user: readerB, csrf_token: 'csrf-reader', must_change_password: false });
    throw new Error(`unexpected ${options.method || 'GET'} ${path}`);
  } });
  await waitReady(ui);
  const select = ui.elements.get('#admin-credential-user');
  select.value = readerA.user_id; select.dispatch('change');
  await until(() => ui.requests.some((request) => request.path === `/api/admin/users/${readerA.user_id}/credentials`), 'first user list request should start');
  select.value = readerB.user_id; select.dispatch('change');
  await until(() => ui.elements.get('#admin-user-credentials').children.length > 0, 'second selected user list should render');
  pendingAList.resolve(response({ credentials: [{ credential_id: 'for-a', user_id: readerA.user_id, label: '甲旧列表', token_prefix: 'mcp_a1', created_at: '2026-10-14T00:00:00Z', expires_at: null, revoked_at: null }] }));
  await new Promise((resolve) => setImmediate(resolve));
  assert.match(ui.elements.get('#admin-user-credentials').children[0].children[1].children[0].children[0].textContent, /乙客户端/);

  select.value = readerA.user_id; select.dispatch('change');
  await new Promise((resolve) => setImmediate(resolve));
  const issueForm = ui.elements.get('#admin-credential-form'); issueForm.values = { label: '待完成签发', expires: '2592000' };
  const submit = issueForm.onsubmit({ preventDefault() {}, currentTarget: issueForm });
  await until(() => issueCount === 1, 'issue request should be pending');
  const issueButton = issueForm.querySelector('button');
  assert.equal(issueButton.disabled, true);
  issueForm.onsubmit({ preventDefault() {}, currentTarget: issueForm });
  assert.equal(issueCount, 1, 'pending prevents duplicate issue POSTs');
  select.value = readerB.user_id; select.dispatch('change');
  pendingIssue.resolve(response({ credential: { credential_id: 'late-a', token: 'mcp_late_secret', token_prefix: 'mcp_late', created_at: '2026-10-14T00:00:00Z', expires_at: null } }, 201));
  await submit;
  assert.equal(ui.elements.get('#admin-credential-token').value, '', 'late token for a previous selection is never displayed');
  assert.equal(deletes.length, 0, 'changing the selected user never causes an implicit revoke request');
  assert.match(ui.elements.get('#admin-credential-status').textContent, /未显示完整凭据/);
});

test('late admin issuance after an account switch is discarded without revoking or changing the new account UI', async () => {
  const admin = { user_id: 'admin-race', username: '管理员', role: 'admin', active: true, must_change_password: false };
  const reader = { user_id: 'reader-new', username: '新账号', role: 'reader', active: true, must_change_password: false };
  const pendingIssue = deferred(); let issueCalls = 0; const deletes = [];
  const ui = base({ user: admin, users: [admin, reader], onRequest: async (path, options) => {
    if (path === `/api/admin/users/${admin.user_id}/credentials` && options.method === 'POST') { issueCalls += 1; return pendingIssue.promise; }
    if (path === '/api/auth/login') return response({ user: reader, csrf_token: 'csrf-reader', must_change_password: false });
    if (path.startsWith('/api/admin/credentials/') && options.method === 'DELETE') { deletes.push(path); return response({ ok: true }); }
    throw new Error(`unexpected ${options.method || 'GET'} ${path}`);
  } });
  await waitReady(ui);
  const select = ui.elements.get('#admin-credential-user'); select.value = admin.user_id; select.dispatch('change');
  const form = ui.elements.get('#admin-credential-form'); form.values = { label: '管理员客户端', expires: '0' };
  const submit = form.onsubmit({ preventDefault() {}, currentTarget: form });
  await until(() => issueCalls === 1, 'admin issue request should be pending');
  const switchPromise = ui.identity.unauthorized();
  const login = ui.elements.get('#identity-login-form'); login.elements.username.value = reader.username; login.elements.password.value = 'synthetic';
  login.dispatch('submit', { preventDefault() {} });
  await switchPromise;
  pendingIssue.resolve(response({ credential: { credential_id: 'late-admin', token: 'mcp_late_admin_secret', token_prefix: 'mcp_late', created_at: '2026-10-14T00:00:00Z', expires_at: null } }, 201));
  await submit;
  assert.equal(ui.identity.user.user_id, reader.user_id);
  assert.equal(ui.elements.get('#admin-credential-token').value, '', 'old admin token must not be restored after account change');
  assert.equal(ui.elements.get('#credential-token').value, '', 'the reader account must not receive the admin token');
  assert.equal(deletes.length, 0, 'late completion must never send a delete under the new account');
  assert.equal(issueCalls, 1, 'account switch must not trigger a second issuance');
});

test('successful issue remains visible after list refresh failure and refresh does not reissue', async () => {
  const reader = { user_id: 'reader-refresh', username: '普通用户', role: 'reader', active: true, must_change_password: false };
  let postCount = 0; let listCount = 0;
  const ui = startIdentity(async (path, options) => {
    if (path === '/api/auth/mode') return response({ mode: 'accounts', session_policy: { idle_seconds: 1800, absolute_seconds: 43200 } });
    if (path === '/api/auth/me') return response({ user: reader });
    if (path === '/api/auth/csrf') return response({ csrf_token: 'csrf-reader' });
    if (path === '/api/auth/credentials' && options.method === 'POST') {
      postCount += 1;
      return response({ credential: { credential_id: 'new-once', token: 'mcp_once_secret', token_prefix: 'mcp_once', created_at: '2026-10-14T00:00:00Z', expires_at: null } }, 201);
    }
    if (path === '/api/auth/credentials' && (options.method || 'GET') === 'GET') {
      listCount += 1;
      if (listCount === 2) return response({ detail: 'temporary list failure' }, 503);
      return response({ credentials: [] });
    }
    throw new Error(`unexpected ${options.method || 'GET'} ${path}`);
  });
  await waitReady(ui);
  const form = ui.elements.get('#credential-form'); form.values = { label: '构建 Agent', expires: '0' };
  await form.onsubmit({ preventDefault() {}, currentTarget: form });
  assert.equal(ui.elements.get('#credential-token').value, 'mcp_once_secret');
  assert.match(ui.elements.get('#credential-status').textContent, /列表刷新失败/);
  await ui.elements.get('#credential-refresh').onclick();
  assert.equal(ui.elements.get('#credential-token').value, 'mcp_once_secret', 'a list refresh preserves the one-time delivery');
  assert.equal(postCount, 1, 'refresh never repeats credential issuance');
});
