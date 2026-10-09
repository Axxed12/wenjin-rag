import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import vm from 'node:vm';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const identityPath = resolve(root, 'src/wenjin/file_management/admin/identity.js');
const identitySource = readFileSync(identityPath, 'utf8');

class Element {
  constructor() {
    this.children = [];
    this.listeners = new Map();
    this.classList = {
      add() {}, remove() {}, toggle() {}, contains() { return false; },
    };
    this.elements = {
      username: { value: '' }, password: { value: '' },
      old_password: { value: '' }, new_password: { value: '' },
    };
    this.attributes = new Map();
    this._selectedOptions = [];
  }
  addEventListener(name, callback) {
    if (!this.listeners.has(name)) this.listeners.set(name, []);
    this.listeners.get(name).push(callback);
  }
  removeEventListener(name, callback) {
    this.listeners.set(name, (this.listeners.get(name) || []).filter((item) => item !== callback));
  }
  dispatch(name, event = {}) {
    for (const callback of this.listeners.get(name) || []) callback(event);
  }
  append(...items) { this.children = [...(this.children || []), ...items]; }
  add(item) { this.children = [...(this.children || []), item]; }
  replaceChildren(...items) { this.children = [...items]; }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  get lastElementChild() {
    if (this.children?.length) return this.children.at(-1);
    this._lastElementChild ||= new Element();
    return this._lastElementChild;
  }
  get selectedOptions() { return this._selectedOptions; }
  set selectedOptions(value) { this._selectedOptions = value; }
  showModal() { this.open = true; }
  close() { this.open = false; }
  reset() { this.elements.username.value = ''; this.elements.password.value = ''; }
}

