import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import vm from 'node:vm';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const appPath = resolve(root, 'src/wenjin/file_management/admin/app.js');
const appSource = readFileSync(appPath, 'utf8');

class Element {
  constructor() {
    this.children = [];
    this.dataset = {};
    this.value = '';
    this.files = [];
    this.listeners = new Map();
    this.style = {};
    const classes = new Set();
    this.classList = {
      add(...names) { names.forEach((name) => classes.add(name)); },
      remove(...names) { names.forEach((name) => classes.delete(name)); },
      toggle(name, force) {
        const enabled = force === undefined ? !classes.has(name) : Boolean(force);
        if (enabled) classes.add(name); else classes.delete(name);
        return enabled;
      },
      contains(name) { return classes.has(name); },
    };
  }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = [...items]; }
  addEventListener(name, callback) {
    if (!this.listeners.has(name)) this.listeners.set(name, []);
    this.listeners.get(name).push(callback);
  }
  setAttribute(name, value) { (this.attributes ||= {})[name] = String(value); }
  click() {
    return Promise.all((this.listeners.get('click') || []).map((callback) => callback({
      target: this,
      preventDefault() {},
    })));
  }
  showModal() { this.open = true; }
  close() { this.open = false; }
  remove() {}
  focus() {}
  querySelector() { return new Element(); }
  querySelectorAll() { return []; }
  get options() { return this.children; }
}

function response(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

function makeApp(fetchHandler, identity = null) {
  const elements = new Map();
  const requests = [];
  const timers = new Map();
  const windowListeners = new Map();
  const objectUrls = [];
  let nextTimerId = 0;
  const document = {
    querySelector(selector) {
      if (!elements.has(selector)) elements.set(selector, new Element());
      return elements.get(selector);
    },
    querySelectorAll() { return []; },
    createElement(tagName) { const element = new Element(); element.tagName = tagName; return element; },
    createElementNS(_namespace, tagName) { const element = new Element(); element.tagName = tagName; return element; },
    createTextNode(text) { return { textContent: text }; },
    body: new Element(),
  };
  const window = {
    location: { href: 'http://127.0.0.1/admin/', origin: 'http://127.0.0.1' },
    WenjinIdentity: identity,
    confirm: () => true,
    setTimeout(callback, delay) { const id = ++nextTimerId; timers.set(id, { callback, delay }); return id; },
    clearTimeout(id) { timers.delete(id); },
    setInterval() { return 1; },
    clearInterval() {},
    matchMedia() { return { matches: false }; },
    scrollTo() {},
    addEventListener(name, callback) {
      if (!windowListeners.has(name)) windowListeners.set(name, []);
      windowListeners.get(name).push(callback);
    },
    dispatchEvent(event) {
      for (const callback of windowListeners.get(event.type) || []) callback(event);
      return true;
    },
  };
  const context = {
    document,
    Headers,
    URL,
    URLSearchParams,
    Intl,
    Date,
    Number,
    String,
    Array,
    Set,
    Map,
    Promise,
    CustomEvent: globalThis.CustomEvent,
    Option: class Option { constructor(text, value) { this.text = text; this.value = value; } },
    encodeURIComponent,
    CSS: { escape: (value) => value },
    URL: {
      createObjectURL() { const value = `blob:synthetic-${objectUrls.length + 1}`; objectUrls.push(value); return value; },
      revokeObjectURL(value) { objectUrls.push(`revoked:${value}`); },
    },
    window,
    fetch: async (path, options = {}) => {
      const request = { path: String(path), method: options.method || 'GET', options };
      requests.push(request);
      return fetchHandler(request, requests.length);
    },
    console,
    setTimeout,
    clearTimeout,
  };
  const marker = /\n\s*wireEvents\(\);\s*renderAll\(\);\s*(?:renderAccountMenu\(\);\s*)?setConnection\('idle'\);[\s\S]*?\n\}\)\(\);\s*$/;
  const hook = `
  wireEvents();
  renderAll = () => {};
  toast = () => {};
  navigate = () => {};
  openJobDetail = async () => {};
  showApiError = (error) => { globalThis.__testError = error; };
  updatePolling = () => {};
  globalThis.__admin = { state, api, clearSensitiveState, loadFiles, loadLibraries, loadJobs, refreshAll, setRefreshAll: (fn) => { refreshAll = fn; }, scheduleLibraryRefresh, chooseUpload, startUploadPicker, deleteFile, retryJob, downloadFile, renderFiles, renderJobs, renderSearchLibraries, renderSearchSources, renderSearchDocuments, runSearch, sourceKey, sourceSearchable, sourceDisplayName, sourcePublicationLabel, loadEvaluations, loadEvaluationResult, loadEvaluationCase, evaluationAction, renderEvaluationJobs, renderReleaseLibraryOptions, syncReleaseAccess, lastError: () => globalThis.__testError };
})();`;
  assert.match(appSource, marker, 'test hook must replace only app startup, leaving product functions intact');
  vm.runInNewContext(appSource.replace(marker, hook), context, { filename: appPath });
  return { app: context.__admin, requests, elements, timers, window, document, objectUrls };
}

const catalog = (file, versionId = file.active_version_id) => [{
  library_id: file.library_id,
  library: '技术资料',
  sources: [{
    source_type: 'managed', file_id: file.file_id, version: file.version,
    version_id: versionId, searchable: file.status === 'active',
    documents: [{ document_id: file.file_id, display_name: file.display_name, searchable: file.status === 'active' }],
  }],
}];

const libraryWith = (...files) => [{
  library_id: 'docs', library: '技术资料',
  sources: files.map((file) => ({
    source_type: 'managed', file_id: file.file_id, version: file.version,
    version_id: file.active_version_id, searchable: file.status === 'active',
    documents: [{ document_id: file.file_id, display_name: file.display_name, searchable: file.status === 'active' }],
  })),
}];

const managedFile = (status, activeVersionId = null) => ({
  file_id: 'file-1', library_id: 'docs', display_name: 'new-guide.md',
  version: 'v1', status, active_version_id: activeVersionId, managed: true,
});

test('download keeps the original extension when the display name has none', async () => {
  const { app, requests, document } = makeApp(async () => new Response('synthetic source', { status: 200 }));
  app.state.connected = true;
  await app.downloadFile({
    ...managedFile('active', 'version-1'),
    file_id: 'file-download',
    display_name: '华东地貌说明',
    original_filename: '地形图.v1.txt',
  });

  assert.equal(requests.length, 1);
  assert.match(requests[0].path, /\/api\/files\/file-download\/download\?name=%E5%9C%B0%E5%BD%A2%E5%9B%BE\.v1\.txt$/);
  const link = document.body.children.find((child) => child.tagName === 'a');
  assert.ok(link, 'download handler creates an anchor');
  assert.equal(link.download, '地形图.v1.txt');
});

