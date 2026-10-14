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
    const classes = new Set();
    this.classList = {
      add(...names) { names.forEach((name) => classes.add(name)); },
      remove(...names) { names.forEach((name) => classes.delete(name)); },
      toggle(name, force) { const enabled = force === undefined ? !classes.has(name) : Boolean(force); if (enabled) classes.add(name); else classes.delete(name); return enabled; },
      contains(name) { return classes.has(name); },
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
  querySelector(selector) {
    const find = (node) => {
      for (const child of node.children || []) {
        if (selector === 'button' && child.tagName === 'button') return child;
        const nested = find(child); if (nested) return nested;
      }
      return null;
    };
    if (find(this)) return find(this);
    this._queryResults ||= new Map();
    if (!this._queryResults.has(selector)) {
      const child = new Element(); child.tagName = selector === 'button' ? 'button' : selector;
      this._queryResults.set(selector, child);
    }
    return this._queryResults.get(selector);
  }
  focus() { this.focused = true; }
  select() { this.selected = true; }
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

test('sign out clears identity and page evidence before a slow logout response settles', async () => {
  const elements = new Map();
  const requests = [];
  let releaseLogout;
  const logoutResponse = new Promise((resolve) => { releaseLogout = resolve; });
  const users = [
    { user_id: 'reader-before', username: '旧账号', role: 'reader', active: true, must_change_password: false },
    { user_id: 'reader-after', username: '新账号', role: 'reader', active: true, must_change_password: false },
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
  const window = {
    location: { origin: 'http://127.0.0.1' }, dispatchEvent() { return true; }, addEventListener() {},
  };
  const fetch = async (path, options = {}) => {
    requests.push({ path: String(path), options });
    if (path === '/api/auth/mode') return jsonResponse({ mode: 'accounts' });
    if (path === '/api/auth/me') return jsonResponse({ user: users[0] });
    if (path === '/api/auth/csrf') return jsonResponse({ csrf_token: 'csrf-before' });
    if (path === '/api/auth/credentials') return jsonResponse({ credentials: [] });
    if (path === '/api/auth/logout') return logoutResponse;
    if (path === '/api/auth/login') return jsonResponse({ user: users[1], csrf_token: 'csrf-after', must_change_password: false });
    throw new Error(`unexpected identity request ${path}`);
  };
  const context = {
    document, window, fetch, Headers, Response, CustomEvent: globalThis.CustomEvent,
    FormData: class { get() { return ''; } }, console,
  };
  vm.runInNewContext(identitySource, context, { filename: identityPath });
  const identity = window.WenjinIdentity;
  await identity.ready;
  let pageClears = 0;
  identity.setClearPageState(() => { pageClears += 1; });

  const signOut = identity.signOut();
  const loginDialog = document.querySelector('#identity-login-dialog');
  const logoutCall = requests.find((request) => request.path === '/api/auth/logout');
  assert.equal(identity.user, null, 'the old account is cleared synchronously');
  assert.equal(identity.csrf, null, 'the old CSRF token is cleared synchronously');
  assert.equal(pageClears, 1, 'connected page data is cleared before awaiting logout');
  assert.equal(logoutCall.options.headers.get('X-CSRF-Token'), 'csrf-before', 'logout still carries the current CSRF token');
  assert.equal(Boolean(loginDialog.open), false, 'new login does not start before logout settles');

  releaseLogout(jsonResponse({ ok: true }));
  for (let index = 0; index < 10 && !loginDialog.open; index += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
  assert.equal(loginDialog.open, true, 'the login prompt opens after the revocation request settles');
  const form = elements.get('#identity-login-form');
  form.elements.username.value = '新账号';
  form.elements.password.value = 'synthetic-password';
  form.dispatch('submit', { preventDefault() {} });
  await signOut;

  assert.equal(identity.user?.user_id, 'reader-after');
  assert.equal(pageClears, 1, 'a stale logout response does not clear the newly authenticated account');
  assert.equal(requests.filter((request) => request.path === '/api/auth/logout').length, 1);
  assert.equal(requests.filter((request) => request.path === '/api/auth/login').length, 1);
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
    createElement(tag) { const element = new Element(); element.tagName = tag; return element; },
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
  const libraryEditor = find(grantHost, (node) => node.className === 'grant-library-card');
  assert.ok(libraryEditor, 'the library has a single expandable grant editor');
  const checkboxes = [];
  const collect = (node) => { if (node?.tagName === 'input' && node.type === 'checkbox') checkboxes.push(node); for (const child of node?.children || []) collect(child); };
  collect(libraryEditor);
  assert.equal(checkboxes.length, 2, 'only ordinary users appear in the checklist; the administrator is omitted');
  assert.ok(elements.get('#identity-current-user').children.some((item) => item.textContent === '管理员'),
    'the signed-in admin has a Chinese role badge');
  for (const checkbox of checkboxes) { checkbox.checked = true; checkbox.dispatch('change'); }
  const save = find(libraryEditor, (node) => node.textContent === '保存修改');
  const submit = save.listeners.get('click')[0]();
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
    'the stale administrator batch does not publish old-account grant details into the reader page');
  assert.equal(events.filter((event) => event.type === 'wenjin-access-catalog-invalidated').length, 1,
    'a committed late write asks the new account to refresh its catalog without exposing grant details');
  const readerReady = events.findIndex((event) => event.type === 'wenjin-account-ready' && event.detail?.user_id === reader.user_id);
  const invalidated = events.findIndex((event) => event.type === 'wenjin-access-catalog-invalidated');
  assert.ok(readerReady >= 0 && invalidated > readerReady,
    'when the new account is ready before the old write commits, the late completion triggers a second catalog refresh');
  assert.equal(elements.get('#identity-grants-status')?.textContent || '', '',
    'the stale batch does not write a completion/error message into the new account UI');
  assert.ok(requests.find((request) => request.path === '/api/admin/grants')?.options.headers instanceof Object);
});

test('authorization UI renders one ordinary-user checklist per library and saves only changed grants', async () => {
  const elements = new Map();
  const events = [];
  const grantPosts = [];
  let resourceReads = 0;
  const admin = { user_id: 'admin-ui', username: '资料管理员', role: 'admin', active: true, must_change_password: false };
  const users = [
    admin,
    { user_id: 'reader-ui-1', username: '检索员甲', role: 'reader', active: true },
    { user_id: 'reader-ui-2', username: '检索员乙', role: 'reader', active: true },
    { user_id: 'reader-ui-3', username: '停用检索员', role: 'reader', active: false },
  ];
  const libraries = [
    { resource_kind: 'library', resource_id: 'managed-aaa', display_name: '地理资料', active: true },
    { resource_kind: 'library', resource_id: 'managed-bbb', display_name: '部署手册', active: true },
  ];
  const grants = [
    { resource_kind: 'library', resource_id: 'managed-aaa', principal_kind: 'user', principal_id: 'reader-ui-1', allowed: true, revision: 4 },
    { resource_kind: 'library', resource_id: 'managed-aaa', principal_kind: 'user', principal_id: 'reader-ui-3', allowed: true, revision: 6 },
    { resource_kind: 'library', resource_id: 'managed-aaa', principal_kind: 'user', principal_id: 'admin-ui', allowed: true, revision: 8 },
    { resource_kind: 'library', resource_id: 'managed-aaa', principal_kind: 'user', principal_id: 'deleted-user', allowed: true, revision: 9 },
    { resource_kind: 'library', resource_id: 'managed-bbb', principal_kind: 'user', principal_id: 'reader-ui-3', allowed: false, revision: 2 },
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
  const window = { location: { origin: 'http://127.0.0.1' }, dispatchEvent(event) { events.push(event); return true; }, addEventListener() {} };
  const fetch = async (path, options = {}) => {
    if (path === '/api/auth/mode') return jsonResponse({ mode: 'accounts' });
    if (path === '/api/auth/me') return jsonResponse({ user: admin });
    if (path === '/api/auth/csrf') return jsonResponse({ csrf_token: 'csrf-ui' });
    if (path === '/api/auth/credentials') return jsonResponse({ credentials: [] });
    if (path === '/api/admin/users') return jsonResponse({ users });
    if (path === '/api/admin/resources') { resourceReads += 1; return jsonResponse({ resources: libraries, grants }); }
    if (path === '/api/admin/grants' && options.method === 'POST') {
      const payload = JSON.parse(options.body); grantPosts.push(payload);
      if (payload.user_id === 'reader-ui-2') return jsonResponse({ detail: 'revision conflict' }, 409);
      const existing = grants.find((grant) => grant.resource_kind === payload.resource_kind && grant.resource_id === payload.resource_id && grant.principal_id === payload.user_id);
      if (existing && existing.revision !== payload.expected_revision) return jsonResponse({ detail: 'revision conflict' }, 409);
      if (existing) { existing.allowed = payload.allowed; existing.revision += 1; }
      else grants.push({ resource_kind: payload.resource_kind, resource_id: payload.resource_id, principal_kind: 'user', principal_id: payload.user_id, allowed: payload.allowed, revision: 1 });
      return jsonResponse({ ok: true });
    }
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

  const find = (rootNode, predicate) => {
    if (rootNode && predicate(rootNode)) return rootNode;
    for (const child of rootNode?.children || []) { const found = find(child, predicate); if (found) return found; }
    return null;
  };
  const findAll = (rootNode, predicate, found = []) => {
    if (rootNode && predicate(rootNode)) found.push(rootNode);
    for (const child of rootNode?.children || []) findAll(child, predicate, found);
    return found;
  };
  const grantHost = elements.get('#identity-grants');
  assert.match(grantHost.children[0].textContent, /管理员可读取所有有效资料库/);
  assert.match(grantHost.children[0].textContent, /公开\/私有分类不改变读取权限/);
  const cards = grantHost.children.filter((child) => child.className === 'grant-library-card');
  assert.equal(cards.length, 2, 'each active library has one expandable editor');
  assert.deepEqual(cards.map((card) => card.children[0].children[0].textContent), ['地理资料', '部署手册']);
  assert.equal(cards[0].children[0].children[1].textContent, '2 人已授权', 'the count includes ordinary-user grant records, not administrator grants');
  assert.equal(cards[0].open, undefined, 'the library checklist is collapsed until opened');

  const boxesA = findAll(cards[0], (node) => node.tagName === 'input' && node.type === 'checkbox');
  assert.deepEqual(boxesA.map((box) => box.value), ['reader-ui-1', 'reader-ui-2', 'reader-ui-3']);
  assert.deepEqual(boxesA.map((box) => box.checked), [true, false, true]);
  assert.equal(boxesA[2].disabled, false, 'an inactive reader with an existing grant can be unchecked to revoke it');
  assert.equal(findAll(cards[1], (node) => node.tagName === 'input' && node.type === 'checkbox').at(-1).disabled, true,
    'an inactive reader without an allowed grant cannot be selected');
  assert.ok(find(cards[0], (node) => node.className === 'grant-unknown-details'), 'unmatched grant IDs remain visible and outside the editable checklist');
  assert.equal(boxesA.some((box) => box.value === admin.user_id), false, 'administrator grants are not editable here');
  const initialSave = find(cards[0], (node) => node.textContent === '保存修改');
  assert.equal(initialSave.disabled, true, 'save starts disabled when the server snapshot has no draft changes');

  const boxesB = findAll(cards[1], (node) => node.tagName === 'input' && node.type === 'checkbox');
  boxesB[0].checked = true; boxesB[0].dispatch('change');
  assert.equal(find(cards[1], (node) => node.textContent === '保存修改').disabled, false, 'changing a checkbox enables save');
  find(cards[1], (node) => node.textContent === '取消').listeners.get('click')[0]();
  assert.equal(boxesB[0].checked, false, 'cancel restores the original checkbox snapshot');
  assert.equal(find(cards[1], (node) => node.textContent === '保存修改').disabled, true);

  boxesA[0].checked = false; boxesA[0].dispatch('change');
  boxesA[1].checked = true; boxesA[1].dispatch('change');
  const save = find(cards[0], (node) => node.textContent === '保存修改');
  await save.listeners.get('click')[0]();
  assert.deepEqual(grantPosts, [
    { resource_kind: 'library', resource_id: 'managed-aaa', user_id: 'reader-ui-1', allowed: false, expected_revision: 4 },
    { resource_kind: 'library', resource_id: 'managed-aaa', user_id: 'reader-ui-2', allowed: true, expected_revision: 0 },
  ], 'only changed ordinary-user grants are posted with their CAS revisions');
  assert.equal(grants.find((grant) => grant.principal_id === 'admin-ui').revision, 8, 'admin grant remains untouched');
  assert.equal(grants.find((grant) => grant.principal_id === 'deleted-user').revision, 9, 'unknown grant remains untouched');
  assert.equal(events.filter((event) => event.type === 'wenjin-grants-changed').length, 1, 'only the successful change immediately notifies the page');
  assert.equal(resourceReads, 2, 'a partial failure reloads the actual service grant snapshot');
  const refreshedCards = elements.get('#identity-grants').children.filter((child) => child.className === 'grant-library-card');
  const refreshedBoxes = findAll(refreshedCards[0], (node) => node.tagName === 'input' && node.type === 'checkbox');
  assert.deepEqual(refreshedBoxes.map((box) => box.checked), [false, false, true], 'the reloaded checkboxes show committed and rejected writes from the server');
  assert.match(elements.get('#identity-grants-status').textContent, /部分授权未保存/);
  assert.match(elements.get('#identity-grants-status').textContent, /授权已被其他管理员更新/);
});

test('a failed post-save grant refresh leaves the stale checklist locked', async () => {
  const elements = new Map();
  const admin = { user_id: 'admin-lock', username: '管理员', role: 'admin', active: true, must_change_password: false };
  const reader = { user_id: 'reader-lock', username: '普通用户', role: 'reader', active: true };
  let resourceReads = 0;
  const document = {
    querySelector(selector) { if (!elements.has(selector)) elements.set(selector, new Element()); return elements.get(selector); },
    querySelectorAll() { return []; },
    createElement(tag) { const element = new Element(); element.tagName = tag; return element; },
    createTextNode(text) { return { textContent: String(text) }; },
  };
  const window = { location: { origin: 'http://127.0.0.1' }, dispatchEvent() { return true; }, addEventListener() {} };
  const fetch = async (path, options = {}) => {
    if (path === '/api/auth/mode') return jsonResponse({ mode: 'accounts' });
    if (path === '/api/auth/me') return jsonResponse({ user: admin });
    if (path === '/api/auth/csrf') return jsonResponse({ csrf_token: 'csrf-lock' });
    if (path === '/api/auth/credentials') return jsonResponse({ credentials: [] });
    if (path === '/api/admin/users') return jsonResponse({ users: [admin, reader] });
    if (path === '/api/admin/resources') {
      resourceReads += 1;
      if (resourceReads > 1) return jsonResponse({ detail: 'catalog temporarily unavailable' }, 503);
      return jsonResponse({ resources: [{ resource_kind: 'library', resource_id: 'lock-docs', display_name: '锁定测试', active: true }], grants: [] });
    }
    if (path === '/api/admin/identity-audit?limit=100') return jsonResponse({ events: [] });
    if (path === '/api/admin/grants' && options.method === 'POST') return jsonResponse({ ok: true });
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
  const find = (rootNode, predicate) => {
    if (rootNode && predicate(rootNode)) return rootNode;
    for (const child of rootNode?.children || []) { const result = find(child, predicate); if (result) return result; }
    return null;
  };
  const card = find(elements.get('#identity-grants'), (node) => node.className === 'grant-library-card');
  const checkbox = find(card, (node) => node.tagName === 'input' && node.type === 'checkbox');
  checkbox.checked = true; checkbox.dispatch('change');
  const save = find(card, (node) => node.textContent === '保存修改');
  const cancel = find(card, (node) => node.textContent === '取消');
  await save.listeners.get('click')[0]();

  assert.match(elements.get('#identity-grants-status').textContent, /当前清单已锁定/);
  assert.equal(checkbox.disabled, true, 'the stale checkbox stays disabled after finally');
  assert.equal(save.disabled, true, 'the save action stays disabled until a successful reload');
  assert.equal(cancel.disabled, true, 'cancel cannot mutate a stale snapshot');
});

test('admin MCP credentials use a dedicated owner-aware list and retain one-time delivery on refresh failure', async () => {
  const elements = new Map(); const requests = []; const confirms = [];
  const admin = { user_id: 'admin-credential', username: '站点管理员', role: 'admin', active: true, must_change_password: false };
  const users = [
    admin,
    { user_id: 'reader-live', username: '检索员', role: 'reader', active: true, must_change_password: false },
    { user_id: 'reader-disabled', username: '停用员', role: 'reader', active: false, must_change_password: false },
    { user_id: 'reader-first-login', username: '待改密员', role: 'reader', active: true, must_change_password: true },
  ];
  const credentials = [
    { credential_id: 'cred-live', user_id: 'reader-live', label: 'IDE agent', token_prefix: 'mcp_ab12', created_at: '2026-10-13T00:00:00+00:00', expires_at: '2026-10-20T00:00:00+00:00', revoked_at: null },
    { credential_id: 'cred-expired', user_id: 'reader-live', label: 'old build', token_prefix: 'mcp_cd34', created_at: '2026-09-01T00:00:00+00:00', expires_at: '2026-09-02T00:00:00+00:00', revoked_at: null },
    { credential_id: 'cred-revoked', user_id: 'reader-live', label: 'retired', token_prefix: 'mcp_ef56', created_at: '2026-09-03T00:00:00+00:00', expires_at: null, revoked_at: '2026-09-04T00:00:00+00:00' },
  ];
  let selectedCredentialReads = 0;
  const document = {
    querySelector(selector) { if (!elements.has(selector)) elements.set(selector, new Element()); return elements.get(selector); },
    querySelectorAll() { return []; },
    createElement(tag) { const element = new Element(); element.tagName = tag; return element; },
    createTextNode(text) { return { textContent: String(text) }; },
  };
  const window = { location: { origin: 'http://127.0.0.1' }, confirm(message) { confirms.push(message); return true; }, dispatchEvent() { return true; }, addEventListener() {} };
  const fetch = async (path, options = {}) => {
    const method = options.method || 'GET'; requests.push({ path: String(path), method, options });
    if (path === '/api/auth/mode') return jsonResponse({ mode: 'accounts', session_policy: { idle_seconds: 1800, absolute_seconds: 43200 } });
    if (path === '/api/auth/me') return jsonResponse({ user: admin });
    if (path === '/api/auth/csrf') return jsonResponse({ csrf_token: 'csrf-credential' });
    if (path === '/api/auth/credentials') return jsonResponse({ credentials: [] });
    if (path === '/api/admin/users') return jsonResponse({ users });
    if (path === '/api/admin/resources') return jsonResponse({ resources: [], grants: [] });
    if (path === '/api/admin/identity-audit?limit=100') return jsonResponse({ events: [] });
    if (path === '/api/admin/users/reader-live/credentials' && method === 'GET') {
      selectedCredentialReads += 1;
      if (selectedCredentialReads === 2) return jsonResponse({ detail: 'temporary list failure' }, 503);
      return jsonResponse({ credentials });
    }
    if (String(path).startsWith('/api/admin/users/') && String(path).endsWith('/credentials') && method === 'GET') return jsonResponse({ credentials: [] });
    if (path === '/api/admin/users/reader-live/credentials' && method === 'POST') {
      const payload = JSON.parse(options.body);
      credentials.unshift({ credential_id: 'cred-new', user_id: 'reader-live', label: payload.label, token_prefix: 'mcp_new1', created_at: '2026-10-14T00:00:00+00:00', expires_at: '2026-11-13T00:00:00+00:00', revoked_at: null });
      return jsonResponse({ credential: { credential_id: 'cred-new', token: 'mcp-secret-once', token_prefix: 'mcp_new1', created_at: '2026-10-14T00:00:00+00:00', expires_at: '2026-11-13T00:00:00+00:00' } }, 201);
    }
    if (path === '/api/admin/credentials/cred-new' && method === 'DELETE') { credentials[0].revoked_at = '2026-10-14T01:00:00+00:00'; return jsonResponse({ ok: true }); }
    throw new Error(`unexpected credential request ${method} ${path}`);
  };
  const context = {
    document, window, fetch, Headers, Response, CustomEvent: globalThis.CustomEvent,
    FormData: class { constructor(form) { this.form = form; } get(name) { return this.form.values?.[name] ?? ''; } },
    Option: class Option { constructor(text, value) { this.text = text; this.value = value; } }, console,
  };
  vm.runInNewContext(identitySource, context, { filename: identityPath });
  await window.WenjinIdentity.ready;

  const targetSelect = elements.get('#admin-credential-user');
  const issueForm = elements.get('#admin-credential-form');
  const issueButton = issueForm.querySelector('button');
  targetSelect.value = 'reader-disabled'; targetSelect.dispatch('change');
  assert.match(elements.get('#admin-credential-target-note').textContent, /已停用/);
  assert.equal(issueButton.disabled, true, 'disabled account cannot be issued a credential from the UI');
  targetSelect.value = 'reader-first-login'; targetSelect.dispatch('change');
  assert.match(elements.get('#admin-credential-target-note').textContent, /首次密码修改/);
  assert.equal(issueButton.disabled, true, 'must-change account cannot be issued a credential from the UI');

  targetSelect.value = 'reader-live'; targetSelect.dispatch('change');
  for (let index = 0; index < 10 && selectedCredentialReads === 0; index += 1) await new Promise((resolve) => setImmediate(resolve));
  for (let index = 0; index < 10 && !elements.get('#admin-user-credentials').children.length; index += 1) await new Promise((resolve) => setImmediate(resolve));
  assert.equal(selectedCredentialReads, 1, `admin selected-user list request completed; ${elements.get('#admin-user-credentials').textContent}`);
  const list = elements.get('#admin-user-credentials');
  assert.ok(list.children[0]?.children?.[1], `expected table body in list; children=${list.children?.length}, first=${list.children?.[0]?.tagName}, firstchildren=${list.children?.[0]?.children?.map((node) => node.tagName)}`);
  const bodyRows = list.children[0].children[1].children;
  const rowText = bodyRows.map((row) => row.children.map((cell) => cell.textContent).join(' ')).join('\n');
  assert.match(rowText, /有效/); assert.match(rowText, /已过期/); assert.match(rowText, /已撤销/);
  assert.match(rowText, /2026-10-13 08:00:00/, 'timestamps use Beijing time in yyyy-MM-dd HH:mm:ss format');
  assert.doesNotMatch(rowText, /mcp-secret-once/, 'list responses never expose a complete credential');

  issueForm.values = { label: 'IDE 研发客户端', expires: '2592000' };
  const submit = (currentTarget) => issueForm.onsubmit({ preventDefault() {}, currentTarget });
  await submit(issueForm);
  assert.equal(requests.filter((request) => request.path === '/api/admin/users/reader-live/credentials' && request.method === 'POST').length, 1);
  const delivery = elements.get('#admin-credential-delivery');
  assert.equal(elements.get('#admin-credential-token').value, 'mcp-secret-once');
  assert.match(elements.get('#admin-credential-delivery-meta').textContent, /检索员/);
  assert.match(elements.get('#admin-credential-delivery-meta').textContent, /IDE 研发客户端/);
  assert.match(elements.get('#admin-credential-delivery-meta').textContent, /Bearer/);
  assert.match(elements.get('#admin-credential-status').textContent, /列表刷新失败/);
  assert.equal(elements.get('#admin-credential-token').value, 'mcp-secret-once', 'a failed list refresh does not discard the only copy of the issued token');
  elements.get('#admin-credential-copy').onclick();
  assert.equal(elements.get('#admin-credential-token').selected, true, 'when Clipboard API is absent, the full token is selected for manual copy');
  assert.match(elements.get('#admin-credential-copy-status').textContent, /Ctrl\+C/);
  elements.get('#admin-credential-hide').onclick();
  assert.equal(elements.get('#admin-credential-token').value, '');
  assert.equal(delivery.classList.contains('hidden'), true, 'hide clears the only in-memory token field');

  await elements.get('#admin-credential-refresh').onclick();
  const refreshedRows = elements.get('#admin-user-credentials').children[0].children[1].children;
  assert.equal(refreshedRows[0].children[0].textContent, 'IDE 研发客户端');
  await refreshedRows[0].children.at(-1).children[0].onclick();
  assert.match(confirms.at(-1), /IDE 研发客户端.*mcp_new1/s, 'revocation confirmation names the exact target credential');
  assert.ok(requests.some((request) => request.path === '/api/admin/credentials/cred-new' && request.method === 'DELETE'));
});