function jsonResponse(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

test('concurrent unauthorized responses share one login prompt and one account-ready event', async () => {
  const elements = new Map();
  const eventLog = [];
  const requests = [];
  const document = {
    querySelector(selector) {
      if (!elements.has(selector)) elements.set(selector, new Element());
      return elements.get(selector);
    },
    querySelectorAll() { return []; },
    createElement(tag) { const element = new Element(); element.tagName = tag; return element; },
    createTextNode(text) { return {textContent: String(text)}; },
  };
  const window = {
    location: { origin: 'http://127.0.0.1' },
    dispatchEvent(event) { eventLog.push(event); return true; },
    addEventListener() {},
  };
  const fetch = async (path, options = {}) => {
    requests.push({ path: String(path), options });
    if (path === '/api/auth/mode') return json({ mode: 'legacy' });
    if (path === '/api/auth/login') {
      return json({
        user: { user_id: 'reader-1', username: 'reader', role: 'reader' },
        csrf_token: 'csrf-reader', must_change_password: false,
      });
    }
    if (path === '/api/auth/csrf') return json({ csrf_token: 'csrf-reader-rotated' });
    if (path === '/api/auth/credentials') return json({ credentials: [] });
    throw new Error(`unexpected identity request ${path}`);
  };
  function json(body, status = 200) {
    return new Response(JSON.stringify(body), {
      status,
      headers: { 'content-type': 'application/json' },
    });
  }
  const context = {
    document, window, fetch, Headers, Response, CustomEvent: globalThis.CustomEvent,
    FormData: class { get() { return ''; } },
    console,
  };
  vm.runInNewContext(identitySource, context, { filename: identityPath });
  const identity = window.WenjinIdentity;
  await identity.ready;

  const first = identity.unauthorized();
  const second = identity.unauthorized();
  const form = elements.get('#identity-login-form');
  assert.equal((form.listeners.get('submit') || []).length, 1, 'only one login submit handler is installed');
  form.elements.username.value = 'reader';
  form.elements.password.value = 'synthetic-password';
  form.dispatch('submit', { preventDefault() {} });
  await Promise.all([first, second]);

  assert.equal(requests.filter((request) => request.path === '/api/auth/login').length, 1);
  assert.equal(requests.filter((request) => request.path === '/api/auth/csrf').length, 1);
  assert.equal(requests.filter((request) => request.path === '/api/auth/credentials').length, 1);
  assert.equal(eventLog.filter((event) => event.type === 'wenjin-account-ready').length, 1);
  assert.equal(identity.user?.user_id, 'reader-1');
});

test('forced password flow emits account-ready only after password change and fresh login', async () => {
  const elements = new Map();
  const eventLog = [];
  const requests = [];
  let loginCount = 0;
  let passwordChangeCount = 0;
  const document = {
    querySelector(selector) {
      if (!elements.has(selector)) elements.set(selector, new Element());
      return elements.get(selector);
    },
    querySelectorAll() { return []; },
    createElement() { return new Element(); },
    createTextNode(text) { return {textContent: String(text)}; },
  };
  const window = {
    location: { origin: 'http://127.0.0.1' },
    dispatchEvent(event) { eventLog.push(event); return true; },
    addEventListener() {},
  };
  const fetch = async (path, options = {}) => {
    requests.push({ path: String(path), options });
    if (path === '/api/auth/mode') return jsonResponse({ mode: 'accounts' });
    if (path === '/api/auth/me') return jsonResponse({ error: 'unauthorized' }, 401);
    if (path === '/api/auth/login') {
      loginCount += 1;
      return loginCount === 1
        ? jsonResponse({ user: { user_id: 'reader-1', username: 'reader', role: 'reader', must_change_password: true }, csrf_token: 'csrf-temporary', must_change_password: true })
        : jsonResponse({ user: { user_id: 'reader-1', username: 'reader', role: 'reader', must_change_password: false }, csrf_token: 'csrf-current', must_change_password: false });
    }
    if (path === '/api/auth/password') {
      passwordChangeCount += 1;
      if (passwordChangeCount === 1) return jsonResponse({ error: 'unauthorized', detail: '当前密码错误' }, 401);
      return jsonResponse({ user: { user_id: 'reader-1', username: 'reader', role: 'reader' }, must_change_password: false });
    }
    if (path === '/api/auth/csrf') return jsonResponse({ csrf_token: 'csrf-rotated' });
    if (path === '/api/auth/credentials') return jsonResponse({ credentials: [] });
    throw new Error(`unexpected identity request ${path}`);
  };
  const context = {
    document, window, fetch, Headers, Response, CustomEvent: globalThis.CustomEvent,
    FormData: class { get() { return ''; } },
    console,
  };
  vm.runInNewContext(identitySource, context, { filename: identityPath });
  const identity = window.WenjinIdentity;
  const loginFlowReady = identity.ready;
  async function formWithSubmit(selector) {
    for (let index = 0; index < 20; index += 1) {
      const form = elements.get(selector);
      if (form?.listeners.get('submit')?.length) return form;
      await new Promise((resolve) => setImmediate(resolve));
    }
    throw new Error(`submit handler not installed for ${selector}`);
  }
  const loginForm = await formWithSubmit('#identity-login-form');
  loginForm.elements.username.value = 'reader';
  loginForm.elements.password.value = 'temporary-password';
  const loginSubmit = loginForm.listeners.get('submit')[0]({ preventDefault() {} });
  const passwordForm = await formWithSubmit('#identity-password-form');

  const earlyReadyCount = eventLog.filter((event) => event.type === 'wenjin-account-ready').length;
  const earlyUserId = identity.user?.user_id || null;
  const passwordDialog = elements.get('#identity-password-dialog');
  const passwordError = elements.get('#identity-password-error');
  passwordForm.elements.old_password.value = 'incorrect-old-password';
  passwordForm.elements.new_password.value = 'Changed_Synthetic_Password_2026!';
  await Promise.race([
    passwordForm.listeners.get('submit')[0]({ preventDefault() {} }),
    new Promise((_, reject) => setTimeout(() => reject(new Error('wrong-password submission did not settle')), 1000)),
  ]);
  assert.equal(passwordDialog.open, true, 'invalid current password leaves the change dialog open');
  assert.match(passwordError.textContent, /[\u4e00-\u9fff]/, 'the password failure is shown in Chinese');
  const userAfterFailedChange = identity.user?.user_id || null;

  passwordForm.elements.old_password.value = 'temporary-password';
  passwordForm.elements.new_password.value = 'Changed_Synthetic_Password_2026!';
  await Promise.race([
    passwordForm.listeners.get('submit')[0]({ preventDefault() {} }),
    new Promise((_, reject) => setTimeout(() => reject(new Error(`correct-password submission did not settle: ${requests.map((item) => item.path).join(',')}; identityRole=${identity.user?.role}; currentUser=${elements.get('#identity-current-user')?.textContent}; loginFormHandlers=${loginForm.listeners.get('submit')?.length}; loginDialog=${elements.get('#identity-login-dialog')?.open}; passwordDialog=${elements.get('#identity-password-dialog')?.open}; ready=${eventLog.length}`)), 1000)),
  ]);
  await Promise.race([
    Promise.all([loginSubmit, loginFlowReady]),
    new Promise((_, reject) => setTimeout(() => reject(new Error('forced login initialization did not settle')), 1000)),
  ]);

  assert.equal(passwordChangeCount, 2);
  assert.equal(loginCount, 2, 'the new password is used for one clean post-revocation login');
  assert.equal(earlyReadyCount, 0,
    'a user who must change a temporary password is not yet ready for page API refresh');
  assert.equal(earlyUserId, null, 'the forced-password account is not published before the password changes');
  assert.equal(userAfterFailedChange, null, 'failed forced change does not publish a usable account');
  assert.equal(eventLog.filter((event) => event.type === 'wenjin-account-ready').length, 1);
  assert.equal(identity.user?.user_id, 'reader-1');
  assert.ok(requests.some((request) => request.path === '/api/auth/password'));
});

test('a grant batch stops after account changes while its first request is pending', async () => {
  const elements = new Map();
  const windowListeners = new Map();
  const requests = [];
  let releaseFirstGrant;
  let grantPosts = 0;
  const events = [];
  const admin = { user_id: 'admin-synthetic', username: '管理员甲', role: 'admin', active: true, must_change_password: false };
  const reader = { user_id: 'reader-synthetic', username: '只读乙', role: 'reader', active: true, must_change_password: false };
  const document = {
    querySelector(selector) {
      if (!elements.has(selector)) elements.set(selector, new Element());
      return elements.get(selector);
    },
    querySelectorAll() { return []; },
    createElement() { return new Element(); },
    createTextNode(text) { return { textContent: String(text) }; },
  };
  const window = {
    location: { origin: 'http://127.0.0.1' },
    dispatchEvent(event) {
      events.push(event);
      for (const callback of windowListeners.get(event.type) || []) callback(event);
      return true;
    },
    addEventListener(name, callback) {
      if (!windowListeners.has(name)) windowListeners.set(name, []);
      windowListeners.get(name).push(callback);
    },
  };
  const fetch = async (path, options = {}) => {
    requests.push({ path: String(path), options });
    if (path === '/api/auth/mode') return jsonResponse({ mode: 'accounts' });
    if (path === '/api/auth/me') return jsonResponse({ user: admin });
    if (path === '/api/auth/csrf') return jsonResponse({ csrf_token: 'csrf-current' });
    if (path === '/api/auth/credentials') return jsonResponse({ credentials: [] });
    if (path === '/api/admin/users') return jsonResponse({ users: [admin, reader, { ...reader, user_id: 'reader-two', username: '只读丙' }] });
    if (path === '/api/admin/resources') return jsonResponse({
      resources: [{ resource_kind: 'library', resource_id: 'synthetic-docs', display_name: '合成资料库', active: true }],
      grants: [],
    });
    if (path === '/api/admin/identity-audit?limit=100') return jsonResponse({ events: [] });
    if (path === '/api/admin/grants' && options.method === 'POST') {
      grantPosts += 1;
      if (grantPosts === 1) return await new Promise((resolve) => { releaseFirstGrant = () => resolve(jsonResponse({ ok: true })); });
      return jsonResponse({ ok: true });
    }
    if (path === '/api/auth/login') return jsonResponse({ user: reader, csrf_token: 'csrf-reader', must_change_password: false });
    throw new Error(`unexpected identity request ${path}`);
  };
  const context = {
    document, window, fetch, Headers, Response, CustomEvent: globalThis.CustomEvent,
    FormData: class { get() { return ''; } },
    Option: class Option { constructor(text, value) { this.text = text; this.value = value; } },
    console,
  };
  vm.runInNewContext(identitySource, context, { filename: identityPath });
  const identity = window.WenjinIdentity;
  await identity.ready;
  const find = (rootNode, predicate) => {
    if (rootNode && predicate(rootNode)) return rootNode;
    for (const child of rootNode?.children || []) {
      const found = find(child, predicate);
      if (found) return found;
    }
    return null;
  };
  const grantHost = elements.get('#identity-grants');
  const grantForm = find(grantHost, (node) => node.className === 'identity-form grant-batch-form');
  assert.ok(grantForm, 'the real rendered per-library multi-user grant form exists');
  const userSelect = find(grantForm, (node) => node.tagName === 'select' || node.multiple === true);
  assert.ok(userSelect);
  assert.deepEqual(userSelect.children.map((option) => option.value), [
    admin.user_id, reader.user_id, 'reader-two',
  ], 'one library selector contains each active user exactly once');
  assert.ok(userSelect.children.some((option) => option.text.includes('管理员')),
    'the selector identifies administrator role in Chinese');
  assert.ok(elements.get('#identity-current-user').children.some((item) => item.textContent === '管理员'),
    'the signed-in admin has a Chinese role badge');
  userSelect.selectedOptions = [{ value: 'reader-synthetic' }, { value: 'reader-two' }];
  const submit = grantForm.listeners.get('submit')[0]({ preventDefault() {} });
  while (!releaseFirstGrant) await new Promise((resolve) => setImmediate(resolve));

  const reauth = identity.unauthorized();
  const loginForm = elements.get('#identity-login-form');
  loginForm.elements.username.value = reader.username;
  loginForm.elements.password.value = 'synthetic-reader-password';
  await loginForm.listeners.get('submit').at(-1)({ preventDefault() {} });
  await reauth;
  releaseFirstGrant();
  await submit;

  assert.equal(identity.user?.user_id, reader.user_id);
  assert.equal(grantPosts, 1, 'the second user grant is never sent under the new account');
  assert.equal(events.filter((event) => event.type === 'wenjin-grants-changed').length, 0,
    'the stale administrator batch does not publish a grant-change event into the reader page');
  assert.equal(elements.get('#identity-grants-status')?.textContent || '', '',
    'the stale batch does not write a completion/error message into the new account UI');
  assert.ok(requests.find((request) => request.path === '/api/admin/grants')?.options.headers instanceof Object);
});

test('authorization UI renders one expandable multi-user row per library', async () => {
  const elements = new Map();
  const admin = { user_id: 'admin-ui', username: '资料管理员', role: 'admin', active: true, must_change_password: false };
  const users = [
    admin,
    { user_id: 'reader-ui-1', username: '检索员甲', role: 'reader', active: true },
    { user_id: 'reader-ui-2', username: '检索员乙', role: 'reader', active: true },
  ];
  const libraries = [
    { resource_kind: 'library', resource_id: 'managed-aaa', display_name: '地理资料', active: true },
    { resource_kind: 'library', resource_id: 'managed-bbb', display_name: '部署手册', active: true },
  ];
  const document = {
    querySelector(selector) {
      if (!elements.has(selector)) elements.set(selector, new Element());
      return elements.get(selector);
    },
    querySelectorAll() { return []; },
    createElement(tag) { const element = new Element(); element.tagName = tag; return element; },
    createTextNode(text) { return { textContent: String(text) }; },
  };
  const window = { location: { origin: 'http://127.0.0.1' }, dispatchEvent() { return true; }, addEventListener() {} };
  const fetch = async (path) => {
    if (path === '/api/auth/mode') return jsonResponse({ mode: 'accounts' });
    if (path === '/api/auth/me') return jsonResponse({ user: admin });
    if (path === '/api/auth/csrf') return jsonResponse({ csrf_token: 'csrf-ui' });
    if (path === '/api/auth/credentials') return jsonResponse({ credentials: [] });
    if (path === '/api/admin/users') return jsonResponse({ users });
    if (path === '/api/admin/resources') return jsonResponse({ resources: libraries, grants: [] });
    if (path === '/api/admin/identity-audit?limit=100') return jsonResponse({ events: [] });
    throw new Error(`unexpected identity request ${path}`);
  };
  const context = {
    document, window, fetch, Headers, Response, CustomEvent: globalThis.CustomEvent,
    FormData: class { get() { return ''; } },
    Option: class Option { constructor(text, value) { this.text = text; this.value = value; } },
    console,
  };
  vm.runInNewContext(identitySource, context, { filename: identityPath });
  await window.WenjinIdentity.ready;

  const table = elements.get('#identity-grants').children[0];
  const rows = table.children.at(-1).children;
  assert.equal(rows.length, 2, 'each active library has exactly one authorization row');
  assert.deepEqual(rows.map((row) => row.children[0].children[0].textContent), ['地理资料', '部署手册']);
  for (const row of rows) {
    const details = row.children[1].children[0];
    assert.equal(details.className, 'grant-user-picker');
    assert.equal(details.children[0].textContent, '管理用户授权 · 0 人已允许');
    const form = details.children.find((child) => child.className === 'identity-form grant-batch-form');
    const select = form.children[0].children[0];
    assert.equal(select.multiple, true);
    assert.equal(select.children.length, users.length, 'all active users can be selected in one library-specific control');
    assert.equal(details.open, undefined, 'the multi-user editor is collapsed until the user expands it');
  }
});