test('fast completed upload refreshes the live retrieval catalog without page reload', async () => {
  const existing = { ...managedFile('active', 'version-old'), file_id: 'file-old', display_name: 'old-guide.md' };
  let files = [existing];
  const uploaded = managedFile('active', 'version-1');
  const { app, requests, elements } = makeApp(async ({ path, method }) => {
    if (path.startsWith('/api/files?') && method === 'POST') return response({ job_id: 'job-upload', status: 'completed' }, 201);
    if (path === '/api/files') return response({ files });
    if (path.startsWith('/api/jobs?')) return response({ jobs: [{ job_id: 'job-upload', status: 'completed' }], offset: 0 });
    if (path === '/api/admin/libraries') return response({ libraries: libraryWith(...files) });
    throw new Error(`unexpected ${method} ${path}`);
  });
  app.state.connected = true;
  app.state.config = { max_upload_bytes: 1000 };
  app.state.uploadLibraryId = 'docs';
  await app.loadFiles(); // establish the page's initial catalog signature
  await app.loadLibraries();
  const librarySelect = elements.get('#search-library') || new Element(); elements.set('#search-library', librarySelect);
  const sourceSelect = elements.get('#search-source') || new Element(); elements.set('#search-source', sourceSelect);
  const documentSelect = elements.get('#search-document') || new Element(); elements.set('#search-document', documentSelect);
  const queryInput = elements.get('#search-query') || new Element(); elements.set('#search-query', queryInput);
  app.renderSearchLibraries();
  librarySelect.value = 'docs';
  app.renderSearchSources();
  const oldSourceKey = app.sourceKey(app.state.libraries[0].sources[0]);
  sourceSelect.value = oldSourceKey;
  queryInput.value = 'find the old feature';
  files = [existing, uploaded];
  const input = elements.get('#upload-input') || elements.set('#upload-input', new Element()).get('#upload-input');
  input.files = [{ name: 'new-guide.md', size: 12 }];
  app.startUploadPicker();
  await input.onchange();
  assert.equal(app.state.libraries[0]?.sources.find((source) => source.file_id === uploaded.file_id)?.version_id, 'version-1', 'retrieval source is refreshed from the server');
  assert.ok(requests.some((request) => request.path === '/api/admin/libraries'), 'the live catalog endpoint is called');
  app.renderSearchLibraries();
  assert.equal(librarySelect.value, 'docs', 'a still-valid selected library survives the catalog refresh');
  assert.equal(sourceSelect.value, oldSourceKey, 'a still-valid selected source survives the catalog refresh');
  assert.equal(queryInput.value, 'find the old feature', 'the query is not cleared by an unrelated catalog refresh');
  const uploadedSource = app.state.libraries[0].sources.find((source) => source.file_id === uploaded.file_id);
  sourceSelect.value = app.sourceKey(uploadedSource);
  app.renderSearchDocuments();
  assert.ok(sourceSelect.options.some((option) => option.value === app.sourceKey(uploadedSource)));
  assert.ok(documentSelect.options.some((option) => option.value === uploaded.file_id), 'the uploaded document is selectable for retrieval');
});

test('upload cancel and close work before choosing a library and never open the file picker', async () => {
  const { app, requests, elements } = makeApp(async ({ path, method }) => {
    throw new Error(`unexpected request ${method} ${path}`);
  });
  app.state.connected = true;
  const dialog = elements.get('#upload-dialog');
  const input = elements.get('#upload-input') || new Element();
  elements.set('#upload-input', input);
  let pickerCalls = 0;
  input.click = () => { pickerCalls += 1; };

  dialog.showModal();
  await elements.get('#upload-cancel-button').click();
  assert.equal(dialog.open, false, 'footer cancel closes an empty dialog');
  assert.equal(pickerCalls, 0, 'cancel never opens the native file picker');
  assert.equal(requests.length, 0, 'cancel makes no upload or library API request');

  dialog.showModal();
  await elements.get('#upload-close-button').click();
  assert.equal(dialog.open, false, 'corner close closes an empty dialog');
  assert.equal(pickerCalls, 0, 'close never opens the native file picker');
  assert.equal(requests.length, 0, 'close makes no upload or library API request');

  dialog.showModal();
  await elements.get('#choose-upload-file').click();
  assert.equal(dialog.open, true, 'attempting upload without a destination leaves the dialog open');
  assert.equal(pickerCalls, 0, 'missing destination is rejected before opening the picker');
  assert.equal(requests.length, 0, 'missing destination makes no upload request');
});

test('creating a named library makes the stable ID selectable in the upload dialog', async () => {
  const library = { library_id: 'managed-a1b2c3d4e5f6', display_name: '部门资料' };
  const { app, requests, elements } = makeApp(async ({ path, method }) => {
    if (path === '/api/admin/managed-libraries' && method === 'POST') return response({ library }, 201);
    if (path === '/api/admin/managed-libraries' && method === 'GET') return response({ libraries: [library] });
    if (path === '/api/admin/libraries') return response({ libraries: [] });
    throw new Error(`unexpected ${method} ${path}`);
  });
  app.state.connected = true;
  const getElement = (selector) => {
    if (!elements.has(selector)) elements.set(selector, new Element());
    return elements.get(selector);
  };
  getElement('#new-library-name').value = '部门资料';

  await elements.get('#create-library-button').click();

  const post = requests.find((request) => request.path === '/api/admin/managed-libraries' && request.method === 'POST');
  assert.deepEqual(JSON.parse(post.options.body), { display_name: '部门资料' });
  assert.equal(app.state.uploadLibraryId, library.library_id);
  assert.equal(getElement('#upload-library').value, library.library_id);
  assert.ok(getElement('#upload-library').options.some((option) => option.value === library.library_id
    && option.textContent === '部门资料'));
});

test('an empty account can still open upload setup to create its first library', async () => {
  const { app, requests, elements } = makeApp(async ({ path }) => {
    if (path === '/api/admin/managed-libraries') return response({ libraries: [] });
    throw new Error(`unexpected request ${path}`);
  });
  app.state.connected = true;
  app.state.libraries = [];
  app.state.managedLibraries = [];

  await app.chooseUpload();

  assert.equal(elements.get('#upload-dialog').open, true);
  assert.equal(elements.get('#upload-library').options.length, 1, 'only the empty placeholder is shown');
  assert.equal(requests.length, 1, 'opening the dialog only reads the managed-library list');
});

test('replacing a file keeps its saved display name instead of stale new-upload input', async () => {
  let uploadHeaders;
  const { app, requests, elements } = makeApp(async ({ path, method, options }) => {
    if (path === '/api/files/file-replace' && method === 'PUT') {
      uploadHeaders = options.headers;
      return response({ job_id: 'replace-job', status: 'queued' }, 202);
    }
    if (path === '/api/files') return response({ files: [] });
    if (path.startsWith('/api/jobs?')) return response({ jobs: [], offset: 0 });
    throw new Error(`unexpected ${method} ${path}`);
  });
  app.state.connected = true;
  app.state.config = { max_upload_bytes: 1000 };
  const displayNameInput = elements.get('#upload-display-name') || new Element();
  elements.set('#upload-display-name', displayNameInput);
  displayNameInput.value = '上次新上传的旧标题';
  const input = elements.get('#upload-input') || new Element();
  elements.set('#upload-input', input);
  input.files = [{ name: '替换版.v2.txt', size: 24 }];

  app.startUploadPicker({
    ...managedFile('active', 'version-old'),
    file_id: 'file-replace', library_id: 'managed-existing',
    display_name: '原有地貌资料', original_filename: '原始地形.v1.txt',
  });
  await input.onchange();

  assert.equal(requests[0].path, '/api/files/file-replace');
  assert.equal(uploadHeaders.get('X-File-Name'), encodeURIComponent('替换版.v2.txt'));
  assert.equal(uploadHeaders.get('X-Display-Name'), encodeURIComponent('原有地貌资料'));
  assert.notEqual(uploadHeaders.get('X-Display-Name'), encodeURIComponent('上次新上传的旧标题'));
});

test('a late create-library response cannot select or publish the old account library after switching accounts', async () => {
  let releaseCreate;
  const { app, requests, elements, window } = makeApp(async ({ path, method }) => {
    if (path === '/api/admin/managed-libraries' && method === 'POST') {
      return await new Promise((resolve) => { releaseCreate = () => resolve(response({ library: {
        library_id: 'managed-old-account', display_name: '旧账号资料库',
      } }, 201)); });
    }
    throw new Error(`unexpected request ${method} ${path}`);
  });
  const getElement = (selector) => {
    if (!elements.has(selector)) elements.set(selector, new Element());
    return elements.get(selector);
  };
  const identity = {
    mode: 'accounts', user: { user_id: 'admin-new', username: '新管理员', role: 'admin' }, csrf: 'csrf-new',
  };
  window.WenjinIdentity = identity;
  app.setRefreshAll(async () => false);
  app.state.connected = true;
  getElement('#new-library-name').value = '即将建立的旧账号资料库';
  const createButton = getElement('#create-library-button');
  const pendingClick = createButton.click();
  while (!releaseCreate) await new Promise((resolve) => setImmediate(resolve));
  assert.equal(createButton.disabled, true, 'the in-flight create button is disabled');

  identity.user = { user_id: 'admin-switched', username: '切换后的管理员', role: 'admin' };
  identity.csrf = 'csrf-switched';
  window.dispatchEvent(new CustomEvent('wenjin-account-ready', { detail: identity.user }));
  assert.equal(createButton.disabled, false, 'account cleanup restores the create button');
  releaseCreate();
  await pendingClick;

  assert.equal(requests.filter((request) => request.path === '/api/admin/managed-libraries' && request.method === 'POST').length, 1);
  assert.equal(app.state.uploadLibraryId, '', 'the stale response does not select the old account library');
  assert.equal(app.state.managedLibraries.length, 0, 'the stale response does not populate the new account directory');
  assert.equal(getElement('#upload-library').value, '', 'the new account keeps no stale library selection');
  assert.equal(getElement('#new-library-name').value, '', 'the old account form value is cleared on switch');
  assert.equal(createButton.disabled, false);
});

test('upload dialog posts the original Chinese filename and independent display name to the chosen library', async () => {
  const captured = [];
  const { app, requests, elements } = makeApp(async ({ path, method, options }) => {
    if (path.startsWith('/api/files?') && method === 'POST') {
      captured.push({ path, headers: options.headers, body: options.body });
      return response({ job_id: 'job-created', status: 'queued' }, 202);
    }
    if (path === '/api/files') return response({ files: [] });
    if (path.startsWith('/api/jobs?')) return response({ jobs: [], offset: 0 });
    throw new Error(`unexpected ${method} ${path}`);
  });
  app.state.connected = true;
  app.state.config = { max_upload_bytes: 1000 };
  app.state.uploadLibraryId = 'managed-a1b2c3d4e5f6';
  const getElement = (selector) => {
    if (!elements.has(selector)) elements.set(selector, new Element());
    return elements.get(selector);
  };
  getElement('#upload-library').value = app.state.uploadLibraryId;
  getElement('#upload-display-name').value = '部门道路网络';
  getElement('#upload-dialog').showModal();
  const input = elements.get('#upload-input') || new Element();
  elements.set('#upload-input', input);
  let pickerCalls = 0;
  input.click = () => { pickerCalls += 1; };

  await elements.get('#choose-upload-file').click();
  assert.equal(elements.get('#upload-dialog').open, false);
  assert.equal(pickerCalls, 1);
  input.files = [{ name: '原始道路网.v2.txt', size: 31 }];
  await input.onchange();

  assert.equal(captured.length, 1);
  assert.equal(captured[0].path, '/api/files?library_id=managed-a1b2c3d4e5f6');
  assert.equal(captured[0].headers.get('X-File-Name'), encodeURIComponent('原始道路网.v2.txt'));
  assert.equal(captured[0].headers.get('X-Display-Name'), encodeURIComponent('部门道路网络'));
  assert.equal(captured[0].body.name, '原始道路网.v2.txt');
  assert.ok(requests.some((request) => request.path === '/api/files'));
});

test('deleted historical job uses a readable fallback instead of exposing its file id in the row', () => {
  const { app, elements } = makeApp(async ({ path, method }) => {
    throw new Error(`unexpected request ${method} ${path}`);
  });
  app.state.connected = true;
  app.state.jobs = [{
    job_id: 'delete-job-1', file_id: 'private-file-id-1', kind: 'delete', status: 'deleted',
    phase: 'complete', progress: 100, created_at: '2026-10-08T00:00:00Z',
    file_display_name: null, library_id: 'managed-abcd1234ef56', library_display_name: null,
    original_filename: null,
  }];
  elements.get('#job-search').value = '';
  elements.get('#job-kind-filter').value = 'all';
  elements.get('#job-status-filter').value = 'all';

  app.renderJobs();
  const fileCell = elements.get('#job-rows').children[0].children[1];
  assert.equal(fileCell.children[0].textContent, '名称未记录');
  const renderedText = fileCell.children.map((child) => child.textContent).join(' ');
  assert.doesNotMatch(renderedText, /private-file-id-1/,
    'the stable file ID remains available in job detail/API fields, not in the main target-name column');
});

test('account-ready event refreshes the live search catalog after an account switch', async () => {
  let reads = 0;
  const user = { user_id: 'reader-1', username: 'reader', role: 'reader' };
  const identity = { mode: 'accounts', user, csrf: 'csrf-reader' };
  const { app, requests, window } = makeApp(async ({ path }) => {
    if (path === '/api/admin/libraries') {
      reads += 1;
      return response({ libraries: libraryWith(managedFile('active', 'version-after-switch')) });
    }
    throw new Error(`unexpected request ${path}`);
  });
  window.WenjinIdentity = identity;
  app.state.token = 'stale-admin-token';
  window.dispatchEvent(new CustomEvent('wenjin-account-ready', { detail: user }));
  for (let index = 0; index < 8 && app.state.libraries.length === 0; index += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
  assert.equal(reads, 1, 'the account-ready event triggers an actual catalog request');
  assert.equal(app.state.token, null, 'the previous static bearer is cleared on account change');
  assert.equal(app.state.libraries[0]?.sources[0]?.version_id, 'version-after-switch', `${JSON.stringify(app.state.libraries)}; ${app.lastError()?.stack || ''}`);
  assert.ok(requests.some((request) => request.path === '/api/admin/libraries'));
});

test('a delayed old-account catalog response cannot overwrite the switched account catalog', async () => {
  let releaseOld;
  let reads = 0;
  const identity = {
    mode: 'accounts',
    user: { user_id: 'reader-a', username: 'reader-a', role: 'reader' },
    csrf: 'csrf-a',
  };
  const { app, window } = makeApp(async ({ path }) => {
    if (path !== '/api/admin/libraries') throw new Error(`unexpected request ${path}`);
    reads += 1;
    const requester = identity.user.user_id;
    if (requester === 'reader-a') {
      return await new Promise((resolve) => {
        releaseOld = () => resolve(response({ libraries: libraryWith(managedFile('active', 'catalog-A')) }));
      });
    }
    return response({ libraries: libraryWith(managedFile('active', 'catalog-B')) });
  });
  window.WenjinIdentity = identity;
  app.state.connected = true;

  const oldRequest = app.loadLibraries();
  while (!releaseOld) await new Promise((resolve) => setImmediate(resolve));
  identity.user = { user_id: 'reader-b', username: 'reader-b', role: 'reader' };
  identity.csrf = 'csrf-b';
  window.dispatchEvent(new CustomEvent('wenjin-account-ready', { detail: identity.user }));
  for (let index = 0; index < 12 && app.state.libraries[0]?.sources[0]?.version_id !== 'catalog-B'; index += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
  releaseOld();
  const staleResult = await oldRequest.catch((error) => error);

  assert.equal(staleResult.status, 0, 'old generation responses are explicitly discarded');
  assert.equal(reads, 2);
  assert.equal(app.state.libraries[0]?.sources[0]?.version_id, 'catalog-B');
});

test('a late 401 from the old account cannot start another login after state has switched', async () => {
  let releaseOld401;
  let unauthorizedCalls = 0;
  const identity = {
    mode: 'accounts',
    user: { user_id: 'reader-a', username: 'reader-a', role: 'reader' },
    csrf: 'csrf-a',
    unauthorized() { unauthorizedCalls += 1; return Promise.resolve(); },
  };
  const { app, window } = makeApp(async ({ path }) => {
    if (path !== '/api/admin/libraries') throw new Error(`unexpected request ${path}`);
    return await new Promise((resolve) => {
      releaseOld401 = () => resolve(response({ error: 'unauthorized' }, 401));
    });
  });
  window.WenjinIdentity = identity;

  const oldRequest = app.api('/api/admin/libraries').catch((error) => error);
  while (!releaseOld401) await new Promise((resolve) => setImmediate(resolve));
  app.clearSensitiveState({ disconnected: true });
  identity.user = { user_id: 'reader-b', username: 'reader-b', role: 'reader' };
  identity.csrf = 'csrf-b';
  releaseOld401();
  const result = await oldRequest;

  assert.equal(result.status, 0, 'old-generation response is ignored as a stale request');
  assert.equal(unauthorizedCalls, 0, 'stale 401 cannot prompt for credentials belonging to the new account');
});

test('self grant refreshes an authorized search option and revoke clears old evidence', async () => {
  let granted = false;
  let searchStarted;
  let releaseSearch;
  let libraryReads = 0;
  const identity = { mode: 'accounts', user: { user_id: 'reader-self', username: '合成读者', role: 'reader' }, csrf: 'csrf-reader' };
  const { app, requests, elements, window } = makeApp(async ({ path, method }) => {
    if (path === '/api/admin/libraries') {
      libraryReads += 1;
      return response({ libraries: granted ? libraryWith(managedFile('active', 'version-granted')) : [] });
    }
    if (path === '/api/admin/search' && method === 'POST') {
      searchStarted?.();
      return await new Promise((resolve) => {
        releaseSearch = () => resolve(response({
          query: 'synthetic query', context_char_budget: 1000,
          catalog_identity: { library_id: 'docs', version_id: 'version-granted', index_target_id: 'synthetic-target' },
          retrieval: {}, evidence_status: 'verified',
          evidence: [{ rank: 1, source_path: 'synthetic.txt', version: 'v1', text: 'synthetic marker evidence' }],
        }));
      });
    }
    throw new Error(`unexpected ${method} ${path}`);
  });
  window.WenjinIdentity = identity;
  app.state.connected = true;
  await app.loadLibraries();
  granted = true;
  window.dispatchEvent(new CustomEvent('wenjin-grants-changed', {
    detail: { user_ids: ['reader-self'], library_id: 'docs', allowed: true },
  }));
  for (let index = 0; index < 10 && app.state.libraries.length === 0; index += 1) await new Promise((resolve) => setImmediate(resolve));
  assert.equal(libraryReads, 2, 'a grant event for the current reader triggers a real library-list request');
  app.renderSearchLibraries();
  const librarySelect = elements.get('#search-library');
  assert.ok(librarySelect.options.some((option) => option.value === 'docs'), 'the newly allowed library becomes selectable without reload');
  librarySelect.value = 'docs';
  app.renderSearchSources();
  const sourceSelect = elements.get('#search-source') || new Element(); elements.set('#search-source', sourceSelect);
  sourceSelect.value = app.sourceKey(app.state.libraries[0].sources[0]);
  const searchQuery = elements.get('#search-query') || new Element(); elements.set('#search-query', searchQuery);
  const topK = elements.get('#search-top-k') || new Element(); elements.set('#search-top-k', topK);
  const budget = elements.get('#search-budget') || new Element(); elements.set('#search-budget', budget);
  searchQuery.value = 'synthetic query';
  topK.value = '5';
  budget.value = '1000';

  let resolveStarted;
  const started = new Promise((resolve) => { resolveStarted = resolve; });
  searchStarted = resolveStarted;
  const staleSearch = app.runSearch({ preventDefault() {} });
  await started;
  while (!releaseSearch) await new Promise((resolve) => setImmediate(resolve));
  granted = false;
  window.dispatchEvent(new CustomEvent('wenjin-grants-changed', {
    detail: { user_ids: ['reader-self'], library_id: 'docs', allowed: false },
  }));
  for (let index = 0; index < 10 && app.state.libraries.length !== 0; index += 1) await new Promise((resolve) => setImmediate(resolve));
  releaseSearch();
  await staleSearch;

  assert.equal(libraryReads, 3, 'revocation refreshes the current account catalog');
  assert.equal(librarySelect.value, '', 'the revoked library selection is cleared');
  assert.equal(app.state.lastSearch, null, 'revocation clears previously visible evidence');
  assert.equal(elements.get('#evidence-state').textContent, '授权已撤销');
  assert.equal(elements.get('#results-meta').children.length, 0);
  assert.equal(requests.filter((request) => request.path === '/api/admin/search').length, 1);
  assert.equal(app.state.libraries.length, 0);
});

test('a neutral access-catalog invalidation clears evidence and reloads the current account catalog', async () => {
  let libraryReads = 0;
  const { app, elements, window, document } = makeApp(async ({ path }) => {
    if (path === '/api/admin/libraries') {
      libraryReads += 1;
      return response({ libraries: libraryWith(managedFile('active', 'version-current')) });
    }
    throw new Error(`unexpected request ${path}`);
  });
  window.WenjinIdentity = { user: { user_id: 'reader-current', role: 'reader' }, mode: 'accounts' };
  app.state.connected = true;
  await app.loadLibraries();
  app.state.lastSearch = { evidence: [{ text: 'old account evidence' }] };
  app.state.lastSearchTarget = { library_id: 'docs', source_key: 'managed:active' };
  app.state.lastSearchLibraryId = 'docs';
  elements.get('#search-library').value = 'docs';
  elements.get('#search-source').value = 'managed:active';
  elements.get('#search-document').value = 'active';
  document.querySelector('#evidence-list').append({ textContent: 'old evidence' });
  const beforeRevision = app.state.searchRevision;

  window.dispatchEvent(new CustomEvent('wenjin-access-catalog-invalidated'));
  for (let index = 0; index < 10 && libraryReads < 2; index += 1) await new Promise((resolve) => setImmediate(resolve));

  assert.equal(libraryReads, 2, 'the current account catalog is reread after the delayed write commits');
  assert.equal(app.state.searchRevision, beforeRevision + 1, 'in-flight retrieval responses are invalidated');
  assert.equal(app.state.lastSearch, null);
  assert.equal(app.state.lastSearchTarget, null);
  assert.equal(elements.get('#search-library').value, '');
  assert.equal(elements.get('#search-source').value, '');
  assert.equal(elements.get('#search-document').value, '');
  assert.equal(elements.get('#evidence-list').children.length, 1, 'old evidence is replaced by the refresh prompt');
  assert.equal(app.state.connected, true, 'a neutral catalog refresh does not masquerade as disconnecting');
});

test('private catalog candidates stay out of search and lost readability clears evidence and drops a late result', async () => {
  let revoked = false;
  let searchStarted;
  let releaseSearch;
  const source = (candidate = false, readable = true) => ({
    source_type: 'shared', source_scope: candidate ? 'private_candidate' : 'private_config',
    publication_status: candidate ? 'needs_review' : 'published',
    corpus_id: candidate ? 'candidate-corpus' : 'private-corpus',
    index_target_id: candidate ? 'candidate-target' : 'private-target',
    version: candidate ? null : 'v2', version_id: candidate ? null : 'private-version-v2',
    is_active: !candidate, readable, searchable: !candidate && readable,
    documents: [{ document_id: candidate ? 'candidate-doc' : 'private-doc', display_name: candidate ? '候选说明.md' : '私域说明.md', source_path: candidate ? 'candidate.md' : 'private.md', readable: !candidate && readable, searchable: !candidate && readable }],
  });
  const catalog = (readable = true) => [{
    library_id: 'private-docs', library: '内部资料',
    sources: [source(false, readable), source(true, false)],
  }];
  const { app, elements } = makeApp(async ({ path }) => {
    if (path === '/api/admin/libraries') return response({ libraries: catalog(!revoked) });
    if (path === '/api/admin/search') {
      searchStarted?.();
      return await new Promise((resolve) => {
        releaseSearch = () => resolve(response({
          query: 'synthetic private query', context_char_budget: 1000,
          retrieval: {}, evidence_status: 'verified',
          evidence: [{ rank: 1, source_path: 'private.md', version: 'v2', text: 'late private evidence' }],
        }));
      });
    }
    throw new Error(`unexpected catalog/search path ${path}`);
  });
  app.state.connected = true;
  await app.loadLibraries();
  app.renderSearchLibraries();
  elements.get('#search-library').value = 'private-docs';
  app.renderSearchSources();
  const searchOptions = elements.get('#search-source').options;
  assert.equal(searchOptions.length, 2, 'only the placeholder and published private version are selectable');
  assert.match(searchOptions[1].textContent, /私域共享资料/);
  assert.doesNotMatch(searchOptions.map((item) => item.textContent).join(' '), /候选/);
  for (const selector of ['#search-query', '#search-top-k', '#search-budget']) {
    if (!elements.has(selector)) elements.set(selector, new Element());
  }
  elements.get('#search-query').value = 'synthetic private query';
  elements.get('#search-top-k').value = '5';
  elements.get('#search-budget').value = '1000';
  let resolveStarted;
  const started = new Promise((resolve) => { resolveStarted = resolve; });
  searchStarted = resolveStarted;
  const pendingSearch = app.runSearch({ preventDefault() {} });
  await started;
  while (!releaseSearch) await new Promise((resolve) => setImmediate(resolve));

  const selectedSourceKey = app.sourceKey(source(false));
  app.state.lastSearch = { evidence: [{ text: 'previous private evidence' }] };
  app.state.lastSearchLibraryId = 'private-docs';
  app.state.lastSearchTarget = { library_id: 'private-docs', source_key: selectedSourceKey, document_id: 'private-doc' };
  const oldEvidence = new Element(); oldEvidence.textContent = 'previous private evidence';
  elements.get('#evidence-list').append(oldEvidence);
  revoked = true;
  await app.loadLibraries();
  assert.equal(elements.get('#search-source').value, '', 'the revoked source selection is cleared while its library may remain visible');
  assert.equal(app.state.lastSearch, null, 'cached evidence is removed as soon as the refreshed catalog reports revocation');
  assert.doesNotMatch(elements.get('#evidence-list').children.map((item) => item.textContent || '').join(' '), /previous private evidence/, 'previous evidence is no longer rendered');
  releaseSearch();
  await pendingSearch;
  assert.equal(app.state.lastSearch, null, 'a late response cannot restore evidence after the access snapshot changed');
  assert.doesNotMatch(elements.get('#evidence-list').children.map((item) => item.textContent || '').join(' '), /late private evidence/);
});

test('library classification uses revision CAS and remains separate from per-user grants', async () => {
  let current = { classification: 'private', revision: 7, defaulted: true, source_visibility: 'private' };
  let putPayload = null;
  const library = () => ({
    library_id: 'engineering', library: '工程资料', classification: { ...current },
    sources: [{
      source_type: 'shared', source_scope: 'public_index', corpus_id: 'engineering-v2',
      version: '2.0', version_id: 'engineering-v2-id', index_target_id: 'engineering-index',
      searchable: true, readable: true,
      documents: [{ document_id: 'guide', display_name: 'guide.md', searchable: true, readable: true }],
    }],
  });
  const identity = { mode: 'accounts', user: { user_id: 'admin-1', username: '管理员', role: 'admin' }, csrf: 'csrf-admin' };
  const { app, elements, requests, window } = makeApp(async ({ path, method, options }) => {
    if (path === '/api/admin/libraries' && method === 'GET') return response({ libraries: [library()] });
    if (path === '/api/admin/libraries/engineering/classification' && method === 'GET') return response({ classification: { ...current } });
    if (path === '/api/admin/libraries/engineering/classification' && method === 'PUT') {
      putPayload = JSON.parse(options.body);
      current = { classification: putPayload.classification, revision: current.revision + 1, defaulted: false, source_visibility: 'private' };
      return response({ classification: { ...current } });
    }
    throw new Error(`unexpected classification request ${method} ${path}`);
  });
  window.WenjinIdentity = identity;
  app.state.connected = true;
  app.state.libraries = [library()];
  elements.set('#file-search', new Element());
  elements.set('#file-classification-filter', new Element()); elements.get('#file-classification-filter').value = 'private';
  elements.set('#file-status-filter', new Element()); elements.get('#file-status-filter').value = 'all';
  elements.set('#file-type-filter', new Element()); elements.get('#file-type-filter').value = 'all';
  app.renderFiles();
  const details = elements.get('#library-groups').children[0];
  const summary = details.children[0];
  const control = details.children.find((child) => child.className === 'library-classification-control');
  assert.equal(details.tagName, 'details');
  assert.ok(summary.children.some((child) => child.className?.startsWith('library-classification-badge ')), 'the collapsed summary shows a compact category badge');
  assert.equal(summary.children[0].children[0].tagName, 'svg', 'the summary uses the shared inline SVG chevron');
  assert.ok(control, `an administrator sees the classification editor inside the expandable library group (connected=${app.state.connected}, role=${window.WenjinIdentity.user.role}, children=${JSON.stringify(details?.children?.map((item) => item.className))})`);
  assert.match(control.children[2].textContent, /不授予或撤销读取权限/);
  await control.children[1].children[1].click();
  assert.ok(requests.some((item) => item.path === '/api/admin/libraries/engineering/classification' && item.method === 'GET'), 'refresh reads the current classification metadata');
  const refreshedDetails = elements.get('#library-groups').children[0];
  const refreshedControl = refreshedDetails.children.find((child) => child.className === 'library-classification-control');
  const select = refreshedControl.children[1].children[0];
  const save = refreshedControl.children[1].children[2];
  select.value = 'public';
  for (const handler of select.listeners.get('change') || []) handler({});
  await save.click();

  assert.deepEqual(putPayload, { classification: 'public', expected_revision: 7 });
  assert.equal(app.state.libraries[0].classification.classification, 'public');
  assert.equal(app.state.libraries[0].classification.revision, 8);
  assert.ok(requests.some((item) => item.path === '/api/admin/libraries'), 'the saved category is synchronized from the directory');
  assert.equal(requests.some((item) => item.path.includes('/grants')), false, 'classification changes do not mutate per-user grants');
  assert.equal(elements.get('#library-groups').children[0].className, 'panel library-empty', 'saving the category redraws the active classification filter');
});

test('file list hides the default empty managed-files group and filters pending content by classification, search, and status', () => {
  const { app, elements } = makeApp(async () => response({}));
  app.state.connected = true;
  app.state.libraries = [
    { library_id: 'managed-files', library: '受管文件', classification: { classification: 'private' }, sources: [] },
    { library_id: 'new-empty', library: '新建空库', managed_library: true, classification: { classification: 'public' }, sources: [] },
  ];
  app.state.files = [];
  elements.set('#file-search', new Element());
  elements.set('#file-classification-filter', new Element()); elements.get('#file-classification-filter').value = 'all';
  elements.set('#file-status-filter', new Element()); elements.get('#file-status-filter').value = 'all';
  elements.set('#file-type-filter', new Element()); elements.get('#file-type-filter').value = 'all';
  app.renderFiles();
  assert.deepEqual(elements.get('#library-groups').children.map((item) => item.dataset.libraryId), ['new-empty'], 'the default placeholder is hidden while a created empty library remains');

  app.state.files = [{ managed: true, file_id: 'pending-private-1', library_id: 'managed-files', library: '受管文件', display_name: 'pending-needle.md', status: 'importing', version: '1', active_version_id: null }];
  elements.get('#file-classification-filter').value = 'public';
  app.renderFiles();
  assert.deepEqual(elements.get('#library-groups').children.map((item) => item.dataset.libraryId), ['new-empty'], 'the pending private file is excluded by the public category filter');

  elements.get('#file-classification-filter').value = 'private';
  app.renderFiles();
  assert.deepEqual(elements.get('#library-groups').children.map((item) => item.dataset.libraryId), ['managed-files'], 'pending content keeps its default group visible although its active catalog is empty');
  elements.get('#file-search').value = 'needle';
  app.renderFiles();
  assert.equal(elements.get('#library-groups').children[0].dataset.libraryId, 'managed-files', 'pending files match the same text search');
  elements.get('#file-search').value = 'missing';
  app.renderFiles();
  assert.equal(elements.get('#library-groups').children[0].className, 'panel library-empty', 'pending files obey text search');
  elements.get('#file-search').value = '';
  elements.get('#file-status-filter').value = 'failed';
  app.renderFiles();
  assert.equal(elements.get('#library-groups').children[0].className, 'panel library-empty', 'pending files obey status filter');
});

test('private release sources use source visibility for labels and keep directory searchability separate from library classification', () => {
  const { app } = makeApp(async () => response({}));
  const privateRelease = {
    source_type: 'shared', source_scope: 'release_active', source_visibility: 'private',
    searchable: true, readable: true, is_active: true,
    publication_status: null,
  };
  assert.equal(app.sourceDisplayName(privateRelease), '私域共享资料');
  assert.equal(app.sourcePublicationLabel(privateRelease), '当前使用 · 私域来源');
  assert.equal(app.sourceSearchable(privateRelease), true);
  assert.equal(app.sourceSearchable({ ...privateRelease, source_scope: 'private_candidate' }), false);
});

test('forbidden evaluation responses are presented in Chinese instead of the raw error code', async () => {
  const { app, window } = makeApp(async () => response({ error: 'forbidden' }, 403));
  window.WenjinIdentity = { mode: 'accounts', user: { user_id: 'admin', role: 'admin' } };
  app.state.connected = true;
  app.state.evaluationSelectedId = 'synthetic-job';

  let failure;
  try {
    await app.loadEvaluationResult('synthetic-job');
  } catch (error) {
    failure = error;
  }

  assert.ok(failure, 'the forbidden response remains a rejected request');
  assert.notEqual(failure.message, 'forbidden');
  assert.match(failure.message, /[\u4e00-\u9fff]/, 'the UI-facing message should be understandable Chinese');
  assert.doesNotMatch(failure.message, /token|secret|[A-Za-z]:\\/i);
});

test('release library selector uses a readable name without duplicating osg.osg', () => {
  const { app, elements } = makeApp(async () => response({}));
  const select = new Element();
  elements.set('#release-library', select);
  app.state.releaseLibraries = [
    { library_id: 'osg', library: 'osg' },
    { library_id: 'docs', library: '技术资料' },
  ];
  app.renderReleaseLibraryOptions();
  assert.deepEqual(select.options.map((option) => option.value), ['', 'osg', 'docs']);
  assert.equal(select.options.find((option) => option.value === 'osg').textContent, 'osg');
  assert.equal(select.options.some((option) => option.textContent === 'osg · osg'), false);
  assert.equal(select.options.find((option) => option.value === 'docs').textContent, '技术资料');
});

test('job polling refreshes only on catalog changes and captures terminal replacement identity', async () => {
  let file = managedFile('running', 'version-old');
  let libraryReads = 0;
  const snapshots = [];
  const { app, timers } = makeApp(async ({ path }) => {
    if (path.startsWith('/api/jobs?')) return response({ jobs: [{ job_id: 'job-replace', status: file.status === 'active' ? 'completed' : 'running' }], offset: 0 });
    if (path === '/api/files') return response({ files: [file] });
    if (path === '/api/admin/libraries') { libraryReads += 1; snapshots.push([file.status, file.active_version_id]); return response({ libraries: catalog(file) }); }
    throw new Error(`unexpected ${path}`);
  });
  app.state.connected = true;
  app.state.files = [file];
  await app.loadFiles(); // establish the observed inventory baseline
  await app.loadLibraries();
  libraryReads = 0;
  await app.loadJobs({ quiet: true });
  const readsAfterFirstPoll = libraryReads;
  await app.loadJobs({ quiet: true });
  assert.equal(libraryReads, readsAfterFirstPoll, 'unchanged running polls do not rescan all libraries');
  file = managedFile('active', 'version-new');
  await app.loadJobs({ quiet: true });
  assert.equal(app.state.libraries[0]?.sources[0]?.version_id, 'version-new', 'completion replaces the stale search identity');
  assert.equal(libraryReads, readsAfterFirstPoll + 1, 'the terminal catalog change triggers one refresh');
});

test('replacement terminal identity and delete removal update the catalog; retry refreshes changed file state', async () => {
  let file = managedFile('active', 'version-1');
  let jobStatus = 'running';
  const { app, requests, elements } = makeApp(async ({ path, method }) => {
    if (path === '/api/files/file-1' && method === 'PUT') { file = managedFile('running', 'version-1'); return response({ job_id: 'job-replace' }, 202); }
    if (path === '/api/files/file-1' && method === 'DELETE') { file = managedFile('deleting', 'version-1'); return response({ job_id: 'job-delete' }, 202); }
    if (path === '/api/jobs/job-retry/retry' && method === 'POST') { file = managedFile('running', 'version-1'); jobStatus = 'queued'; return response({ job_id: 'job-retry' }, 202); }
    if (path.startsWith('/api/jobs?')) return response({ jobs: [{ job_id: 'job-active', status: jobStatus }], offset: 0 });
    if (path === '/api/files') return response({ files: file ? [file] : [] });
    if (path === '/api/admin/libraries') return response({ libraries: file?.status === 'active' ? catalog(file) : [] });
    throw new Error(`unexpected ${method} ${path}`);
  });
  app.state.connected = true;
  app.state.files = [file];
  await app.loadFiles();
  await app.loadLibraries();
  let input = elements.get('#upload-input');
  if (!input) { input = new Element(); elements.set('#upload-input', input); }
  input.files = [{ name: 'replacement.md', size: 13 }];
  app.startUploadPicker(file);
  await input.onchange();
  file = managedFile('active', 'version-2');
  await app.loadFiles({ quiet: true });
  assert.equal(app.state.libraries[0]?.sources[0]?.version_id, 'version-2', 'replacement completion publishes the new version identity');

  await app.deleteFile(file);
  assert.deepEqual(app.state.libraries, [], 'the deleting file is no longer offered as a ready search source');
  file = null;
  jobStatus = 'completed';
  await app.loadJobs({ quiet: true });
  assert.deepEqual(app.state.files, [], 'terminal deletion removes the file from the live catalog');

  file = managedFile('failed', 'version-2');
  await app.loadFiles();
  await app.loadLibraries();
  await app.retryJob({ job_id: 'job-retry', status: 'failed', retryable: true });
  assert.ok(requests.some((request) => request.path === '/api/jobs/job-retry/retry' && request.method === 'POST'));
  assert.equal(app.state.files[0]?.status, 'running', 'retry refreshes the changed managed-file status');
});

test('a second file identity change during a catalog fetch is not lost', async () => {
  let file = managedFile('running', 'version-1');
  let releaseFirst;
  let libraryRead = 0;
  const { app } = makeApp(async ({ path }) => {
    if (path === '/api/files') return response({ files: [file] });
    if (path === '/api/admin/libraries') {
      libraryRead += 1;
      const snapshot = catalog(file);
      if (libraryRead === 2) await new Promise((resolve) => { releaseFirst = resolve; });
      return response({ libraries: snapshot });
    }
    throw new Error(`unexpected ${path}`);
  });
  app.state.connected = true;
  app.state.files = [file];
  await app.loadFiles();
  await app.loadLibraries();
  file = managedFile('importing', 'version-1');
  const firstRefresh = app.loadFiles();
  while (!releaseFirst) await new Promise((resolve) => setImmediate(resolve));
  file = managedFile('active', 'version-2');
  const secondRefresh = app.loadFiles();
  releaseFirst();
  await Promise.all([firstRefresh, secondRefresh]);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(app.state.libraries[0]?.sources[0]?.version_id, 'version-2');
  assert.equal(libraryRead, 3, 'baseline plus both serialized changes each read one complete snapshot');
});

test('a failed catalog refresh stays dirty and gets one delayed retry', async () => {
  let file = null;
  let libraryRead = 0;
  const { app, timers } = makeApp(async ({ path }) => {
    if (path === '/api/files') return response({ files: file ? [file] : [] });
    if (path === '/api/admin/libraries') {
      libraryRead += 1;
      if (libraryRead === 2) return response({ detail: 'temporary catalog error' }, 503);
      return response({ libraries: file?.status === 'active' ? catalog(file) : [] });
    }
    throw new Error(`unexpected ${path}`);
  });
  app.state.connected = true;
  await app.loadFiles();
  await app.loadLibraries();
  file = managedFile('active', 'version-1');
  await app.loadFiles();
  assert.equal(app.state.fileCatalogDirty, true, 'a failed scan does not mark stale search choices as current');
  const retry = timers.get(app.state.libraryRetryTimer);
  assert.equal(retry?.delay, 3500, 'failure schedules a delayed retry rather than a tight loop');
  retry.callback();
  for (let i = 0; i < 10 && app.state.fileCatalogDirty; i += 1) await new Promise((resolve) => setImmediate(resolve));
  assert.equal(app.state.fileCatalogDirty, false, 'the bounded delayed retry can publish the refreshed catalog');
  assert.equal(app.state.libraries[0]?.sources[0]?.version_id, 'version-1');
  assert.equal(libraryRead, 3, 'one baseline read, one failed read, and one retry; no immediate retry loop');
});

test('an old catalog refresh cannot clear a refresh queued after disconnect and reconnect', async () => {
  let libraryRead = 0;
  let releaseOld;
  let releaseNew;
  const { app } = makeApp(async ({ path }) => {
    if (path !== '/api/admin/libraries') throw new Error(`unexpected ${path}`);
    libraryRead += 1;
    if (libraryRead === 1) { await new Promise((resolve) => { releaseOld = resolve; }); return response({ libraries: [{ library_id: 'old' }] }); }
    if (libraryRead === 2) { await new Promise((resolve) => { releaseNew = resolve; }); return response({ libraries: [{ library_id: 'new' }] }); }
    return response({ libraries: [{ library_id: 'newer' }] });
  });
  app.state.connected = true;
  const oldRefresh = app.scheduleLibraryRefresh();
  while (!releaseOld) await new Promise((resolve) => setImmediate(resolve));
  app.clearSensitiveState();
  app.state.connected = true;
  const reconnectRefresh = app.scheduleLibraryRefresh();
  releaseOld();
  while (!releaseNew) await new Promise((resolve) => setImmediate(resolve));
  releaseNew();
  const [oldResult, reconnectResult] = await Promise.allSettled([oldRefresh, reconnectRefresh]);
  assert.equal(oldResult.status, 'rejected', 'the request started before disconnect is rejected by the API generation guard');
  assert.equal(reconnectResult.status, 'fulfilled', 'the new generation refresh completes independently');
  assert.equal(app.state.libraries[0]?.library_id, 'new');
  assert.equal(libraryRead, 2, 'the new generation owns its own refresh and old completion does not erase it');
});

test('logout clears evaluation task and result state from the page', () => {
  const { app, elements } = makeApp(async () => response({}));
  app.state.evaluationTemplates = [{ experiment_id: 'synthetic' }];
  app.state.evaluationJobs = [{ job_id: 'job-a' }];
  app.state.evaluationAudit = [{ action: 'synthetic' }];
  app.state.evaluationSelectedId = 'job-a';
  app.state.evaluationComparison = { marker: 'synthetic-result' };
  app.state.connected = true;

  app.clearSensitiveState({ disconnected: true });

  assert.equal(app.state.evaluationTemplates.length, 0);
  assert.equal(app.state.evaluationJobs.length, 0);
  assert.equal(app.state.evaluationAudit.length, 0);
  assert.equal(app.state.evaluationSelectedId, null);
  assert.equal(app.state.evaluationComparison, null);
  assert.equal(elements.get('#evaluation-per-case').children.length, 0);
  assert.equal(app.state.connected, false);
});

test('a late result or case response from task A cannot replace the selected task B', async () => {
  let releaseResultA;
  let releaseCaseA;
  const resultB = {
    case_count: 1, valid_case_ids: ['case-b'], assets_unchanged: true,
    profiles: { baseline: { cases: 1, candidate_count: 2, selected_evidence: 1, mean_total_seconds: 0.1 } },
    marker: 'result-B',
  };
  const { app, requests, elements, window } = makeApp(async ({ path }) => {
    if (path === '/api/admin/evaluations/job-a/result?file=summary.json') {
      return await new Promise((resolve) => { releaseResultA = () => resolve(response({ marker: 'result-A', profiles: {} })); });
    }
    if (path === '/api/admin/evaluations/job-b/result?file=summary.json') return response(resultB);
    if (path === '/api/admin/evaluations/job-a/result?case_id=case-a') {
      return await new Promise((resolve) => { releaseCaseA = () => resolve(response({ query: 'stale case A', profiles: {} })); });
    }
    throw new Error(`unexpected request ${path}`);
  });
  window.WenjinIdentity = { mode: 'accounts', user: { user_id: 'admin', role: 'admin' } };
  app.state.connected = true;
  app.state.evaluationJobs = [{ job_id: 'job-a', status: 'completed' }, { job_id: 'job-b', status: 'completed' }];

  const resultA = app.evaluationAction('view', 'job-a');
  while (!releaseResultA) await new Promise((resolve) => setImmediate(resolve));
  await app.evaluationAction('view', 'job-b');
  releaseResultA();
  await resultA;
  assert.equal(app.state.evaluationSelectedId, 'job-b');
  assert.equal(app.state.evaluationComparison.marker, 'result-B');

  const oldGeneration = app.state.evaluationGeneration;
  const oldEpoch = app.state.evaluationSelectionEpoch;
  const lateCaseA = app.loadEvaluationCase('job-a', 'case-a', oldGeneration, oldEpoch);
  while (!releaseCaseA) await new Promise((resolve) => setImmediate(resolve));
  await app.evaluationAction('view', 'job-b');
  const caseArea = elements.get('#evaluation-per-case');
  const currentChildren = [...caseArea.children];
  releaseCaseA();
  await lateCaseA;
  assert.deepEqual(caseArea.children, currentChildren, 'late case detail is ignored after selection moved to B');
  assert.equal(app.state.evaluationComparison.marker, 'result-B');
  assert.ok(requests.some((request) => request.path.includes('job-a')));
});

test('reader identity hides evaluation navigation and does not load evaluation data', async () => {
  const identity = { mode: 'accounts', user: { user_id: 'reader', role: 'reader' } };
  const { app, requests, elements, window } = makeApp(async ({ path }) => {
    throw new Error(`unexpected reader request ${path}`);
  });
  window.WenjinIdentity = identity;
  app.state.connected = true;

  app.syncReleaseAccess();
  await app.loadEvaluations();

  assert.equal(elements.get('[data-page="evaluations"]').classList.contains('hidden'), true);
  assert.equal(requests.some((request) => request.path.startsWith('/api/admin/evaluations')), false);
});
