/* 问津管理界面：只调用同源管理 API；API 文本一律经 textContent 写入 DOM。 */
 (async () => {
  'use strict';

  const LIMIT = 100;
  const ACTIVE_JOBS = new Set(['queued', 'running']);
  const state = {
    token: null, connected: false, config: null, files: [], jobs: [], managedLibraries: [],
    jobOffset: 0, jobHasNext: false, jobLookup: null, selectedFile: null,
    libraries: [], calls: [], callCoverage: null, callOffset: 0, callTotal: 0, callHasNext: false,
    callLimit: 50, callFilters: { type: 'all', status: 'all', target: '', retrieval_target: '' }, uploadLibraryId: '', uploadReplaceFile: null, callFilterTimer: null, callRevision: 0,
    searchTarget: null,
    expandedLibraries: new Set(), lastFileAutoExpanded: false,
    searchRevision: 0, lastSearchLibraryId: null, lastSearchTarget: null,
    pollTimer: null, busy: new Set(), libraryClassificationSaving: new Set(), lastSearch: null, generation: 0, detailRevision: 0,
    fileLoadRevision: 0, libraryLoadRevision: 0, fileCatalogSignature: null, fileCatalogDirty: false,
    libraryRefreshPromise: null, libraryRefreshGeneration: null, libraryRefreshQueued: false,
    libraryRetryTimer: null, libraryRetryAttempted: false,
    releaseLibraryId: '', releaseLibraries: [], releaseTargets: [], releaseSnapshot: null, releaseJob: null, releasePollTimer: null, releaseLoadRevision: 0,
    evaluationTemplates: [], evaluationJobs: [], evaluationAudit: [], evaluationSelectedId: null,
    evaluationComparison: null, evaluationGeneration: 0, evaluationPollTimer: null,
    evaluationSelectionEpoch: 0, evaluationLoadPromise: null, lastPermissionRefreshAt: 0,
  };
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
  const text = (value, fallback = '—') => value === null || value === undefined || value === '' ? fallback : String(value);
  const escPath = (value) => encodeURIComponent(String(value));

  function node(tag, className, content) {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (content !== undefined && content !== null) item.textContent = String(content);
    return item;
  }

  function append(parent, ...children) {
    for (const child of children) if (child) parent.append(child);
    return parent;
  }

  function toast(message, kind = 'success') {
    const box = node('div', `toast ${kind === 'error' ? 'error' : ''}`);
    append(box, node('span', 'toast-icon', kind === 'error' ? '!' : '✓'), node('span', '', message));
    $('#toast-stack').append(box);
    window.setTimeout(() => box.remove(), 4600);
  }

  function setBusy(key, value) {
    if (value) state.busy.add(key);
    else state.busy.delete(key);
    $$(`[data-busy="${CSS.escape(key)}"]`).forEach((button) => { button.disabled = value; });
  }

  function setConnection(mode, message) {
    const pill = $('#connection-pill');
    pill.classList.remove('connected', 'error');
    const dotLabel = $('span', pill);
    if (mode === 'connected') {
      pill.classList.add('connected');
      dotLabel.textContent = '已连接';
      $('#sidebar-service-state').textContent = '服务在线';
    } else if (mode === 'error') {
      pill.classList.add('error');
      dotLabel.textContent = '连接异常';
      $('#sidebar-service-state').textContent = '需要检查连接';
    } else {
      dotLabel.textContent = '等待连接';
      $('#sidebar-service-state').textContent = '等待连接';
    }
    if (message) {
      $('#banner-message').textContent = message;
      $('#connection-banner').classList.remove('hidden');
    } else {
      $('#connection-banner').classList.add('hidden');
    }
    renderAccountMenu();
  }

  function renderAccountMenu() {
    const identity = window.WenjinIdentity;
    const inAccountMode = identity?.mode === 'accounts';
    const user = inAccountMode ? identity.user : null;
    const avatar = $('#account-avatar');
    const name = $('#account-menu-name');
    const role = $('#account-menu-role');
    const session = $('#account-menu-session');
    const switchButton = $('#account-switch-account');
    const logoutButton = $('#account-logout');
    const connectButton = $('#account-connect');
    const disconnectButton = $('#account-disconnect');
    if (inAccountMode) {
      const roleLabel = user?.role === 'admin' ? '管理员' : user ? '只读用户' : '等待登录';
      name.textContent = user?.username || '本机账户';
      role.textContent = roleLabel;
      avatar.textContent = user?.role === 'admin' ? '管' : user?.username?.slice(0, 1) || '问';
      avatar.setAttribute('aria-label', user ? `${user.username}，${roleLabel}` : '未登录账户');
      session.textContent = user ? '本机账号会话 · 凭据绑定当前账号' : '登录后可切换账号或退出登录';
      switchButton.classList.toggle('hidden', !user);
      logoutButton.classList.toggle('hidden', !user);
      connectButton.classList.add('hidden');
      disconnectButton.classList.add('hidden');
    } else {
      const label = state.token ? 'Bearer 管理连接' : state.connected ? '本机管理服务' : '服务未连接';
      name.textContent = label;
      role.textContent = state.token ? '旧版 Bearer 认证' : state.connected ? '无需账号的本机连接' : '连接管理服务';
      avatar.textContent = '管';
      avatar.setAttribute('aria-label', label);
      session.textContent = state.token ? 'Bearer 凭据只保留在当前页面内存，不会写入浏览器存储或 URL。' : '此服务使用管理连接，不显示独立用户账号。';
      switchButton.classList.add('hidden');
      logoutButton.classList.add('hidden');
      connectButton.classList.remove('hidden');
      disconnectButton.classList.toggle('hidden', !state.connected && !state.token);
    }
  }

  function clearSensitiveState({ disconnected = false } = {}) {
    state.generation += 1;
    // 账号切换会使创建请求过期；不要让旧请求的 finally 留下禁用状态。
    $('#create-library-button').disabled = false;
    $('#new-library-name').value = '';
    state.detailRevision += 1;
    state.connected = false;
    state.files = [];
    state.fileCatalogSignature = null;
    state.fileCatalogDirty = false;
    state.fileLoadRevision += 1;
    state.libraryLoadRevision += 1;
    state.libraryRefreshPromise = null;
    state.libraryRefreshGeneration = null;
    state.libraryRefreshQueued = false;
    state.libraryRetryAttempted = false;
    if (state.libraryRetryTimer) window.clearTimeout(state.libraryRetryTimer);
    state.libraryRetryTimer = null;
    state.jobs = [];
    state.jobOffset = 0;
    state.jobHasNext = false;
    state.jobLookup = null;
    state.selectedFile = null;
    state.config = null;
    state.managedLibraries = [];
    state.libraries = [];
    state.calls = [];
    state.callCoverage = null;
    state.callOffset = 0;
    state.callTotal = 0;
    state.callHasNext = false;
    state.callFilters = { type: 'all', status: 'all', target: '', retrieval_target: '' };
    $('#call-retrieval-filter').value = '';
    state.searchTarget = null;
    state.uploadLibraryId = '';
    state.uploadReplaceFile = null;
    state.expandedLibraries.clear();
    state.lastFileAutoExpanded = false;
    state.lastSearch = null;
    state.lastSearchLibraryId = null;
    state.lastSearchTarget = null;
    state.searchRevision += 1;
    state.releaseTargets = [];
    state.releaseLibraries = [];
    state.releaseSnapshot = null;
    state.releaseJob = null;
    state.releaseLibraryId = '';
    state.releaseLoadRevision += 1;
    if (state.releasePollTimer) window.clearTimeout(state.releasePollTimer);
    state.releasePollTimer = null;
    state.evaluationGeneration += 1;
    state.evaluationSelectionEpoch += 1;
    state.evaluationLoadPromise = null;
    state.evaluationTemplates = [];
    state.evaluationJobs = [];
    state.evaluationAudit = [];
    state.evaluationSelectedId = null;
    state.evaluationComparison = null;
    state.lastPermissionRefreshAt = 0;
    state.libraryClassificationSaving.clear();
    if (state.evaluationPollTimer) window.clearInterval(state.evaluationPollTimer);
    state.evaluationPollTimer = null;
    $('#evaluation-template')?.replaceChildren(new Option('读取允许的评测模板…', ''));
    $('#evaluation-job-rows')?.replaceChildren(node('tr', '', node('td', 'table-placeholder', '尚未读取评测任务')));
    $('#evaluation-arm-rows')?.replaceChildren(node('tr', '', node('td', 'table-placeholder', '没有已完成任务')));
    $('#evaluation-per-case')?.replaceChildren();
    $('#evaluation-audit-rows')?.replaceChildren(node('tr', '', node('td', 'table-placeholder', '连接后读取审计')));
    if ($('#evaluation-template-note')) $('#evaluation-template-note').textContent = '结果中的候选数量和耗时用于检索诊断，不直接代表答案语义正确率。';
    const dialog = $('#detail-dialog');
    if (dialog.open) dialog.close();
    $('#detail-content').replaceChildren();
    $('#results-meta').replaceChildren();
    $('#results-meta').classList.add('hidden');
    $('#evidence-list').replaceChildren(evidenceEmpty('等待检索', '连接管理服务后再查看证据'));
    $('#evidence-state').textContent = '尚未查询';
    $('#evidence-state').className = 'evidence-state';
    $('#results-subtitle').textContent = '运行一次检索后，真实返回片段会显示在这里';
    $('#admin-token').value = '';
    $('#search-query').value = '';
    $('#query-length').textContent = '0 / 2000';
    $('#file-search').value = '';
    $('#job-search').value = '';
    $('#job-id-lookup').value = '';
    $('#call-target-filter').value = '';
    $('#call-type-filter').value = 'all';
    $('#call-status-filter').value = 'all';
    $('#toast-stack').replaceChildren();
    if (disconnected) {
      state.token = null;
    state.connected = false;
    syncReleaseAccess();
      if (state.pollTimer) window.clearInterval(state.pollTimer);
      state.pollTimer = null;
      setConnection('idle');
    }
    renderAll();
    renderCallCoverage(null);
  }

  class ApiError extends Error {
    constructor(message, status, payload) { super(message); this.status = status; this.payload = payload; }
  }

  async function api(path, options = {}) {
    const generation = state.generation;
    const headers = new Headers(options.headers || {});
    if (window.WenjinIdentity?.mode === 'accounts') {
      headers.delete('Authorization');
      const method = (options.method || 'GET').toUpperCase();
      if (method !== 'GET' && method !== 'HEAD' && window.WenjinIdentity.csrf) headers.set('X-CSRF-Token', window.WenjinIdentity.csrf);
    } else if (state.token) headers.set('Authorization', `Bearer ${state.token}`);
    const response = await fetch(path, { ...options, headers, credentials: 'same-origin', cache: 'no-store' });
    if (generation !== state.generation) throw new ApiError('连接状态已变化，已忽略旧请求结果。', 0);
    if (response.status === 401) {
      clearSensitiveState({ disconnected: true });
      if (window.WenjinIdentity?.mode === 'accounts') {
        setConnection('error', '登录已过期，页面数据已清除，请重新登录。');
        window.WenjinIdentity.unauthorized().catch(() => {});
      } else {
        setConnection('error', '管理认证已失效。敏感数据已清除，请重新输入有效 token。');
        $('#banner-title').textContent = '管理认证失败';
        if (!$('#auth-dialog').open) $('#auth-dialog').showModal();
      }
      throw new ApiError('认证失败，请重新登录。', 401);
    }
    const contentType = response.headers.get('content-type') || '';
    let body = null;
    if (contentType.includes('application/json')) {
      try { body = await response.json(); } catch { body = null; }
    }
    if (generation !== state.generation) throw new ApiError('连接状态已变化，已忽略旧请求结果。', 0);
    if (!response.ok) {
      const safeMessages = {
        forbidden: '当前账号没有执行此操作的权限。',
        evaluation_library_access_required: body?.detail || '当前账号缺少评测所需资料库的读取授权，请到“身份与授权”处理后重试。',
        evaluation_forbidden: body?.detail || '当前账号无权提交此评测，请确认管理员身份和账号状态。',
        current_password_invalid: '当前密码不正确，请重新输入。',
        unauthorized: '登录已过期，请重新登录。',
      };
      const code = String(body?.error || '').toLowerCase();
      const message = text(body?.detail || safeMessages[code], `请求失败（HTTP ${response.status}）`);
      throw new ApiError(message, response.status, body);
    }
    return { response, body };
  }

  function showApiError(error, prefix = '') {
    if (error?.status === 401 || error?.status === 0) return;
    const message = `${prefix}${error?.message || '网络请求失败'}`;
    if (error instanceof TypeError) {
      clearSensitiveState();
      setConnection('error', '无法连接同源管理 API。请检查服务进程与页面地址。');
      $('#banner-title').textContent = '无法连接管理服务';
    }
    toast(message, 'error');
  }

  function statusLabel(status) {
    const names = {
      active: '已就绪', importing: '处理中', deleting: '清理中', failed: '有失败',
      legacy_unmanaged: '历史未受管', queued: '排队中', running: '执行中',
      completed: '已完成', deleted: '已删除', duplicate: '重复内容', cancelled: '已取消',
      preparing: '准备版本中', ready: '待切换', retained: '已保留', interrupted_after_restart: '服务重启后中断',
      failed_cleanup: '清理失败', cleanup_pending: '等待清理', verifying: '校验中',
    };
    return names[status] || text(status, '未知');
  }

  function phaseLabel(phase) {
    const names = {
      complete: '已完成', completed: '已完成', queued: '排队中', validating: '校验中', parsing: '解析中',
      embedding: '生成向量中', indexing: '写入索引中', activating: '切换版本中',
      cleanup: '清理文件中', cleanup_pending: '等待清理', interrupted: '服务中断',
      validating_and_sealing: '检查并准备版本', ready: '准备完成', failed: '失败',
      interrupted_after_restart: '服务重启后中断',
    };
    return names[phase] || text(phase, '等待开始');
  }

  function evidenceStatusLabel(status) {
    return ({verified:'已核验', unverified:'尚未核验', partial:'部分核验', stale:'版本已变化',
      source_modified:'原文已变化', integrity_failure:'完整性校验失败'}[status] || '状态未知');
  }

  function syncReleaseAccess() {
    if (window.WenjinIdentity?.mode !== 'accounts') return;
    const allowed = window.WenjinIdentity.user?.role === 'admin';
    $('[data-page="releases"]')?.classList.toggle('hidden', !allowed);
    if (!allowed && $('#page-releases')?.classList.contains('active')) navigate('search');
    $('[data-page="evaluations"]')?.classList.toggle('hidden', !allowed);
    if (!allowed && $('#page-evaluations')?.classList.contains('active')) navigate('identity');
  }

  function badge(status) {
    return node('span', `status-badge status-${String(status || 'unknown').replace(/[^a-z0-9_-]/gi, '')}`, statusLabel(status));
  }

  function formatDate(value) {
    if (!value) return '—';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }).format(date);
  }

  function formatNumber(value) {
    if (value === null || value === undefined || value === '') return '—';
    return Number.isFinite(Number(value)) ? new Intl.NumberFormat('zh-CN').format(Number(value)) : '—';
  }

  function refreshAuthorizedCatalog() {
    if (!state.connected || window.WenjinIdentity?.mode !== 'accounts') return Promise.resolve(null);
    const now = Date.now();
    if (now - state.lastPermissionRefreshAt < 5000) return Promise.resolve(null);
    state.lastPermissionRefreshAt = now;
    return loadLibraries({quiet: true});
  }

  function userLabel(userId) {
    if (!userId) return '系统';
    return window.WenjinIdentity?.usernameFor?.(userId) || '已删除账号';
  }

  function profileLabel(profile) {
    return ({baseline:'基础检索', parent_child:'父子上下文', multi_query:'多种问法', mmr:'结果去重'}[profile] || profile);
  }

  function evaluationStatus(status) {
    return ({queued:'排队中', preparing:'准备输入', running:'运行中', cancelling:'正在取消',
      interrupted:'已中断', cancelled:'已取消', failed:'失败', completed:'已完成', integrity_failure:'完整性校验失败'}[status] || statusLabel(status));
  }

  function evaluationStage(stage) {
    if (!stage) return '等待开始';
    const [prefix, suffix] = String(stage).split(/:(.*)/s, 2);
    const labels = {queued:'排队中', preparing:'准备评测输入', verifying_bound_inputs:'核对固定输入', ready:'输入已就绪',
      running:'检索评测中', cancelling:'正在取消', completed:'已完成', cancelled:'已取消', interrupted:'已中断',
      failed:'运行失败', integrity_failure:'完整性校验失败', baseline:'基础检索', parent_child:'父子上下文',
      multi_query:'多种问法', mmr:'结果去重', rerank:'结果重排', retrieve:'召回候选',
      checkpoint_reused:'复用已完成题目', case_complete:'题目已完成'};
    return `${labels[prefix] || prefix}${suffix ? ` · ${suffix}` : ''}`;
  }

  function evaluationDatasetLabel(datasetId, experimentId) {
    if (datasetId === 'developer-validation-2026-10-04') return '固定开发题集';
    if (datasetId === 'dv-gdal-003' || experimentId === 'gdal_dstalpha_diagnostic_v1') return 'GDAL专项题';
    return datasetId || experimentId || '固定评测';
  }

  function evaluationActionLabel(action) {
    return ({evaluation_submitted:'提交评测', evaluation_cancel_requested:'请求取消',
      evaluation_resume_requested:'恢复评测', evaluation_list_read:'查看评测列表',
      evaluation_status_read:'查看任务状态', evaluation_result_read:'查看评测结果',
      evaluation_access_revoked:'读取权限撤销', evaluation_context_close_failed:'关闭评测资源失败'}[action] || action || '未知操作');
  }

  function fileLabel(file) { return text(file.display_name, file.file_id); }
  function isManaged(file) { return file.managed !== false && !String(file.file_id || '').startsWith('legacy:'); }
  function buildFileIdentity(file) {
    const wrap = node('div', 'file-identity');
    const glyph = node('span', 'file-glyph');
    const icon = node('img', 'file-icon');
    icon.src = '/admin/file-icon.svg';
    icon.alt = '';
    icon.width = 25;
    icon.height = 30;
    glyph.append(icon);
    const copy = node('span', 'file-copy');
    append(copy, node('strong', '', fileLabel(file)), node('small', '', text(file.file_id)));
    return append(wrap, glyph, copy);
  }

  function safeExternalUrl(value) {
    if (!value) return null;
    try {
      const url = new URL(value, window.location.href);
      return ['https:', 'http:'].includes(url.protocol) ? url.href : null;
    } catch { return null; }
  }

  function resetRows(tbody, placeholder, colspan) {
    tbody.replaceChildren();
    if (placeholder) {
      const row = node('tr');
      const cell = node('td', 'table-placeholder', placeholder);
      cell.colSpan = colspan;
      row.append(cell);
      tbody.append(row);
    }
  }

  function renderMetrics() {
    if (!state.connected) {
      for (const id of ['#metric-files', '#metric-managed', '#metric-legacy', '#metric-chunks']) $(id).textContent = '—';
      $('#metric-files-sub').textContent = '连接服务后加载';
      $('#nav-file-count').textContent = '—';
      $('#footer-config').textContent = '等待连接管理服务';
      return;
    }
    const managed = state.files.filter(isManaged);
    const sharedDocuments = state.libraries.reduce((sum, library) => sum + (library.sources || []).filter((source) => source.source_type === 'shared').reduce((count, source) => count + (source.documents?.length || 0), 0), 0);
    const knownChunkCounts = managed.every((file) => file.chunk_count !== null && file.chunk_count !== undefined && file.chunk_count !== '' && Number.isFinite(Number(file.chunk_count)));
    const chunks = knownChunkCounts ? managed.reduce((sum, file) => sum + Number(file.chunk_count), 0) : null;
    $('#metric-files').textContent = formatNumber(state.files.length);
    $('#metric-managed').textContent = formatNumber(managed.length);
    $('#metric-legacy').textContent = formatNumber(sharedDocuments);
    $('#metric-chunks').textContent = formatNumber(chunks);
    $('#metric-files-sub').textContent = state.connected ? '文件管理器当前登记记录' : '连接服务后加载';
    $('#nav-file-count').textContent = state.connected ? formatNumber(state.files.length) : '—';
    const uploadMax = state.config?.max_upload_bytes;
    const authMode = state.config?.authentication_mode;
    const authLabel = authMode === 'accounts'
      ? '账号会话认证已启用'
      : authMode === 'bearer'
        ? 'Bearer 认证已启用'
        : authMode === 'none'
          ? '本机无需认证'
          : '认证状态未知';
    const retrieval = state.config?.retrieval;
    const retrievalLabel = retrieval
      ? `检索 Top${retrieval.top_k} · 候选 ${retrieval.candidate_k} · 重排 ${retrieval.reranker_enabled ? `${retrieval.rerank_pool} / ${retrieval.reranker_model_id}` : '关闭'}`
      : null;
    const uploadLabel = uploadMax ? `上传上限 ${formatBytes(uploadMax)}` : '配置未返回上传上限';
    $('#footer-config').textContent = state.connected
      ? [uploadLabel, retrievalLabel, authLabel].filter(Boolean).join(' · ')
      : '等待连接管理服务';
  }

  function renderOverview() {
    const tbody = $('#overview-file-rows');
    resetRows(tbody, state.connected && !state.files.length ? '服务当前未登记文件' : '连接管理服务后显示真实文件', 5);
    if (state.connected && state.files.length) {
      tbody.replaceChildren();
      const files = [...state.files].sort((a, b) => String(b.updated_at || '').localeCompare(String(a.updated_at || ''))).slice(0, 5);
      for (const file of files) {
        const row = node('tr');
        const name = node('td'); name.append(buildFileIdentity(file));
        const status = node('td'); status.append(badge(file.status));
        const version = node('td', '', text(file.version));
        const chunks = node('td', '', formatNumber(file.chunk_count));
        const updated = node('td', '', formatDate(file.updated_at));
        row.append(name, status, version, chunks, updated);
        row.addEventListener('click', () => openFileDetail(file.file_id));
        row.classList.add('clickable-row');
        tbody.append(row);
      }
    }
    $('#overview-file-foot').textContent = state.connected ? `${state.files.length} 条服务端文件记录` : '等待服务数据';
    const list = $('#overview-jobs');
    list.replaceChildren();
    const latest = [...state.jobs].sort((a, b) => String(b.created_at || '').localeCompare(String(a.created_at || ''))).slice(0, 4);
    if (!latest.length) list.append(node('div', 'empty-inline', state.connected ? '当前页没有任务记录' : '暂无任务数据'));
    for (const job of latest) {
      const row = node('div', 'activity-row');
      const mark = node('span', 'activity-mark', job.kind === 'delete' ? '×' : '↗');
      const main = node('span', 'activity-main');
      append(main, node('strong', '', `${job.kind === 'delete' ? '清理文件' : '导入资料'} · ${job.file_display_name || '名称未记录'}`), node('small', '', `${job.library_display_name || '资料库名称未记录'} · ${job.version || '版本信息未记录'} · ${statusLabel(job.status)}`));
      row.addEventListener('click', () => openJobDetail(job.job_id));
      row.classList.add('clickable-row');
      append(row, mark, main, node('span', 'activity-progress', `${Math.max(0, Math.min(100, Number(job.progress) || 0))}%`));
      list.append(row);
    }
    $('#overview-jobs-foot').textContent = state.connected ? '显示当前读取页内最近任务' : '任务记录来自管理服务';
  }

  function sourceKey(source) {
    return [source.source_type, source.corpus_id || '', source.file_id || '', source.version_id || '', source.index_target_id || ''].join('|');
  }

  function isPrivateSharedSource(source) {
    const privateVisibility = ['private', 'internal'].includes(String(source?.source_visibility || '').toLowerCase());
    return source?.source_type === 'shared' && (privateVisibility
      || source?.source_scope === 'private_config' || source?.source_scope === 'private_candidate');
  }

  function sourceReadable(source) { return source?.readable !== false; }

  function sourceSearchable(source) {
    // 未发布的私域候选不能检索；已发布或release中的私域索引按目录明确能力判断。
    const candidate = source?.source_scope === 'private_candidate'
      || (source?.source_scope === 'private_config' && source?.publication_status !== 'published');
    return sourceReadable(source) && source?.searchable !== false && !candidate;
  }

  function sourceDisplayName(source) {
    return isPrivateSharedSource(source) ? '私域共享资料' : source?.source_type === 'shared' ? '共享资料' : '受管文件';
  }

  function sourcePublicationLabel(source) {
    if (!isPrivateSharedSource(source)) return '';
    if (source.publication_status === 'published' && source.is_active === true) return '已发布 · 当前使用';
    if (source.source_scope === 'release_active' && source.is_active === true && source.searchable !== false) return '当前使用 · 私域来源';
    if (source.publication_status === 'ambiguous') return '目标不明确 · 需检查';
    if (source.publication_status === 'needs_review') return '需检查后发布';
    return '尚未发布 · 暂不可检索';
  }

  function canManageLibraryClassification() {
    const identity = window.WenjinIdentity;
    return state.connected && (identity?.mode !== 'accounts' || identity.user?.role === 'admin');
  }

  function renderLibraryClassificationControl(library) {
    if (!canManageLibraryClassification()) return null;
    const classification = library.classification;
    const value = classification?.classification === 'public' ? 'public' : 'private';
    const control = node('div', 'library-classification-control');
    const heading = node('div', 'library-classification-heading');
    append(heading,
      node('span', 'library-classification-label', '资料库分类'),
      node('strong', `library-classification-state ${value}`, value === 'public' ? '公开资料' : '私有资料'));
    if (classification?.defaulted) heading.append(node('small', 'library-classification-defaulted', '默认分类'));
    const select = node('select', 'library-classification-select');
    select.setAttribute('aria-label', `设置${text(library.library)}的资料库分类`);
    for (const [optionValue, label] of [['private', '私有资料'], ['public', '公开资料']]) {
      const option = node('option', '', label); option.value = optionValue; select.append(option);
    }
    select.value = value;
    const key = `library-classification:${library.library_id}`;
    const isBusy = state.libraryClassificationSaving.has(key);
    select.disabled = !classification || isBusy;
    const refresh = node('button', 'library-classification-refresh', '刷新分类');
    refresh.type = 'button'; refresh.disabled = isBusy;
    refresh.setAttribute('aria-label', `刷新${text(library.library)}分类`);
    refresh.addEventListener('click', () => refreshLibraryClassification(library.library_id));
    const save = node('button', 'library-classification-save', '保存分类');
    save.type = 'button'; save.disabled = !classification || isBusy || select.value === classification?.classification;
    select.addEventListener('change', () => { save.disabled = !classification || isBusy || select.value === classification?.classification; });
    save.addEventListener('click', () => saveLibraryClassification(library, select.value));
    const note = node('small', 'library-classification-note', '公开/私有仅作管理分类，不授予或撤销读取权限；普通账号的文件查看和检索仍按逐人授权执行。');
    control.append(heading);
    const actions = node('div', 'library-classification-actions');
    append(actions, select, refresh, save);
    control.append(actions, note);
    return control;
  }

  function renderLibraryClassificationBadge(library) {
    const value = library?.classification?.classification;
    if (!['public', 'private'].includes(value)) return null;
    return node('span', `library-classification-badge ${value}`, value === 'public' ? '公开资料' : '私有资料');
  }

  function libraryChevron() {
    const wrapper = node('span', 'library-chevron');
    const icon = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    icon.setAttribute('viewBox', '0 0 16 16');
    icon.setAttribute('aria-hidden', 'true');
    icon.setAttribute('focusable', 'false');
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', 'M3.5 5.5 8 10l4.5-4.5');
    path.setAttribute('fill', 'none');
    path.setAttribute('stroke', 'currentColor');
    path.setAttribute('stroke-width', '1.7');
    path.setAttribute('stroke-linecap', 'round');
    path.setAttribute('stroke-linejoin', 'round');
    icon.append(path);
    wrapper.append(icon);
    return wrapper;
  }

  async function refreshLibraryClassification(libraryId) {
    if (!canManageLibraryClassification()) return;
    const generation = state.generation;
    const actorId = window.WenjinIdentity?.user?.user_id || null;
    const key = `library-classification:${libraryId}`;
    if (state.libraryClassificationSaving.has(key)) return;
    state.libraryClassificationSaving.add(key);
    try {
      const { body } = await api(`/api/admin/libraries/${encodeURIComponent(libraryId)}/classification`);
      if (generation !== state.generation || actorId !== (window.WenjinIdentity?.user?.user_id || null)) return;
      state.libraries = state.libraries.map((library) => library.library_id === libraryId
        ? { ...library, classification: body?.classification || null } : library);
      renderAll();
    } catch (error) {
      if (generation === state.generation && error?.status !== 0) showApiError(error, '读取资料库分类失败：');
    } finally {
      if (generation === state.generation) {
        state.libraryClassificationSaving.delete(key);
        renderFiles();
      }
    }
  }

  async function saveLibraryClassification(library, classificationValue) {
    const classification = library.classification;
    if (!canManageLibraryClassification() || !classification || !['private', 'public'].includes(classificationValue)) return;
    if (classificationValue === classification.classification) return;
    const generation = state.generation;
    const actorId = window.WenjinIdentity?.user?.user_id || null;
    const libraryId = library.library_id;
    const key = `library-classification:${libraryId}`;
    if (state.libraryClassificationSaving.has(key)) return;
    state.libraryClassificationSaving.add(key);
    renderFiles();
    try {
      await api(`/api/admin/libraries/${encodeURIComponent(libraryId)}/classification`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ classification: classificationValue, expected_revision: classification.revision }),
      });
      if (generation !== state.generation || actorId !== (window.WenjinIdentity?.user?.user_id || null)) return;
      await loadLibraries({ quiet: true });
      if (generation !== state.generation || actorId !== (window.WenjinIdentity?.user?.user_id || null)) return;
      toast(`已更新“${text(library.library)}”分类：${classificationValue === 'public' ? '公开资料' : '私有资料'}`);
    } catch (error) {
      if (generation !== state.generation || error?.status === 0) return;
      if (error?.status === 409) {
        state.libraryClassificationSaving.delete(key);
        await refreshLibraryClassification(libraryId);
        if (generation === state.generation) toast('资料库分类已被其他管理员更新，已刷新当前值；请核对后再保存。', 'error');
      } else showApiError(error, '保存资料库分类失败：');
    } finally {
      if (generation === state.generation) {
        state.libraryClassificationSaving.delete(key);
        renderFiles();
      }
    }
  }

  function legacyForDocument(source, document) {
    if (source.source_type !== 'shared') return null;
    const baseName = String(document.source_path || '').split(/[\\/]/).pop();
    const candidates = state.files.filter((file) => file.managed === false
      && file.file_id.startsWith(`legacy:${source.corpus_id}:`)
      && file.display_name === baseName
      && (!source.version || file.version === source.version));
    return candidates.length === 1 ? candidates[0] : null;
  }

  function catalogGroups() {
    const query = $('#file-search').value.trim().toLocaleLowerCase();
    const statusFilter = $('#file-status-filter').value;
    const classificationFilter = $('#file-classification-filter').value;
    return state.libraries.map((library) => {
      if (classificationFilter !== 'all' && library.classification?.classification !== classificationFilter) {
        return null;
      }
      const sources = library.sources || [];
      const rendered = [];
      for (const source of sources) {
        const documents = (source.documents || []).map((document) => ({ source, document })).filter(({ source, document }) => {
          const managed = source.source_type === 'managed';
          const file = managed
            ? state.files.find((item) => item.file_id === source.file_id)
            : legacyForDocument(source, document);
      const status = file?.status || (source.readable === false || document.readable === false ? 'unauthorized' : managed ? 'active' : 'legacy_unmanaged');
      const extension = String(document.display_name || document.source_path || '').split('.').pop().toLowerCase();
      const fileType = extension === 'pdf' ? 'pdf' : ['txt', 'md', 'markdown', 'csv', 'json'].includes(extension) ? 'text' : ['html', 'htm'].includes(extension) ? 'html' : extension === 'xml' ? 'xml' : 'other';
          const haystack = [library.library_id, library.library, source.corpus_id, source.file_id, source.version, document.display_name, document.document_id, document.source_path].filter(Boolean).join(' ').toLocaleLowerCase();
          const typeFilter = $('#file-type-filter')?.value || 'all';
          return (!query || haystack.includes(query))
            && (statusFilter === 'all' || status === statusFilter)
            && (typeFilter === 'all' || typeFilter === fileType);
        });
        if (documents.length) rendered.push({ source, documents });
      }
      return { ...library, rendered, visibleCount: rendered.reduce((total, item) => total + item.documents.length, 0) };
    }).filter((group) => group && (group.visibleCount > 0 || (!query && group.managed_library === true
      && !(group.sources || []).some((source) => (source.documents || []).length > 0))));
  }

  function renderFiles() {
    const container = $('#library-groups');
    const queryActive = Boolean($('#file-search').value.trim());
    const expandedNow = new Set($$('.library-group[open]', container).map((item) => item.dataset.libraryId));
    if (!state.lastFileAutoExpanded) state.expandedLibraries = new Set([...state.expandedLibraries, ...expandedNow]);
    state.lastFileAutoExpanded = queryActive;
    const groups = catalogGroups();
    container.replaceChildren();
    if (!state.connected) { $('#file-count').textContent = '—'; container.append(node('div', 'panel table-placeholder', '连接管理服务后读取真实资料库目录')); return; }
    const catalogFileIds = new Set(state.libraries.flatMap((library) => (library.sources || []).filter((source) => source.source_type === 'managed').map((source) => source.file_id)));
    const classificationFilter = $('#file-classification-filter').value;
    const pending = state.files.filter((file) => isManaged(file) && !catalogFileIds.has(file.file_id)
      && (file.status !== 'deleted')
      && (statusFilterForPending(file, queryActive))
      && (classificationFilter === 'all' || state.libraries.find((library) => library.library_id === (file.library_id || 'managed-files'))?.classification?.classification === classificationFilter));
    const total = groups.reduce((sum, group) => sum + group.visibleCount, 0);
    const visibleLibraryIds = new Set([...groups.map((group) => group.library_id), ...pending.map((file) => file.library_id || 'managed-files')]);
    $('#file-count').textContent = `${formatNumber(total)} 条目录文档 · ${formatNumber(visibleLibraryIds.size)} 个资料库`;
    if (!groups.length && !pending.length) {
      const empty = node('div', 'panel library-empty');
      append(empty, node('div', 'empty-illustration', '⌕'), node('strong', '', state.libraries.length ? '没有匹配的文档' : '没有可用资料库'), node('p', '', '调整资料库类型、状态、文档类型或搜索词；分类标签不改变读取权限。'));
      container.append(empty);
      return;
    }
    for (const group of groups) {
      const details = node('details', 'library-group');
      details.dataset.libraryId = group.library_id;
      details.open = queryActive || state.expandedLibraries.has(group.library_id);
      details.addEventListener('toggle', () => {
        if (queryActive) return;
        if (details.open) state.expandedLibraries.add(group.library_id);
        else state.expandedLibraries.delete(group.library_id);
      });
      const summary = node('summary', 'library-summary');
      const summaryMain = node('span', 'library-summary-main');
      append(summaryMain, node('strong', '', text(group.library)));
      summaryMain.title = `资料库编号：${group.library_id}`;
      const count = node('span', 'library-summary-count', `${formatNumber(group.visibleCount)} 文档`);
      append(summary, libraryChevron(), summaryMain);
      const classificationBadge = renderLibraryClassificationBadge(group);
      if (classificationBadge) summary.append(classificationBadge);
      summary.append(count);
      details.append(summary);
      const classificationControl = renderLibraryClassificationControl(group);
      if (classificationControl) details.append(classificationControl);
      for (const entry of group.rendered) details.append(renderSourceGroup(group, entry.source, entry.documents));
      container.append(details);
    }
    // 未active的受管任务仍须可见；它们尚未出现在检索目录中，不能被误当作可检索来源。
    // 未就绪记录也按服务端 library_id 放回稳定资料库分组，不猜测其归属。
    const pendingByLibrary = new Map();
    for (const file of pending) {
      const libraryId = file.library_id || 'managed-files';
      if (!pendingByLibrary.has(libraryId)) pendingByLibrary.set(libraryId, []);
      pendingByLibrary.get(libraryId).push(file);
    }
    for (const [libraryId, files] of pendingByLibrary) {
      let group = [...container.querySelectorAll('.library-group')].find((item) => item.dataset.libraryId === libraryId);
      if (!group) {
        const library = state.libraries.find((item) => item.library_id === libraryId);
        group = node('details', 'library-group pending-library');
        group.dataset.libraryId = libraryId;
        group.open = queryActive || state.expandedLibraries.has(libraryId);
        group.addEventListener('toggle', () => { if (!queryActive) detailsToggle(libraryId, group.open); });
        const summary = node('summary', 'library-summary');
        const libraryTitle = node('span', 'library-summary-main', text(library?.library || files[0]?.library_name, '资料库名称未记录'));
        libraryTitle.title = `资料库编号：${libraryId}`;
        append(summary, libraryChevron(), libraryTitle);
        const classificationBadge = renderLibraryClassificationBadge(library);
        if (classificationBadge) summary.append(classificationBadge);
        summary.append(node('span', 'library-summary-count', `${files.length} 个未就绪`));
        group.append(summary);
        if (library) {
          const classificationControl = renderLibraryClassificationControl(library);
          if (classificationControl) group.append(classificationControl);
        }
        container.append(group);
      } else {
        const count = $('.library-summary-count', group);
        count.textContent = `${formatNumber(Number(count.textContent.match(/^\d+/)?.[0] || 0) + files.length)} 文档 · ${files.length} 个未就绪`;
      }
      for (const file of files) {
        const sourcePanel = node('section', 'source-group');
        const heading = node('div', 'source-heading');
        append(heading, node('span', 'source-kind managed', '受管文件'), node('strong', '', fileLabel(file)), node('span', 'source-version', `版本 ${text(file.version)}`), node('span', 'source-count', statusLabel(file.status)));
        sourcePanel.append(heading);
        sourcePanel.append(makeLibraryTable([{ file, doc: { display_name: file.display_name, document_id: file.file_id, searchable: false }, source: { source_type: 'managed', file_id: file.file_id, version: file.version, version_id: file.active_version_id, searchable: false, manageable: true }, library: { library_id: libraryId } }], libraryId));
        group.append(sourcePanel);
      }
    }
  }

  function statusFilterForPending(file, queryActive) {
    const filter = $('#file-status-filter').value;
    const q = $('#file-search').value.trim().toLocaleLowerCase();
    const haystack = [file.display_name, file.file_id, file.library, file.library_id].filter(Boolean).join(' ').toLocaleLowerCase();
    const extension = String(file.display_name || '').split('.').pop().toLowerCase();
    const type = extension === 'pdf' ? 'pdf' : ['txt', 'md', 'markdown', 'csv', 'json'].includes(extension) ? 'text' : ['html', 'htm'].includes(extension) ? 'html' : extension === 'xml' ? 'xml' : 'other';
    const typeFilter = $('#file-type-filter').value;
    return (filter === 'all' || filter === file.status) && (typeFilter === 'all' || typeFilter === type) && (!queryActive || haystack.includes(q));
  }

  function detailsToggle(id, open) { if (open) state.expandedLibraries.add(id); else state.expandedLibraries.delete(id); }

  function renderSourceGroup(library, source, documents) {
    const section = node('section', `source-group source-${source.source_type}`);
    const publication = sourcePublicationLabel(source);
    const title = source.source_type === 'shared' ? `${sourceDisplayName(source)} · 版本 ${text(source.version, publication ? '未发布' : '—')}` : `${fileLabel(state.files.find((file) => file.file_id === source.file_id) || { display_name: '名称未记录' })}`;
    const head = node('div', 'source-heading');
    head.title = `corpus_id：${text(source.corpus_id)} · file_id：${text(source.file_id)} · index_target_id：${text(source.index_target_id)}`;
    append(head, node('span', `source-kind ${source.source_type}`, source.source_type === 'shared' ? sourceDisplayName(source) : '受管文件'), node('strong', '', title));
    if (publication) head.append(node('span', `source-publication-status${source.publication_status === 'published' ? ' published' : ''}`, publication));
    if (source.version) head.append(node('span', 'source-version', `版本 ${text(source.version)}`));
    head.append(node('span', 'source-count', `${documents.length} 文档`));
    section.append(head);
    section.append(makeLibraryTable(documents.map(({ source: itemSource, document }) => ({ file: itemSource.source_type === 'managed' ? state.files.find((file) => file.file_id === itemSource.file_id) : legacyForDocument(itemSource, document), doc: document, source: itemSource, library })), sourceKey(source)));
    return section;
  }

  function makeLibraryTable(items, groupKey) {
    const wrap = node('div', 'table-wrap library-table-wrap');
    const table = node('table', 'data-table library-table');
    const head = node('thead');
    const header = node('tr');
    for (const label of ['文档', '状态', '检索范围', '片段', '操作']) header.append(node('th', '', label));
    head.append(header);
    const body = node('tbody');
    for (const item of items) {
      const { file, doc, source } = item;
      const managed = source.source_type === 'managed';
      const row = node('tr');
      const identityCell = node('td');
      const identity = node('div', 'file-identity');
      const glyph = node('span', 'file-glyph');
      const icon = node('img', 'file-icon'); icon.src = '/admin/file-icon.svg'; icon.alt = ''; icon.width = 25; icon.height = 30; glyph.append(icon);
      const copy = node('span', 'file-copy');
      const name = node('button', 'file-name-button', text(doc.display_name, doc.document_id));
      name.type = 'button';
      name.setAttribute('aria-label', `查看文档详情：${text(doc.display_name, doc.document_id)}`);
      name.addEventListener('click', (event) => {
        event.stopPropagation();
        openCatalogDocumentDetail(item);
      });
      append(copy, name, node('small', '', text(doc.source_path, managed ? file?.file_id : doc.document_id)));
      append(identity, glyph, copy); identityCell.append(identity);
      const statusCell = node('td');
      statusCell.append(managed ? badge(file?.status || 'active') : source.readable === false || doc.readable === false ? node('span', 'status-badge status-unauthorized', '未获读取授权') : node('span', 'status-badge status-legacy_unmanaged', '共享只读'));
      const canSearch = doc.readable !== false && doc.searchable !== false && sourceSearchable(source);
      const searchCell = node('td', '', !sourceReadable(source) || doc.readable === false ? '未获读取授权' : canSearch ? '可检索' : sourcePublicationLabel(source) || '不可检索');
      const chunksCell = node('td', '', managed ? formatNumber(file?.chunk_count) : '—');
      const actionCell = node('td', 'align-right');
      const actions = node('div', 'row-actions');
      const target = {
        library_id: item.library?.library_id,
        source_type: source.source_type,
        corpus_id: source.corpus_id,
        file_id: source.file_id,
        version_id: source.version_id,
        index_target_id: source.index_target_id,
        source,
        document_id: doc.document_id,
      };
      actions.append(actionButton('检索', 'search', doc.document_id, false, !canSearch, target));
      if (managed && file) {
        if (doc.replaceable && source.manageable && !['importing', 'deleting'].includes(file.status)) actions.append(actionButton('替换', 'replace', file.file_id));
        if (doc.deletable && source.manageable && file.status !== 'deleting') actions.append(actionButton('删除', 'delete', file.file_id, true));
        actions.append(actionButton('详情', 'detail', file.file_id));
      } else if (file?.legacy_id || String(file?.file_id || '').startsWith('legacy:')) {
        actions.append(actionButton('创建可管理副本', 'adopt', file.file_id));
      }
      actionCell.append(actions);
      row.append(identityCell, statusCell, searchCell, chunksCell, actionCell);
      body.append(row);
    }
    table.append(head, body); wrap.append(table);
    return wrap;
  }

  function actionButton(label, action, id, danger = false, disabled = false, searchTarget = null) {
    const button = node('button', `table-action ${danger ? 'danger' : ''}`, label);
    button.type = 'button';
    button.dataset.action = action;
    button.dataset.id = id;
    const busyKey = `${action}-${id}`;
    button.dataset.busy = busyKey;
    button.disabled = disabled || state.busy.has(busyKey);
    button.addEventListener('click', (event) => {
      event.stopPropagation();
      if (button.disabled || state.busy.has(busyKey)) return;
      const file = state.files.find((entry) => entry.file_id === id);
      if (action === 'detail') openFileDetail(id);
      else if (action === 'search') {
        // 详情属于模态层，跳转前关闭它，才能在背景检索表单中输入。
        if ($('#detail-dialog').open) $('#detail-dialog').close();
        openSearchTarget(searchTarget || (file ? { library_id: file.library_id || 'managed-files', source_type: 'managed', file_id: file.file_id, version_id: file.active_version_id } : null));
      }
      else if (action === 'download') downloadFile(file);
      else if (action === 'replace') chooseUpload({ replace: file });
      else if (action === 'delete') deleteFile(file);
      else if (action === 'adopt') adoptFile(file);
    });
    return button;
  }

  function openCatalogDocumentDetail(item) {
    const { file, doc, source, library } = item;
    if (source.source_type === 'managed' && file) {
      openFileDetail(file.file_id);
      return;
    }
    // 共享资料使用目录快照提供只读详情；绝不从管理页面构造下载或修改入口。
    state.detailRevision += 1;
    state.selectedFile = null;
    state.jobLookup = null;
    const content = $('#detail-content');
    content.replaceChildren();
    const isShared = source.source_type === 'shared';
    const privateSource = isPrivateSharedSource(source);
    const publication = sourcePublicationLabel(source);
    const canSearch = doc.readable !== false && doc.searchable !== false && sourceSearchable(source);
    const head = node('header', 'detail-head');
    append(head,
      node('span', 'section-kicker', isShared ? sourceDisplayName(source) : '受管资料'),
      node('h2', '', text(doc.display_name, doc.document_id)));
    const tags = node('div', 'detail-tags');
    tags.append(node('span', 'status-badge status-retained', isShared ? source.readable === false || doc.readable === false ? '未获读取授权' : '共享只读' : text(file?.status, '目录记录')));
    if (source.version) tags.append(node('span', 'meta-chip', `版本 ${text(source.version)}`));
    if (publication) tags.append(node('span', `source-publication-status${source.publication_status === 'published' ? ' published' : ''}`, publication));
    append(head, tags, node('p', '', isShared
      ? source.readable === false || doc.readable === false
        ? '当前账号可查看目录元数据，但尚未获得资料读取授权；本资料不可检索，也不提供下载或修改操作。'
        : privateSource ? '这是已发布的私域共享资料。管理界面提供目录查看和证据检索，不提供下载、替换或删除。'
          : '此文档来自资料库目录，管理界面仅提供查看和证据检索，不提供下载、替换或删除。'
      : '目录中存在此受管文档，但当前文件详情记录不可用；此处仅展示目录字段。'));
    content.append(head);
    const section = appendSection(content, '目录详情');
    const grid = node('div', 'detail-grid');
    append(grid,
      detailField('资料库', library?.library),
      detailField('library_id', library?.library_id),
      detailField('来源类型', isShared ? sourceDisplayName(source) : '受管文件'),
      detailField('发布状态', publication || (source.is_active === true ? '当前生效' : '目录版本')),
      detailField('读取授权', source.readable === false || doc.readable === false ? '未获读取授权' : '可读取'),
      detailField('file_id', source.file_id),
      detailField('corpus_id', source.corpus_id),
      detailField('版本', source.version),
      detailField('version_id', source.version_id),
      detailField('index_target_id', source.index_target_id),
      detailField('文档 ID', doc.document_id),
      detailField('目录相对路径', doc.source_path),
      detailField('可检索', canSearch ? '是' : '否'));
    section.append(grid);
    const actions = node('div', 'detail-actions');
    if (canSearch) {
      const target = {
        library_id: library?.library_id,
        source_type: source.source_type,
        corpus_id: source.corpus_id,
        file_id: source.file_id,
        version_id: source.version_id,
        index_target_id: source.index_target_id,
        document_id: doc.document_id,
      };
      actions.append(actionButton('检索此文档', 'search', doc.document_id, false, false, target));
    }
    if (actions.childElementCount) content.append(actions);
    if (!$('#detail-dialog').open) $('#detail-dialog').showModal();
  }

  function visibleJobs() {
    const q = $('#job-search').value.trim().toLocaleLowerCase();
    const kind = $('#job-kind-filter').value;
    const status = $('#job-status-filter').value;
    return state.jobs.filter((job) => {
      const matchText = `${job.job_id || ''} ${job.file_id || ''} ${job.reason || ''}`.toLocaleLowerCase();
      return (!q || matchText.includes(q)) && (kind === 'all' || job.kind === kind) && (status === 'all' || job.status === status);
    });
  }

  function renderJobs() {
    const running = state.jobs.filter((job) => ACTIVE_JOBS.has(job.status)).length;
    const done = state.jobs.filter((job) => ['completed', 'deleted', 'duplicate', 'cancelled'].includes(job.status)).length;
    const failed = state.jobs.filter((job) => job.status === 'failed').length;
    $('#job-running-count').textContent = formatNumber(running);
    $('#job-done-count').textContent = formatNumber(done);
    $('#job-failed-count').textContent = formatNumber(failed);
    $('#nav-job-count').textContent = state.connected ? formatNumber(state.jobs.length) : '—';
    $('#nav-job-count').title = '当前读取页的任务记录数';
    const jobs = visibleJobs();
    $('#job-count').textContent = state.connected ? `${jobs.length} / ${state.jobs.length} 条` : '—';
    $('#jobs-empty').classList.toggle('hidden', jobs.length > 0 || !state.connected);
    const rows = $('#job-rows');
    if (!state.connected || !jobs.length) {
      resetRows(rows, state.connected ? null : '连接管理服务后显示真实任务', 6);
    } else {
      rows.replaceChildren();
      for (const job of jobs) {
        const row = node('tr');
        const identity = node('td');
        append(identity, node('span', 'job-title', job.kind === 'delete'
          ? '清理全部受管版本'
          : `资料导入 · ${job.version || '版本信息未记录'}`));
        const fileCell = node('td');
        const file = state.files.find((item) => item.file_id === job.file_id);
        append(fileCell, node('span', 'job-title', job.file_display_name || (file ? fileLabel(file) : '名称未记录')),
          node('span', 'job-subtitle', job.library_display_name || '资料库名称未记录'));
        const status = node('td'); status.append(badge(job.status));
        const progressCell = node('td', 'progress-cell');
        const pct = Math.max(0, Math.min(100, Number(job.progress) || 0));
        const progressTop = node('div', 'progress-top');
        append(progressTop, node('span', '', phaseLabel(job.phase)), node('span', '', `${pct}%`));
        const track = node('div', 'progress-track');
        const fill = node('span'); fill.style.width = `${pct}%`; track.append(fill);
        append(progressCell, progressTop, track);
        const date = node('td', '', formatDate(job.created_at));
        const actionCell = node('td', 'align-right');
        const actions = node('div', 'row-actions');
        const detail = actionButton('查看', 'job-detail', job.job_id);
        detail.addEventListener('click', (event) => { event.stopPropagation(); openJobDetail(job.job_id); });
        actions.append(detail);
        if (job.status === 'failed' && job.retryable === true) {
          const retry = actionButton('重试', 'retry', job.job_id);
          retry.dataset.busy = `retry-${job.job_id}`;
          retry.disabled = state.busy.has(`retry-${job.job_id}`);
          retry.addEventListener('click', (event) => { event.stopPropagation(); retryJob(job); });
          actions.append(retry);
        }
        actionCell.append(actions);
        row.append(identity, fileCell, status, progressCell, date, actionCell);
        row.addEventListener('click', () => openJobDetail(job.job_id));
        row.classList.add('clickable-row');
        rows.append(row);
      }
    }
    const start = state.connected ? state.jobOffset + 1 : 0;
    const end = state.jobOffset + state.jobs.length;
    $('#job-page-note').textContent = state.connected ? (state.jobs.length ? `第 ${formatNumber(start)}–${formatNumber(end)} 条 · 每页 ${LIMIT} 条` : `第 ${formatNumber(state.jobOffset + 1)} 条之后没有任务`) : '等待连接管理服务';
    $('#jobs-prev').disabled = !state.connected || state.jobOffset <= 0 || state.busy.has('jobs-page');
    $('#jobs-next').disabled = !state.connected || !state.jobHasNext || state.busy.has('jobs-page');
  }

  function renderAll() {
    renderMetrics();
    renderOverview();
    renderFiles();
    renderSearchLibraries();
    renderUploadLibraries();
    renderJobs();
    renderCalls();
    $('#disconnect-button').classList.toggle('hidden', !state.connected || window.WenjinIdentity?.mode === 'accounts');
  }

  async function loadFiles({ quiet = false } = {}) {
    if (!state.connected) return [];
    const generation = state.generation;
    const revision = ++state.fileLoadRevision;
    const { body } = await api('/api/files');
    if (generation !== state.generation || revision !== state.fileLoadRevision || !state.connected) return state.files;
    const files = Array.isArray(body?.files) ? body.files : [];
    const signature = fileCatalogSignature(files);
    const directoryChanged = state.fileCatalogSignature !== null && signature !== state.fileCatalogSignature;
    state.fileCatalogSignature = signature;
    if (directoryChanged) {
      state.fileCatalogDirty = true;
      state.libraryRetryAttempted = false;
      if (state.libraryRetryTimer) window.clearTimeout(state.libraryRetryTimer);
      state.libraryRetryTimer = null;
    }
    state.files = files;
    renderAll();
    if (state.fileCatalogDirty) {
      try { await scheduleLibraryRefresh(); } catch { /* 文件操作已成功；目录刷新已提示并保留待重试状态。 */ }
    }
    if (!quiet) toast(`已读取 ${state.files.length} 条文件记录`);
    return state.files;
  }

  function fileCatalogSignature(files) {
    // 只用会影响检索目录的稳定字段；进度、时间戳等高频字段不会触发共享资产扫描。
    return JSON.stringify(files.map((file) => [
      String(file.file_id || ''), String(file.library_id || ''), String(file.status || ''), String(file.active_version_id || ''),
    ]).sort((left, right) => left[0].localeCompare(right[0])));
  }

  function scheduleLibraryRefresh() {
    if (!state.connected) return Promise.resolve([]);
    const generation = state.generation;
    state.libraryRefreshQueued = true;
    if (state.libraryRefreshPromise && state.libraryRefreshGeneration === generation) return state.libraryRefreshPromise;
    const promise = (async () => {
      while (state.libraryRefreshQueued && state.connected && generation === state.generation) {
        state.libraryRefreshQueued = false;
        const requestedSignature = state.fileCatalogSignature;
        const libraries = await loadLibraries({ quiet: true });
        if (!Array.isArray(libraries)) {
          scheduleLibraryRetry(generation);
          return state.libraries;
        }
        if (generation !== state.generation || !state.connected) return state.libraries;
        // 请求期间如果目录又变化，当前响应不能清掉新目录的待同步标记。
        if (state.libraryRefreshQueued || requestedSignature !== state.fileCatalogSignature) continue;
        state.fileCatalogDirty = false;
        state.libraryRetryAttempted = false;
        if (state.libraryRetryTimer) window.clearTimeout(state.libraryRetryTimer);
        state.libraryRetryTimer = null;
      }
      return state.libraries;
    })().catch((error) => {
      scheduleLibraryRetry(generation, error);
      throw error;
    });
    state.libraryRefreshPromise = promise;
    state.libraryRefreshGeneration = generation;
    promise.finally(() => {
      if (state.libraryRefreshPromise === promise && state.libraryRefreshGeneration === generation) {
        state.libraryRefreshPromise = null;
        state.libraryRefreshGeneration = null;
        if (state.libraryRefreshQueued && state.connected && generation === state.generation) scheduleLibraryRefresh().catch(() => {});
      }
    }).catch(() => {});
    return promise;
  }

  function scheduleLibraryRetry(generation, error = null) {
    if (generation !== state.generation || !state.connected || !state.fileCatalogDirty) return;
    if (error) showApiError(error, '检索目录刷新失败：');
    if (state.libraryRetryAttempted || state.libraryRetryTimer) return;
    state.libraryRetryAttempted = true;
    state.libraryRetryTimer = window.setTimeout(() => {
      state.libraryRetryTimer = null;
      if (generation !== state.generation || !state.connected || !state.fileCatalogDirty) return;
      scheduleLibraryRefresh().catch(() => {});
    }, 3500);
  }

  async function loadLibraries({ quiet = false } = {}) {
    if (!state.connected) return null;
    const generation = state.generation;
    const revision = ++state.libraryLoadRevision;
    const { body } = await api('/api/admin/libraries');
    if (generation !== state.generation || revision !== state.libraryLoadRevision || !state.connected) return null;
    const nextLibraries = Array.isArray(body?.libraries) ? body.libraries : [];
    const selectedLibraryId = $('#search-library').value;
    const selectedSourceKey = $('#search-source').value;
    const selectedDocumentId = $('#search-document').value;
    const nextSelected = nextLibraries.find((item) => item.library_id === selectedLibraryId);
    const nextSource = (nextSelected?.sources || []).find((source) => sourceKey(source) === selectedSourceKey);
    const selectedSourceStillVisible = !selectedSourceKey || Boolean(nextSource && sourceSearchable(nextSource));
    const selectedDocumentStillVisible = !selectedDocumentId || (nextSource?.documents || []).some((doc) => doc.document_id === selectedDocumentId && doc.searchable !== false && doc.readable !== false);
    const lastSearchTarget = state.lastSearchTarget;
    const lastTargetLibrary = nextLibraries.find((item) => item.library_id === lastSearchTarget?.library_id);
    const lastTargetSource = (lastTargetLibrary?.sources || []).find((source) => sourceKey(source) === lastSearchTarget?.source_key);
    const lastSearchStillVisible = !lastSearchTarget || Boolean(lastTargetSource && sourceSearchable(lastTargetSource)
      && (!lastSearchTarget.document_id || (lastTargetSource.documents || []).some((doc) => doc.document_id === lastSearchTarget.document_id && doc.searchable !== false && doc.readable !== false)));
    const selectionInvalid = Boolean(selectedLibraryId && (!nextSelected || !selectedSourceStillVisible || !selectedDocumentStillVisible));
    if (selectionInvalid || !lastSearchStillVisible) {
      state.searchRevision += 1;
      state.lastSearch = null;
      state.lastSearchLibraryId = null;
      state.lastSearchTarget = null;
      state.searchTarget = null;
      $('#results-meta').replaceChildren();
      $('#results-meta').classList.add('hidden');
      $('#evidence-list').replaceChildren(evidenceEmpty('资料库授权已变化', '旧选择或证据已清除，请重新选择当前可访问的资料库。'));
      $('#evidence-state').textContent = '资料库授权已变化';
      $('#evidence-state').className = 'evidence-state';
      if (selectionInvalid) {
        if (!nextSelected) $('#search-library').value = '';
        $('#search-source').value = '';
        $('#search-document').value = '';
      }
    }
    state.libraries = nextLibraries;
    renderReleaseLibraryOptions();
    renderAll();
    if (!quiet) toast(`已读取 ${state.libraries.length} 个资料库分组`);
    return state.libraries;
  }

  function renderReleaseLibraryOptions() {
    const select = $('#release-library');
    if (!select) return;
    const libraries = state.releaseLibraries;
    if (!libraries.some((item) => item.library_id === state.releaseLibraryId)) {
      state.releaseLibraryId = libraries[0]?.library_id || '';
    }
    select.replaceChildren(node('option', '', libraries.length ? '选择资料库' : '没有共享候选资料库'));
    select.options[0].value = '';
    for (const library of libraries) {
      const libraryName = text(library.library);
      const option = node('option', '', libraryName);
      option.value = library.library_id;
      option.title = `资料库编号：${library.library_id}`;
      select.append(option);
    }
    select.value = state.releaseLibraryId;
  }

  async function loadReleaseState() {
    if (!state.connected) return;
    const generation = state.generation;
    const revision = ++state.releaseLoadRevision;
    const targetResult = await api('/api/admin/release-targets');
    if (generation !== state.generation || revision !== state.releaseLoadRevision || !state.connected) return;
    state.releaseTargets = Array.isArray(targetResult.body?.targets) ? targetResult.body.targets : [];
    state.releaseLibraries = Array.isArray(targetResult.body?.libraries) ? targetResult.body.libraries : [];
    renderReleaseLibraryOptions();
    const libraryId = state.releaseLibraryId;
    if (!libraryId) { state.releaseSnapshot = null; renderReleasePage(); return; }
    const releaseResult = await api(`/api/admin/libraries/${encodeURIComponent(libraryId)}/releases`);
    if (generation !== state.generation || revision !== state.releaseLoadRevision || libraryId !== state.releaseLibraryId || !state.connected) return;
    state.releaseSnapshot = releaseResult.body || null;
    renderReleasePage();
  }

  function renderReleasePage() {
    const target = $('#release-target');
    if (!target) return;
    const previousTargetId = target.value;
    const targets = state.releaseTargets.filter((item) => item.library_id === state.releaseLibraryId);
    target.replaceChildren(node('option', '', targets.length ? '选择候选索引' : '没有登记的候选索引'));
    target.options[0].value = '';
    for (const item of targets) {
      const option = node('option', '', `资料版本 ${item.version} · ${formatNumber(item.chunk_count)} 个检索片段`);
      option.value = item.target_id;
      option.title = `索引编号：${item.target_id}`;
      target.append(option);
    }
    if (targets.some((item) => item.target_id === previousTargetId)) target.value = previousTargetId;
    const snapshot = state.releaseSnapshot;
    $('#release-active-title').textContent = snapshot?.active?.release_id ? '当前正在使用的版本' : '当前没有已切换版本';
    $('#release-active-detail').textContent = snapshot?.active
      ? `资料版本 ${snapshot.active.target?.version || '信息未记录'} · 更新于 ${formatDate(snapshot.active.activated_at || snapshot.active.created_at)}`
      : '新候选经过校验并显式激活后，才会成为普通检索使用的版本。';
    $('#release-active-technical').textContent = snapshot?.active
      ? `索引编号：${snapshot.active.target?.target_id || '未记录'} · 版本记录编号：${snapshot.active.release_id} · 状态版本号：${snapshot.revision}`
      : `状态版本号：${snapshot?.revision ?? '未记录'}`;
    $('#release-revision').textContent = `状态版本号 ${snapshot?.revision ?? '—'}`;
    const actions = $('#release-actions');
    actions.replaceChildren();
    const rollback = node('button', 'button button-secondary', '回退到上一版本');
    rollback.type = 'button';
    rollback.disabled = !snapshot?.history || snapshot.history.length < 2;
    rollback.addEventListener('click', () => rollbackRelease());
    actions.append(rollback);
    const history = $('#release-history');
    history.replaceChildren();
    if (!snapshot?.releases?.length) {
      history.append(node('div', 'table-placeholder', '暂无历史版本。准备候选不会自动切换当前使用版本。'));
      return;
    }
    const table = node('table', 'data-table');
    const head = node('thead'); const header = node('tr');
    for (const label of ['资料版本', '状态', '准备时间', '操作']) header.append(node('th', '', label));
    head.append(header); table.append(head);
    const body = node('tbody');
    for (const release of snapshot.releases) {
      const row = node('tr');
      const statusCell = node('td'); statusCell.append(badge(release.status));
      const versionCell = node('td');
      versionCell.append(node('span', '', release.target?.version ? `版本 ${release.target.version}` : '版本信息未记录'));
      const identifiers = node('details', 'release-technical-details');
      identifiers.append(node('summary', '', '技术详情'), node('small', '', `版本记录：${release.release_id} · 索引：${release.target?.target_id || release.source_target_id || '未记录'}`));
      versionCell.append(identifiers);
      row.append(versionCell, statusCell, node('td', '', formatDate(release.created_at)));
      const cell = node('td', 'align-right'); const controls = node('div', 'row-actions');
      const validate = node('button', 'button button-secondary', '校验'); validate.type = 'button';
      validate.addEventListener('click', () => validateRelease(release.release_id)); controls.append(validate);
      if (release.status === 'ready' && release.release_id !== snapshot.active_release_id) {
        const activate = node('button', 'button button-primary', '切换使用'); activate.type = 'button';
        activate.addEventListener('click', () => activateRelease(release.release_id)); controls.append(activate);
      }
      cell.append(controls); row.append(cell); body.append(row);
    }
    table.append(body); history.append(table);
  }

  async function validateRelease(releaseId) {
    try {
      const { body } = await api(`/api/admin/releases/${encodeURIComponent(releaseId)}/validate`, { method: 'POST' });
      toast(body?.valid ? '版本完整性检查通过' : '版本完整性检查未通过', body?.valid ? 'success' : 'error');
    } catch (error) { showApiError(error, '检查版本失败：'); }
  }

  async function activateRelease(releaseId) {
    const snapshot = state.releaseSnapshot;
    const reason = $('#release-reason').value.trim();
    if (!snapshot || !reason) { toast('请填写本次激活原因', 'error'); return; }
    try {
      await api(`/api/admin/libraries/${encodeURIComponent(state.releaseLibraryId)}/activate`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ release_id: releaseId, expected_revision: snapshot.revision, reason }),
      });
      toast('当前使用的资料库版本已切换');
      await loadReleaseState();
      await loadLibraries({ quiet: true });
    } catch (error) { showApiError(error, '激活失败：'); }
  }

  async function rollbackRelease() {
    const snapshot = state.releaseSnapshot;
    const reason = $('#release-reason').value.trim();
    if (!snapshot || !reason) { toast('请填写本次回退原因', 'error'); return; }
    try {
      await api(`/api/admin/libraries/${encodeURIComponent(state.releaseLibraryId)}/rollback`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ expected_revision: snapshot.revision, reason }),
      });
      toast('资料库已回退到上一版本');
      await loadReleaseState();
      await loadLibraries({ quiet: true });
    } catch (error) { showApiError(error, '回退失败：'); }
  }

  async function prepareRelease(event) {
    event.preventDefault();
    const libraryId = $('#release-library').value;
    const targetId = $('#release-target').value;
    const reason = $('#release-reason').value.trim();
    if (!libraryId || !targetId || !reason) { toast('请选择资料库、已建好的候选索引并填写原因', 'error'); return; }
    const button = $('#release-prepare-submit'); button.disabled = true;
    try {
      const { body } = await api('/api/admin/releases/prepare', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ library_id: libraryId, target_id: targetId, reason }),
      });
      state.releaseJob = body?.job || null;
      renderReleaseJob();
      toast('候选版本准备任务已提交');
      if (state.releaseJob?.job_id) pollReleaseJob(state.releaseJob.job_id, state.generation);
    } catch (error) { showApiError(error, '提交版本准备任务失败：'); }
    finally { button.disabled = false; }
  }

  function renderReleaseJob() {
    const container = $('#release-job-state');
    if (!container) return;
    const job = state.releaseJob;
    container.replaceChildren();
    if (!job) { container.textContent = '暂无版本准备任务'; return; }
    const stageName = phaseLabel(job.stage);
    const title = node('strong', '', `${statusLabel(job.status)} · ${stageName}`);
    const detail = node('p', '', `索引编号 ${job.target_id} · ${Math.max(0, Math.min(100, Number(job.progress) || 0))}%`);
    append(container, title, detail);
    if (job.error?.type) container.append(node('p', 'error-text', '候选准备未完成；当前使用版本没有切换。'));
    if (job.error?.detail) container.append(node('p', 'error-text', job.error.detail));
    if (job.result?.release_id) container.append(node('p', '', `新版本已准备，尚未切换使用。版本记录编号：${job.result.release_id}`));
  }

  async function pollReleaseJob(jobId, generation) {
    try {
      const { body } = await api(`/api/admin/release-jobs/${encodeURIComponent(jobId)}`);
      if (generation !== state.generation || state.releaseJob?.job_id !== jobId) return;
      state.releaseJob = body;
      renderReleaseJob();
      if (body.status === 'ready' || body.status === 'failed') {
        if (body.status === 'ready') await loadReleaseState();
        return;
      }
      state.releasePollTimer = window.setTimeout(() => pollReleaseJob(jobId, generation), 1000);
    } catch (error) { showApiError(error, '读取版本准备进度失败：'); }
  }

  async function loadCalls({ offset = state.callOffset, quiet = false } = {}) {
    if (!state.connected) return [];
    const revision = ++state.callRevision;
    const params = new URLSearchParams({
      type: state.callFilters.type,
      status: state.callFilters.status,
      limit: String(state.callLimit),
      offset: String(Math.max(0, offset)),
    });
    if (state.callFilters.target) params.set('target', state.callFilters.target);
    if (state.callFilters.retrieval_target) params.set('retrieval_target', state.callFilters.retrieval_target);
    const { body } = await api(`/api/admin/calls?${params.toString()}`);
    if (revision !== state.callRevision || !state.connected) return [];
    state.calls = Array.isArray(body?.items) ? body.items : [];
    state.callCoverage = body?.coverage || null;
    state.callTotal = Number.isFinite(Number(body?.total)) ? Number(body.total) : 0;
    state.callOffset = Number.isFinite(Number(body?.offset)) ? Number(body.offset) : Math.max(0, offset);
    state.callHasNext = state.callOffset + state.calls.length < state.callTotal;
    renderCalls();
    if (!quiet) toast(`已读取 ${state.calls.length} 条调用日志`);
    return state.calls;
  }

  function renderCallCoverage(coverage) {
    const http = coverage?.http || null;
    const mcp = coverage?.mcp || null;
    const isAvailable = (entry) => entry && ['active', 'available'].includes(entry.status);
    $('#http-coverage-copy').textContent = http ? text(http.message, http.status) : '连接后读取覆盖状态';
    $('#mcp-coverage-copy').textContent = mcp
      ? [text(mcp.message, mcp.status), mcp.source ? `来源：${mcp.source}` : '', Number(mcp.malformed_lines) ? `格式异常 ${mcp.malformed_lines} 行` : '', mcp.truncated ? '日志已截断' : ''].filter(Boolean).join(' · ')
      : '连接后读取覆盖状态';
    $('#http-coverage-dot').className = `coverage-dot ${isAvailable(http) ? 'available' : http ? 'unavailable' : ''}`;
    $('#mcp-coverage-dot').className = `coverage-dot ${isAvailable(mcp) ? 'available' : mcp ? 'unavailable' : ''}`;
    const allAvailable = isAvailable(http) && isAvailable(mcp);
    $('#calls-coverage-state').textContent = !coverage ? '等待读取' : allAvailable ? '覆盖状态可用' : '部分记录不可用';
    $('#calls-coverage-state').className = `coverage-state ${coverage ? allAvailable ? 'available' : 'unavailable' : ''}`;
  }

  function renderCalls() {
    renderCallCoverage(state.callCoverage);
    const rows = $('#call-rows');
    const hasRecords = state.calls.length > 0;
    $('#calls-empty').classList.toggle('hidden', hasRecords || !state.connected);
    if (!state.connected) { resetRows(rows, '连接管理服务后读取调用日志', 8); }
    else if (!hasRecords) { resetRows(rows, null, 8); }
    else {
      rows.replaceChildren();
      for (const call of state.calls) {
        const row = node('tr');
        const result = node('td');
        const success = call.status === 'success';
        result.append(node('span', `status-badge ${success ? 'status-active' : 'status-failed'}`, success ? '成功' : '失败'));
        const route = text(call.target);
      const retrievalTarget = text(call.retrieval_target, call.type === 'mcp' && call.target === 'search_docs' ? 'unknown' : '—');
        const cells = [
          node('td', '', formatDate(call.occurred_at)),
          node('td', '', text(call.type).toUpperCase()),
          node('td', 'call-route', route),
          node('td', 'call-retrieval-target', retrievalTarget),
          node('td', '', call.duration_ms === null || call.duration_ms === undefined ? '—' : `${formatNumber(call.duration_ms)} ms`),
          result,
          node('td', '', [call.status_code, call.error_code].filter((value) => value !== null && value !== undefined && value !== '').join(' · ') || '—'),
          node('td', 'job-id', text(call.request_id)),
        ];
        row.append(...cells);
        rows.append(row);
      }
    }
    $('#call-count').textContent = state.connected ? `${formatNumber(state.calls.length)} / ${formatNumber(state.callTotal)} 条` : '—';
    const start = state.calls.length ? state.callOffset + 1 : 0;
    const end = state.callOffset + state.calls.length;
    $('#call-page-note').textContent = state.connected ? (state.calls.length ? `第 ${formatNumber(start)}–${formatNumber(end)} 条 · 每页 ${state.callLimit} 条` : `共 ${formatNumber(state.callTotal)} 条匹配记录`) : '等待连接管理服务';
    $('#calls-prev').disabled = !state.connected || state.callOffset <= 0 || state.busy.has('calls-page');
    $('#calls-next').disabled = !state.connected || !state.callHasNext || state.busy.has('calls-page');
  }

  async function changeCallPage(delta) {
    const offset = Math.max(0, state.callOffset + delta * state.callLimit);
    if (offset === state.callOffset || state.busy.has('calls-page')) return;
    setBusy('calls-page', true);
    try { await loadCalls({ offset }); }
    catch (error) { showApiError(error, '读取调用日志失败：'); }
    finally { setBusy('calls-page', false); renderCalls(); }
  }

  function callFiltersChanged() {
    state.callFilters = {
      type: $('#call-type-filter').value,
      status: $('#call-status-filter').value,
      target: $('#call-target-filter').value.trim(),
      retrieval_target: $('#call-retrieval-filter').value.trim(),
    };
    state.callOffset = 0;
    if (state.connected) loadCalls({ offset: 0 }).catch((error) => showApiError(error, '读取调用日志失败：'));
  }

  function renderSearchLibraries() {
    const select = $('#search-library');
    const previous = select.value;
    select.replaceChildren();
    const placeholder = node('option', '', state.connected ? '选择资料库' : '连接后加载当前可用资料库');
    placeholder.value = ''; select.append(placeholder);
    for (const library of state.libraries) {
      const option = node('option', '', `${text(library.library)} · ${library.sources?.reduce((n, source) => n + (source.documents?.length || 0), 0) || 0} 份`);
      option.value = library.library_id; select.append(option);
    }
    if (state.libraries.some((library) => library.library_id === previous)) select.value = previous;
    renderSearchSources();
  }

  function selectedLibrary() { return state.libraries.find((library) => library.library_id === $('#search-library').value) || null; }
  function selectedSearchSource() {
    const library = selectedLibrary();
    const key = $('#search-source').value;
    return library?.sources?.find((source) => sourceKey(source) === key) || null;
  }

  function renderSearchSources() {
    const library = selectedLibrary();
    const select = $('#search-source');
    const previous = select.value;
    select.replaceChildren();
    const sources = (library?.sources || []).filter((source) => sourceSearchable(source) && (source.documents || []).some((doc) => doc.searchable !== false && doc.readable !== false))
      .sort((left, right) => Number(right.is_active === true) - Number(left.is_active === true));
    const option = node('option', '', library ? sources.length ? '选择来源与版本' : '该资料库当前没有可检索版本' : '先选择资料库'); option.value = ''; select.append(option);
    for (const source of sources) {
      const label = source.source_type === 'shared'
        ? `${sourceDisplayName(source)} · 版本 ${text(source.version)}${source.is_active ? ' · 当前使用' : ''}`
        : `受管文件 · ${fileLabel(state.files.find((file) => file.file_id === source.file_id) || { display_name: source.display_name || '名称未记录' })} · ${text(source.version)}`;
      const item = node('option', '', label);
      item.value = sourceKey(source);
      item.title = `corpus_id：${text(source.corpus_id)} · file_id：${text(source.file_id)} · index_target_id：${text(source.index_target_id)}`;
      select.append(item);
    }
    select.disabled = !library || !sources.length;
    if (sources.some((source) => sourceKey(source) === previous)) select.value = previous;
    else if (sources.length) select.value = sourceKey(sources[0]);
    renderSearchDocuments();
  }

  function renderSearchDocuments() {
    const source = selectedSearchSource();
    const select = $('#search-document');
    const previous = select.value;
    select.replaceChildren();
    const option = node('option', '', source ? '检索所选版本中的全部文档' : '先选择资料版本'); option.value = ''; select.append(option);
    for (const doc of source?.documents || []) {
      if (doc.readable === false || doc.searchable === false) continue;
      const item = node('option', '', text(doc.display_name, doc.document_id)); item.value = doc.document_id; select.append(item);
    }
    select.disabled = !source || (source.documents || []).filter((doc) => doc.readable !== false && doc.searchable !== false).length === 0;
    if ([...select.options].some((item) => item.value === previous)) select.value = previous;
  }

  function renderUploadLibraries() {
    const select = $('#upload-library');
    const previous = state.uploadLibraryId || select.value;
    select.replaceChildren();
    const placeholder = node('option', '', state.connected ? '选择新文件归属资料库' : '连接后读取资料库'); placeholder.value = ''; select.append(placeholder);
    const options = new Map();
    for (const library of state.libraries) options.set(library.library_id, library.library || library.library_id);
    for (const library of state.managedLibraries) options.set(library.library_id, library.display_name || library.library_id);
    for (const [libraryId, libraryName] of options) {
      const option = node('option', '', libraryName); option.value = libraryId;
      option.title = `资料库编号：${libraryId}`;
      select.append(option);
    }
    if ([...select.options].some((option) => option.value === previous)) select.value = previous;
  }

  async function loadManagedLibraries() {
    if (!state.connected) return [];
    const generation = state.generation;
    const { body } = await api('/api/admin/managed-libraries');
    if (generation !== state.generation || !state.connected) return state.managedLibraries;
    state.managedLibraries = Array.isArray(body?.libraries) ? body.libraries : [];
    renderUploadLibraries();
    return state.managedLibraries;
  }

  function openSearchTarget(target) {
    if (!target) return;
    state.searchTarget = target;
    navigate('search');
    $('#search-library').value = target.library_id || '';
    renderSearchSources();
    const selected = (selectedLibrary()?.sources || []).find((source) => source.source_type === target.source_type
      && (target.corpus_id ? source.corpus_id === target.corpus_id : source.file_id === target.file_id)
      && (!target.index_target_id || source.index_target_id === target.index_target_id));
    if (selected) {
      $('#search-source').value = sourceKey(selected);
      renderSearchDocuments();
      $('#search-document').value = target.document_id || '';
      $('#search-query').focus();
    }
  }

  async function loadJobs({ offset = state.jobOffset, quiet = false } = {}) {
    if (!state.connected) return [];
    const { body } = await api(`/api/jobs?limit=${LIMIT}&offset=${Math.max(0, offset)}`);
    state.jobOffset = Number.isFinite(Number(body?.offset)) ? Number(body.offset) : Math.max(0, offset);
    state.jobs = Array.isArray(body?.jobs) ? body.jobs : [];
    state.jobHasNext = state.jobs.length === LIMIT;
    state.connected = true;
    // 任务完成可能刚刚切换版本或删除文件，刷新清单后再更新可用操作。
    await loadFiles({ quiet: true });
    renderAll();
    if (!quiet && !state.pollTimer) toast(`已读取 ${state.jobs.length} 条任务记录`);
    updatePolling();
    return state.jobs;
  }

  async function refreshAll({ quiet = false } = {}) {
    try {
      if (window.WenjinIdentity?.mode === 'accounts' && window.WenjinIdentity.user?.role === 'reader') {
        state.connected = true;
        await loadLibraries({ quiet: true });
        setConnection('connected');
        navigate('search');
        renderAll();
        if (!state.libraries.length) $('#identity-empty-grants').classList.remove('hidden');
        return true;
      }
      await api('/api/admin/config').then(({ body }) => { state.config = body || null; });
      state.connected = true;
      await Promise.all([
        loadFiles({ quiet: true }),
        loadJobs({ offset: state.jobOffset, quiet: true }),
        loadLibraries({ quiet: true }),
        loadManagedLibraries(),
      ]);
      state.connected = true;
      setConnection('connected');
      if (!state.lastSearch && !state.busy.has('search')) {
        $('#evidence-list').replaceChildren(evidenceEmpty('等待检索', '选择资料库和版本，输入问题后查看证据'));
      }
      $('#banner-title').textContent = '管理服务已连接';
      $('#banner-message').textContent = '';
      renderAll();
      if (!quiet) toast('管理服务连接正常');
      return true;
    } catch (error) {
      if (error?.status === 0 || error?.status === 401) return false;
      clearSensitiveState();
      if (error?.status !== 401) {
        state.connected = false;
        renderAll();
        setConnection('error', error.message || '请确认管理服务已启动。');
        $('#banner-title').textContent = '无法读取管理服务';
        if (!quiet) showApiError(error);
      }
      return false;
    }
  }

  async function connectWithToken(token) {
    clearSensitiveState({ disconnected: true });
    state.token = token || null;
    $('#connect-button').disabled = true;
    $('#connect-button').textContent = '正在验证…';
    try {
      const connected = await refreshAll({ quiet: true });
      if (connected) {
        $('#admin-token').value = '';
        $('#auth-dialog').close();
      }
    } finally {
      $('#connect-button').disabled = false;
      $('#connect-button').textContent = '连接服务';
    }
  }

  async function switchAccountOrSignOut() {
    const identity = window.WenjinIdentity;
    if (!identity?.user || state.busy.has('identity-session-change')) return;
    state.busy.add('identity-session-change');
    $('#account-switch-account').disabled = true;
    $('#account-logout').disabled = true;
    $('#account-menu').open = false;
    try { await identity.signOut(); }
    catch (error) { if (error?.status !== 0) toast(`账号操作失败：${error.message}`, 'error'); }
    finally {
      state.busy.delete('identity-session-change');
      $('#account-switch-account').disabled = false;
      $('#account-logout').disabled = false;
      renderAccountMenu();
    }
  }

  function disconnectLegacyConnection() {
    if (window.WenjinIdentity?.mode === 'accounts') return;
    $('#account-menu').open = false;
    $('#auth-dialog').close();
    clearSensitiveState({ disconnected: true });
    toast('管理连接已断开，页面数据和 Bearer token 已清除');
  }

  function openConnectionSettings() {
    $('#account-menu').open = false;
    $('#auth-dialog').showModal();
  }

  function updatePolling() {
    const hasActive = state.jobs.some((job) => ACTIVE_JOBS.has(job.status));
    if (hasActive && !state.pollTimer) {
      state.pollTimer = window.setInterval(() => { loadJobs({ quiet: true }).catch((error) => showApiError(error)); }, 3500);
    } else if (!hasActive && state.pollTimer) {
      window.clearInterval(state.pollTimer);
      state.pollTimer = null;
    }
  }

  async function changeJobPage(delta) {
    const nextOffset = Math.max(0, state.jobOffset + delta * LIMIT);
    if (nextOffset === state.jobOffset || state.busy.has('jobs-page')) return;
    setBusy('jobs-page', true);
    try { await loadJobs({ offset: nextOffset }); }
    catch (error) { showApiError(error); }
    finally { setBusy('jobs-page', false); renderJobs(); }
  }

  async function lookupJob() {
    const id = $('#job-id-lookup').value.trim();
    if (!id) { toast('请输入完整 job_id', 'error'); return; }
    setBusy('job-lookup', true);
    $('#lookup-job').disabled = true;
    try { await openJobDetail(id); }
    catch (error) { showApiError(error, '任务查询失败：'); }
    finally { $('#lookup-job').disabled = false; setBusy('job-lookup', false); }
  }

  function detailField(label, value) {
    const field = node('div', 'detail-field');
    return append(field, node('small', '', label), node('strong', '', text(value)));
  }

  function appendSection(parent, title) {
    const section = node('section', 'detail-section');
    section.append(node('h3', '', title));
    parent.append(section);
    return section;
  }

  function safeJsonPairs(value, parent, depth = 0) {
    if (depth > 2 || value === null || value === undefined) return;
    const pairs = node('div', 'json-pairs');
    const entries = Array.isArray(value) ? value.map((entry, index) => [String(index), entry]) : Object.entries(value);
    for (const [key, raw] of entries) {
      const row = node('div', 'json-pair');
      let display;
      if (raw && typeof raw === 'object') display = Array.isArray(raw) ? raw.map((item) => typeof item === 'object' ? '[对象]' : String(item)).join('、') : Object.keys(raw).length ? `${Object.keys(raw).length} 项` : '空';
      else display = text(raw, '—');
      append(row, node('span', '', key), node('span', '', display));
      pairs.append(row);
    }
    parent.append(pairs);
    if (depth === 0 && !Array.isArray(value)) {
      const disclosure = node('details', 'resource-details');
      const summary = node('summary', '', '展开完整资源 JSON');
      const raw = node('pre', 'resource-json');
      raw.textContent = JSON.stringify(value, null, 2);
      append(disclosure, summary, raw);
      parent.append(disclosure);
    }
  }

  async function openFileDetail(fileId) {
    const file = state.files.find((entry) => entry.file_id === fileId);
    if (!file) return;
    const revision = ++state.detailRevision;
    state.selectedFile = fileId;
    state.jobLookup = null;
    const content = $('#detail-content');
    content.replaceChildren();
    const head = node('header', 'detail-head');
    append(head, node('span', 'section-kicker', isManaged(file) ? '受管资料' : '历史资料清单'), node('h2', '', fileLabel(file)));
    const tags = node('div', 'detail-tags'); tags.append(badge(file.status));
    tags.append(node('span', 'meta-chip', `版本 ${text(file.version)}`));
    tags.append(node('span', 'meta-chip', `${formatNumber(file.chunk_count)} 个片段`));
    append(head, tags, node('p', '', isManaged(file) ? '文件管理器登记的受管文件。替换和删除作用于该文件的全部受管版本与对应索引资源。' : '此条目来自历史共享资料清单，原始文件及旧索引仍由原资料库管理。创建可管理副本会复制原文并建立独立受管索引，不修改旧库，也不会自动授予读取权限；只有需要单独替换或删除时才使用。'));
    const actions = node('div', 'detail-actions');
    if (isManaged(file)) {
      const canReadActive = file.status === 'active' && Boolean(file.active_version_id);
      const canReplace = !['importing', 'deleting'].includes(file.status);
      actions.append(actionButton('下载原文', 'download', file.file_id, false, !canReadActive));
      actions.append(actionButton('替换版本', 'replace', file.file_id, false, !canReplace));
      actions.append(actionButton('检索调试', 'search', file.file_id, false, !canReadActive));
      actions.append(actionButton('删除受管文件', 'delete', file.file_id, true, file.status === 'deleting'));
    } else actions.append(actionButton('创建可管理副本', 'adopt', file.file_id));
    const identity = appendSection(content, '文件身份');
    const grid = node('div', 'detail-grid');
    append(grid, detailField('资料库名称', file.library_name || file.library || '名称未记录'), detailField('资料显示名称', file.display_name), detailField('原文件名', file.original_filename || '名称未记录'), detailField('file_id', file.file_id), detailField('library_id', file.library_id), detailField('active version_id', file.active_version_id), detailField('最近更新', file.updated_at ? formatDate(file.updated_at) : '历史条目'));
    identity.append(grid);
    content.append(head, actions, identity);
    if (file.latest_job_id) {
      const latest = appendSection(content, '最近任务');
      const job = state.jobs.find((entry) => entry.job_id === file.latest_job_id);
      const open = node('button', 'table-action', `${statusLabel(file.latest_job_status)} · ${text(file.latest_job_phase)} · ${text(file.latest_job_progress, 0)}% · 查看 ${file.latest_job_id}`);
      open.type = 'button'; open.addEventListener('click', () => openJobDetail(file.latest_job_id));
      latest.append(open);
      if (job?.reason) latest.append(node('p', '', job.reason));
    }
    if (!$('#detail-dialog').open) $('#detail-dialog').showModal();
    if (isManaged(file) && file.status === 'active') {
      try {
        const { body } = await api(`/api/files/${escPath(fileId)}/resources`);
        if (state.selectedFile !== fileId || state.detailRevision !== revision || !$('#detail-dialog').open) return;
        const section = appendSection(content, '索引与原文对账');
        for (const [label, key] of [['原文存在', 'source_exists'], ['原文 SHA256 匹配', 'source_sha256_matches'], ['Manifest 存在', 'manifest_exists'], ['索引资源数量一致', 'consistent']]) {
          const row = node('div', 'resource-check');
          const ok = body?.[key] === true;
          append(row, node('span', '', ok ? '✓' : '•'), node('span', '', label), node('b', '', ok ? '通过' : '未确认'));
          row.classList.add(ok ? 'resource-ok' : 'resource-bad'); section.append(row);
        }
        const details = node('div', 'detail-grid');
        append(details, detailField('BM25 行数', formatNumber(body?.bm25_rows)), detailField('Qdrant 点数', formatNumber(body?.dense_points)), detailField('managed 片段数', formatNumber(body?.chunk_count)), detailField('索引 collection', body?.dense_collection));
        section.append(details);
      } catch (error) { if (state.selectedFile === fileId) toast(`读取资源对账失败：${error.message}`, 'error'); }
    }
  }

  async function openJobDetail(jobId) {
    const revision = ++state.detailRevision;
    state.selectedFile = null;
    state.jobLookup = jobId;
    let body;
    try { ({ body } = await api(`/api/jobs/${escPath(jobId)}`)); }
    catch (error) { showApiError(error, '读取任务失败：'); return; }
    if (state.jobLookup !== jobId || state.detailRevision !== revision || !state.connected) return;
    const job = body || {};
    const content = $('#detail-content'); content.replaceChildren();
    const head = node('header', 'detail-head');
    append(head, node('span', 'section-kicker', '任务详情'), node('h2', '', job.file_display_name || '名称未记录'),
      node('p', 'job-readable-context', `${job.library_display_name || '资料库名称未记录'} · ${job.version || '版本信息未记录'} · ${job.original_filename || '原文件名未记录'}`),
      node('span', 'detail-kind', job.kind === 'delete' ? '文件清理任务' : '资料导入任务'));
    const tags = node('div', 'detail-tags'); tags.append(badge(job.status)); tags.append(node('span', 'meta-chip', `${phaseLabel(job.phase)} · ${Math.max(0, Math.min(100, Number(job.progress) || 0))}%`));
    append(head, tags, node('p', 'job-id', job.job_id));
    const gridSection = appendSection(content, '任务状态');
    const grid = node('div', 'detail-grid');
    append(grid, detailField('资料库名称', job.library_display_name || '名称未记录'), detailField('资料名称', job.file_display_name || '名称未记录'), detailField('版本', job.version || '版本信息未记录'), detailField('原文件名', job.original_filename || '名称未记录'), detailField('job_id', job.job_id), detailField('file_id', job.file_id), detailField('library_id', job.library_id), detailField('version_id', job.version_id), detailField('重试次数', job.retry_count), detailField('创建时间', formatDate(job.created_at)), detailField('更新时间', formatDate(job.updated_at)), detailField('仅支持重试', job.retryable === true ? '是' : '否'), detailField('进度', `${Math.max(0, Math.min(100, Number(job.progress) || 0))}%`));
    gridSection.append(grid);
    content.append(head, gridSection);
    if (job.reason) { const section = appendSection(content, '任务说明'); section.append(node('p', '', job.reason)); }
    if (job.resources && Object.keys(job.resources).length) { const section = appendSection(content, '资源记录'); safeJsonPairs(job.resources, section); }
    const actions = node('div', 'detail-actions');
    if (job.status === 'failed' && job.retryable === true) {
      const retry = node('button', 'button button-primary', '重试此任务'); retry.type = 'button';
      retry.dataset.busy = `retry-${job.job_id}`;
      retry.disabled = state.busy.has(`retry-${job.job_id}`);
      retry.addEventListener('click', () => retryJob(job)); actions.append(retry);
    }
    content.append(actions);
    if (!$('#detail-dialog').open) $('#detail-dialog').showModal();
    if (ACTIVE_JOBS.has(job.status)) pollSingleJob(job.job_id, revision);
  }

  async function pollSingleJob(jobId, revision) {
    for (let i = 0; i < 60 && $('#detail-dialog').open && state.jobLookup === jobId && state.detailRevision === revision; i += 1) {
      await new Promise((resolve) => window.setTimeout(resolve, 2200));
      if (!$('#detail-dialog').open || state.jobLookup !== jobId || state.detailRevision !== revision || !state.connected) return;
      try {
        const { body } = await api(`/api/jobs/${escPath(jobId)}`);
        if (!$('#detail-dialog').open || state.jobLookup !== jobId || state.detailRevision !== revision) return;
        const index = state.jobs.findIndex((job) => job.job_id === jobId);
        if (index >= 0) state.jobs[index] = body;
        renderAll();
        if (!ACTIVE_JOBS.has(body.status)) { await openJobDetail(jobId); return; }
        // 打开同一任务会刷新详情，保证展示的是服务端刚返回的状态。
        await renderJobDetailInPlace(body);
      } catch (error) { showApiError(error); return; }
    }
  }

  async function renderJobDetailInPlace(job) {
    // 同一任务详情仅覆盖内容，不重复启动另一个轮询循环。
    const current = state.jobLookup;
    if (current !== job.job_id || !$('#detail-dialog').open) return;
    const content = $('#detail-content');
    const head = node('header', 'detail-head');
    append(head, node('span', 'section-kicker', '任务详情'), node('h2', '', job.file_display_name || '名称未记录'),
      node('p', 'job-readable-context', `${job.library_display_name || '资料库名称未记录'} · ${job.version || '版本信息未记录'} · ${job.original_filename || '原文件名未记录'}`),
      node('span', 'detail-kind', job.kind === 'delete' ? '文件清理任务' : '资料导入任务'));
    const tags = node('div', 'detail-tags'); tags.append(badge(job.status)); tags.append(node('span', 'meta-chip', `${phaseLabel(job.phase)} · ${Math.max(0, Math.min(100, Number(job.progress) || 0))}%`));
    append(head, tags, node('p', 'job-id', job.job_id));
    const section = node('section', 'detail-section'); section.append(node('h3', '', '任务状态'));
    const grid = node('div', 'detail-grid');
    append(grid, detailField('资料库名称', job.library_display_name || '名称未记录'), detailField('资料名称', job.file_display_name || '名称未记录'), detailField('版本', job.version || '版本信息未记录'), detailField('原文件名', job.original_filename || '名称未记录'), detailField('job_id', job.job_id), detailField('file_id', job.file_id), detailField('library_id', job.library_id), detailField('version_id', job.version_id), detailField('重试次数', job.retry_count), detailField('创建时间', formatDate(job.created_at)), detailField('更新时间', formatDate(job.updated_at)), detailField('仅支持重试', job.retryable === true ? '是' : '否'), detailField('进度', `${Math.max(0, Math.min(100, Number(job.progress) || 0))}%`));
    section.append(grid);
    content.replaceChildren(head, section);
    if (job.reason) { const reason = appendSection(content, '任务说明'); reason.append(node('p', '', job.reason)); }
    if (job.resources && Object.keys(job.resources).length) { const resources = appendSection(content, '资源记录'); safeJsonPairs(job.resources, resources); }
  }

  async function retryJob(job) {
    const key = `retry-${job.job_id}`;
    if (state.busy.has(key)) return;
    if (job.status !== 'failed' || job.retryable !== true) { toast('此任务当前不可重试', 'error'); return; }
    if (!window.confirm(`确认重试任务 ${job.job_id}？\n\n重试会按服务端保存的任务记录继续导入或清理。`)) return;
    setBusy(key, true);
    try {
      const { body } = await api(`/api/jobs/${escPath(job.job_id)}/retry`, { method: 'POST' });
      state.jobLookup = body.job_id;
      await loadJobs({ quiet: true });
      toast(`已提交「${body.file_display_name || '名称未记录'}」的重试任务`);
      await openJobDetail(body.job_id);
    } catch (error) { showApiError(error, '提交重试失败：'); }
    finally { setBusy(key, false); }
  }

  function formatBytes(value) {
    const bytes = Number(value);
    if (!Number.isFinite(bytes) || bytes < 0) return '未知';
    if (bytes < 1024) return `${bytes} B`;
    const units = ['KB', 'MB', 'GB'];
    let amount = bytes / 1024; let index = 0;
    while (amount >= 1024 && index < units.length - 1) { amount /= 1024; index += 1; }
    return `${amount.toFixed(amount >= 10 ? 0 : 1)} ${units[index]}`;
  }

  async function chooseUpload({ replace = null } = {}) {
    $('#create-library-button').disabled = false;
    const operationKey = replace ? `replace-${replace.file_id}` : 'upload';
    if (!state.connected || state.busy.has(operationKey)) return;
    if (!replace) {
      state.uploadReplaceFile = null;
      state.uploadLibraryId = '';
      renderUploadLibraries();
      try { await loadManagedLibraries(); } catch (error) { showApiError(error, '读取受管资料库失败：'); return; }
      $('#upload-library').value = '';
      $('#upload-display-name').value = '';
      $('#upload-dialog').showModal();
      return;
    }
    state.uploadReplaceFile = replace;
    state.uploadLibraryId = replace.library_id || 'managed-files';
    startUploadPicker(replace);
  }

  function startUploadPicker(replace = null) {
    const input = $('#upload-input');
    input.value = '';
    input.onchange = async () => {
      const file = input.files?.[0];
      if (!file) return;
      if (state.config?.max_upload_bytes && file.size > state.config.max_upload_bytes) {
        toast(`文件大小 ${formatBytes(file.size)} 超过服务上限 ${formatBytes(state.config.max_upload_bytes)}`, 'error'); return;
      }
      if (replace && !window.confirm(`确认将「${fileLabel(replace)}」替换为「${file.name}」？\n\n服务会先构建并验证新版本，再切换当前使用版本。失败时仍保留原版本。`)) return;
      // 新建上传必须先选资料库；替换沿用原归属，不改变原文件范围。
      const key = replace ? `replace-${replace.file_id}` : 'upload';
      setBusy(key, true);
      try {
        const displayName = replace ? String(replace.display_name || '').trim() : $('#upload-display-name').value.trim();
        const headers = new Headers({ 'Content-Type': 'application/octet-stream', 'X-File-Name': encodeURIComponent(file.name) });
        if (displayName) headers.set('X-Display-Name', encodeURIComponent(displayName));
        const endpoint = replace ? `/api/files/${escPath(replace.file_id)}` : `/api/files?library_id=${escPath(state.uploadLibraryId)}`;
        const { body } = await api(endpoint, {
          method: replace ? 'PUT' : 'POST', headers, body: file,
        });
        state.jobLookup = body.job_id;
        await Promise.all([loadFiles({ quiet: true }), loadJobs({ offset: 0, quiet: true })]);
        toast(body.status === 'duplicate' ? '内容已登记为重复任务，未创建新索引' : `已提交任务 ${body.job_id}`);
        navigate('jobs');
        await openJobDetail(body.job_id);
      } catch (error) { showApiError(error, replace ? '替换提交失败：' : '上传失败：'); }
      finally { setBusy(key, false); }
    };
    input.click();
  }

  async function deleteFile(file) {
    if (!file || !isManaged(file) || file.status === 'deleting' || state.busy.has(`delete-${file.file_id}`)) return;
    const message = `确认删除「${fileLabel(file)}」？\n\n删除会清理该文件所有受管历史版本、原文副本、manifest、BM25 文件、对应 Qdrant 集合和导入暂存目录。\n\n历史原件、旧共享索引、外部备份和历史实验记录会保留。`;
    if (!window.confirm(message)) return;
    const key = `delete-${file.file_id}`; setBusy(key, true);
    try {
      const { body } = await api(`/api/files/${escPath(file.file_id)}`, { method: 'DELETE' });
      await Promise.all([loadFiles({ quiet: true }), loadJobs({ offset: 0, quiet: true })]);
      toast(`已提交受管清理任务 ${body.job_id}`);
      await openJobDetail(body.job_id);
    } catch (error) { showApiError(error, '删除提交失败：'); }
    finally { setBusy(key, false); }
  }

  async function adoptFile(file) {
    if (!file || isManaged(file) || state.busy.has(`adopt-${file.file_id}`)) return;
    if (!window.confirm(`确认创建「${fileLabel(file)}」的可管理副本？\n\n系统会复制历史原文并建立独立的受管索引。原文件、旧资料库和旧索引保持不变；这不会授予任何账号读取权限。仅在需要单独替换或删除时创建。`)) return;
    const key = `adopt-${file.file_id}`; setBusy(key, true);
    try {
      const { body } = await api(`/api/files/${escPath(file.file_id)}/adopt`, { method: 'POST' });
      await Promise.all([loadFiles({ quiet: true }), loadJobs({ offset: 0, quiet: true })]);
      toast(`已提交接管任务 ${body.job_id}`);
      await openJobDetail(body.job_id);
    } catch (error) { showApiError(error, '创建可管理副本失败：'); }
    finally { setBusy(key, false); }
  }

  async function downloadFile(file) {
    if (!file || !isManaged(file) || file.status !== 'active' || !file.active_version_id || state.busy.has(`download-${file.file_id}`)) return;
    const generation = state.generation;
    const downloadName = file.original_filename || fileLabel(file);
    const key = `download-${file.file_id}`; setBusy(key, true);
    try {
      const { response } = await api(`/api/files/${escPath(file.file_id)}/download?name=${encodeURIComponent(downloadName)}`);
      const blob = await response.blob();
      if (generation !== state.generation) return;
      const objectUrl = URL.createObjectURL(blob);
      const link = node('a');
      link.href = objectUrl;
      link.download = downloadName;
      link.style.display = 'none';
      document.body.append(link); link.click(); link.remove();
      window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
      toast('原文下载已开始');
    } catch (error) { showApiError(error, '下载失败：'); }
    finally { setBusy(key, false); }
  }

  function evidenceEmpty(title, subtitle) {
    const empty = node('div', 'evidence-empty');
    append(empty, node('div', 'evidence-glyph', '⌕'), node('strong', '', title), node('span', '', subtitle));
    return empty;
  }

  function renderEvidence(result) {
    const list = $('#evidence-list'); list.replaceChildren();
    const evidence = Array.isArray(result.evidence) ? result.evidence : [];
    if (!evidence.length) list.append(evidenceEmpty('没有返回证据', '检查文件状态、版本与查询内容后重试'));
    for (const item of evidence) {
      const card = node('article', 'evidence-card');
      const header = node('div', 'evidence-card-head');
      const locator = node('div', 'evidence-locator');
      const page = item.page_number ? ` · 第 ${item.page_number} 页` : '';
      const lines = item.start_line !== null && item.start_line !== undefined ? ` · 行 ${item.start_line}${item.end_line && item.end_line !== item.start_line ? `–${item.end_line}` : ''}` : '';
      append(locator, node('strong', '', text(item.source_path, '未知来源')), node('small', '', `${text(item.version)}${page}${lines}`));
      append(header, node('span', 'rank-pill', `#${text(item.rank)}`), locator);
      if (item.rerank_score !== null && item.rerank_score !== undefined) header.append(node('span', 'score-pill', `重排 ${Number(item.rerank_score).toFixed(3)}`));
      else if (item.rrf_score !== null && item.rrf_score !== undefined) header.append(node('span', 'score-pill', `RRF ${Number(item.rrf_score).toFixed(3)}`));
      const body = node('div', 'evidence-text', item.text || '（服务未返回正文）');
      const meta = node('div', 'evidence-meta');
      for (const [label, value] of [['file_id', item.file_id], ['version_id', item.version_id], ['source SHA', item.source_sha256], ['chunk_id', item.chunk_id], ['parser', item.parser], ['OCR 派生', item.ocr_derived]]) {
        if (value !== undefined && value !== null && value !== '') meta.append(node('span', 'meta-chip', `${label}：${value}`));
      }
      if (item.logical_document_id) meta.append(node('span', 'meta-chip', `logical_document_id：${item.logical_document_id}`));
      const url = safeExternalUrl(item.source_url);
      if (url) {
        const link = node('a', 'evidence-source', '查看来源 ↗');
        link.href = url; link.target = '_blank'; link.rel = 'noopener noreferrer';
        meta.append(link);
      }
      append(card, header, body, meta);
      list.append(card);
    }
    const retrieval = result.retrieval || {};
    const meta = $('#results-meta'); meta.replaceChildren(); meta.classList.remove('hidden');
    const bits = [
      ['资料库', result.catalog_identity?.library_id], ['版本', result.catalog_identity?.version_id || result.version], ['索引 target', result.catalog_identity?.index_target_id || result.index_target_id],
      ['Dense 候选', retrieval.dense_returned], ['BM25 候选', retrieval.bm25_returned],
      ['融合候选', retrieval.fused_candidates], ['已用字符', `${formatNumber(result.context_chars_used)} / ${formatNumber(result.context_char_budget)}`],
      ['reranker', retrieval.reranker_used ? '已使用' : '未使用'], ['答案', '未生成'],
    ];
    for (const [key, value] of bits) {
      const item = node('span'); append(item, node('span', '', `${key} `), node('strong', '', text(value))); meta.append(item);
    }
    $('#evidence-state').textContent = evidence.length ? `${evidence.length} 条证据 · ${evidenceStatusLabel(result.evidence_status)}` : '无证据';
    $('#evidence-state').className = `evidence-state ${evidence.length ? 'has-results' : ''}`;
    $('#results-subtitle').textContent = `查询：${text(result.query)} · 分数只表示排序信号`;
  }

  async function runSearch(event) {
    event.preventDefault();
    const library = selectedLibrary();
    const source = selectedSearchSource();
    const documentId = $('#search-document').value || null;
    const query = $('#search-query').value.trim();
    const topK = Number($('#search-top-k').value);
    const budget = Number($('#search-budget').value);
    if (!library || !source) { toast('请先选择资料库和可检索版本', 'error'); return; }
    if (!query) { toast('请输入检索问题', 'error'); return; }
    if (!sourceSearchable(source) || !(source.documents || []).some((doc) => doc.readable !== false && doc.searchable !== false)) { toast('所选资料尚未发布或当前账号未获读取授权，暂不可检索', 'error'); return; }
    if (documentId && !(source.documents || []).some((doc) => doc.document_id === documentId && doc.readable !== false && doc.searchable !== false)) { toast('限定文档已不属于当前版本或当前账号未获读取授权，请刷新后重选', 'error'); return; }
    const target = { library_id: library.library_id, source_type: source.source_type, corpus_id: source.corpus_id, file_id: source.file_id, version_id: source.version_id, index_target_id: source.index_target_id, document_id: documentId };
    const button = $('#run-search'); button.disabled = true; button.textContent = '正在检索…';
    const generation = state.generation;
    const searchRevision = ++state.searchRevision;
    $('#evidence-state').textContent = '查询中';
    $('#evidence-list').replaceChildren(evidenceEmpty('正在搜索索引', '等待管理服务返回真实证据'));
    try {
      state.lastSearchTarget = {library_id: target.library_id, source_key: sourceKey(source), document_id: documentId};
      state.lastSearchLibraryId = target.library_id;
      const { body } = await api('/api/admin/search', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ library_id: target.library_id, source_type: target.source_type, ...(target.corpus_id ? { corpus_id: target.corpus_id } : {}), ...(target.file_id ? { file_id: target.file_id } : {}), ...(target.index_target_id ? { index_target_id: target.index_target_id } : {}), version_id: target.version_id, ...(documentId ? { document_id: documentId } : {}), query, top_k: topK, context_char_budget: budget }),
      });
      if (generation !== state.generation || searchRevision !== state.searchRevision) return;
      state.lastSearch = body;
      state.lastSearchLibraryId = target.library_id;
      state.lastSearchTarget = {library_id: target.library_id, source_key: sourceKey(source), document_id: documentId};
      renderEvidence(body);
    } catch (error) {
      if (error?.status === 0 || error?.status === 401 || generation !== state.generation || searchRevision !== state.searchRevision) return;
      $('#evidence-state').textContent = '检索失败';
      $('#evidence-state').className = 'evidence-state error';
      if (error?.status === 409 || error?.payload?.error === 'stale_snapshot') {
        state.lastSearch = null;
        $('#results-meta').replaceChildren(); $('#results-meta').classList.add('hidden');
        $('#results-subtitle').textContent = '当前资料库或索引版本已变化';
        $('#evidence-list').replaceChildren(evidenceEmpty('版本已变化', '资料库清单已过期。已尝试刷新，请重新选择当前版本后再检索。'));
        try {
          await Promise.all([loadLibraries({ quiet: true }), loadFiles({ quiet: true })]);
          $('#search-source').value = '';
          renderSearchSources();
        } catch (refreshError) { showApiError(refreshError, '刷新资料库失败：'); }
      } else $('#evidence-list').replaceChildren(evidenceEmpty('请求没有成功', error.message || '请检查查询参数与资料版本'));
      showApiError(error, '检索失败：');
    } finally { button.disabled = false; button.replaceChildren(document.createTextNode('⌕ 运行检索')); }
  }

  function renderEvaluationJobs() {
    const body = $('#evaluation-job-rows');
    if (!state.evaluationJobs.length) {
      body.replaceChildren(node('tr', '', node('td', 'table-placeholder', '当前没有评测任务')));
      return;
    }
    body.replaceChildren(...state.evaluationJobs.map((job) => {
      const row = node('tr');
      const title = node('td');
      append(title, node('strong', '', job.title), node('small', 'job-id', job.job_id));
      const status = node('td');
      append(status, node('strong', '', evaluationStatus(job.status)), node('small', '', evaluationStage(job.stage)));
      const progress = node('td', '', `${job.completed_cases} / ${job.total_cases}`);
      const binding = node('td');
      append(binding, node('span', '', evaluationDatasetLabel(job.input_snapshot?.dataset_id, job.experiment_id)),
        node('small', 'job-id', job.input_snapshot?.dataset_id || job.experiment_id),
        node('small', '', (job.input_snapshot?.profiles || []).map(profileLabel).join(' · ')),
        node('small', '', `操作者：${userLabel(job.owner_user_id)}`));
      const created = node('td', '', new Date(job.created_at).toLocaleString());
      const actions = node('td', 'evaluation-actions');
      if (['queued', 'preparing', 'running', 'cancelling'].includes(job.status)) {
        const cancel = node('button', 'button button-secondary', '取消');
        cancel.type = 'button'; cancel.dataset.evaluationAction = 'cancel'; cancel.dataset.jobId = job.job_id;
        actions.append(cancel);
      }
      if (['interrupted', 'failed'].includes(job.status)) {
        const resume = node('button', 'button button-secondary', '恢复');
        resume.type = 'button'; resume.dataset.evaluationAction = 'resume'; resume.dataset.jobId = job.job_id;
        actions.append(resume);
      }
      if (job.status === 'completed') {
        const view = node('button', 'button button-primary', '查看对照');
        view.type = 'button'; view.dataset.evaluationAction = 'view'; view.dataset.jobId = job.job_id;
        actions.append(view);
      }
      append(row, title, status, progress, binding, created, actions);
      return row;
    }));
  }

  function renderEvaluationAudit() {
    const body = $('#evaluation-audit-rows');
    if (!state.evaluationAudit.length) {
      body.replaceChildren(node('tr', '', node('td', 'table-placeholder', '尚无评测审计事件')));
      return;
    }
    body.replaceChildren(...state.evaluationAudit.map((event) => {
      const row = node('tr');
      append(row,
        node('td', '', new Date(event.occurred_at).toLocaleString()),
        node('td', '', userLabel(event.actor_user_id)),
        node('td', '', evaluationActionLabel(event.action)),
        node('td', 'job-id', event.job_id || '—'),
        node('td', '', JSON.stringify(event.metadata || {})));
      return row;
    }));
  }

  async function loadEvaluations(options = {}) {
    if (state.evaluationLoadPromise) return state.evaluationLoadPromise;
    const pending = loadEvaluationsOnce(options);
    state.evaluationLoadPromise = pending;
    try { return await pending; }
    finally { if (state.evaluationLoadPromise === pending) state.evaluationLoadPromise = null; }
  }

  async function loadEvaluationsOnce({ refreshResults = false } = {}) {
    if (!state.connected || window.WenjinIdentity?.mode !== 'accounts'
        || window.WenjinIdentity?.user?.role !== 'admin') return;
    const generation = state.evaluationGeneration;
    const [templates, jobs, audit] = await Promise.all([
      api('/api/admin/evaluations/templates'),
      api('/api/admin/evaluations?limit=100&offset=0'),
      api('/api/admin/evaluations/audit?limit=100&offset=0'),
    ]);
    if (generation !== state.evaluationGeneration) return;
    state.evaluationTemplates = templates.body.templates || [];
    state.evaluationJobs = jobs.body.jobs || [];
    state.evaluationAudit = audit.body.events || [];
    const select = $('#evaluation-template');
    const selected = select.value;
    select.replaceChildren(new Option('选择固定模板…', ''));
    for (const item of state.evaluationTemplates) {
      const access = item.can_submit === false ? `；${item.missing_library_count || 0}项资料检查未通过` : '';
      select.add(new Option(`${item.title}（${item.max_cases}题以内${access}）`, item.experiment_id));
    }
    if (state.evaluationTemplates.some((item) => item.experiment_id === selected)) select.value = selected;
    updateEvaluationSubmitState();
    renderEvaluationJobs();
    renderEvaluationAudit();
    const running = state.evaluationJobs.some((job) => ['queued', 'preparing', 'running', 'cancelling'].includes(job.status));
    if (running && !state.evaluationPollTimer) {
      state.evaluationPollTimer = window.setInterval(() => {
        loadEvaluations().catch((error) => showApiError(error, '刷新评测任务失败：'));
      }, 2500);
    } else if (!running && state.evaluationPollTimer) {
      window.clearInterval(state.evaluationPollTimer);
      state.evaluationPollTimer = null;
    }
    if (refreshResults && state.evaluationSelectedId) {
      await loadEvaluationResult(state.evaluationSelectedId, generation, state.evaluationSelectionEpoch);
    }
  }

  function updateEvaluationSubmitState() {
    const selected = state.evaluationTemplates.find((item) => item.experiment_id === $('#evaluation-template').value);
    const button = $('#evaluation-submit');
    if (!button) return;
    const blocked = Boolean(selected && selected.can_submit === false);
    button.disabled = blocked;
    const note = $('#evaluation-template-note'); note.replaceChildren();
    if (blocked) {
      append(note, node('span', '', `当前题集有${selected.missing_library_count || 0}项资料检查未通过；管理员可读取所有资料库，此状态由服务端题集校验决定。`));
      const visibleMissing = selected.discoverable_missing_libraries || [];
      if (visibleMissing.length) append(note, node('span', '', ` 可检查的逻辑资料库：${visibleMissing.join('、')}。`));
      if (visibleMissing.length < (selected.missing_library_count || 0)) append(note, node('span', '', ' 其余固定输入授权项暂不可在当前资源目录中显示。'));
    } else {
      note.textContent = '提交时会由服务端再次核验管理员身份和题集状态；结果中的候选数量和耗时用于检索诊断。';
    }
  }

  function showEvaluationAccessNotice(payload) {
    if (payload?.error !== 'evaluation_library_access_required') return;
    const note = $('#evaluation-template-note'); note.replaceChildren();
    append(note, node('span', '', payload.detail || '提交期间检测到资料库读取授权变化。'));
    const visibleMissing = payload.discoverable_missing_libraries || [];
    if (visibleMissing.length) append(note, node('span', '', ` 可检查的逻辑资料库：${visibleMissing.join('、')}。`));
    const link = node('button', 'button button-secondary', '前往身份与授权');
    link.type = 'button'; link.addEventListener('click', () => navigate('identity')); note.append(' ', link);
  }

  async function submitEvaluation(event) {
    event.preventDefault();
    const experimentId = $('#evaluation-template').value;
    if (!experimentId) { toast('请选择固定评测模板', 'error'); return; }
    const button = $('#evaluation-submit');
    button.disabled = true;
    try {
      const { body } = await api('/api/admin/evaluations', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ experiment_id: experimentId }),
      });
      state.evaluationSelectedId = body.job_id;
      state.evaluationSelectionEpoch += 1;
      toast('评测已排队；题目进度按已保存的完成记录统计。');
      await loadEvaluations();
    } catch (error) { showEvaluationAccessNotice(error.payload); showApiError(error, '提交评测失败：'); }
    finally { button.disabled = false; }
  }

  async function evaluationAction(action, jobId) {
    const generation = state.evaluationGeneration;
    if (action === 'view') {
      state.evaluationSelectedId = jobId;
      const selectionEpoch = ++state.evaluationSelectionEpoch;
      await loadEvaluationResult(jobId, generation, selectionEpoch);
      return;
    }
    const path = action === 'cancel' ? 'cancel' : 'resume';
    await api(`/api/admin/evaluations/${encodeURIComponent(jobId)}/${path}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
    });
    await loadEvaluations();
  }

  async function loadEvaluationResult(jobId, generation = state.evaluationGeneration,
                                      selectionEpoch = state.evaluationSelectionEpoch) {
    const title = $('#evaluation-result-title');
    const meta = $('#evaluation-result-meta');
    title.textContent = '检索方式对比结果';
    meta.textContent = '正在读取已完成任务的摘要…';
    $('#evaluation-arm-rows').replaceChildren();
    $('#evaluation-per-case').replaceChildren();
    const { body } = await api(`/api/admin/evaluations/${encodeURIComponent(jobId)}/result?file=summary.json`);
    if (generation !== state.evaluationGeneration || selectionEpoch !== state.evaluationSelectionEpoch
        || jobId !== state.evaluationSelectedId) return;
    state.evaluationComparison = body;
    const job = state.evaluationJobs.find((item) => item.job_id === jobId);
    const profiles = body.profiles || {};
    const armRows = Object.entries(profiles).map(([profile, result]) => {
      const row = node('tr');
      append(row, node('td', '', profileLabel(profile)), node('td', '', result.cases),
        node('td', '', result.candidate_count), node('td', '', result.selected_evidence),
        node('td', '', Number(result.mean_total_seconds || 0).toFixed(3)));
      return row;
    });
    $('#evaluation-arm-rows').replaceChildren(...(armRows.length ? armRows : [node('tr', '', node('td', 'table-placeholder', '没有可比较的检索方式'))]));
    meta.textContent = `任务 ${jobId}；${evaluationStatus(job?.status || 'completed')}；完成题数 ${body.case_count}；检索资料版本未变化：${body.assets_unchanged ? '是' : '否'}。这些指标用于检索诊断，不代表语义正确率。`;
    const cases = node('div', 'evaluation-cases');
    for (const caseId of body.valid_case_ids || []) {
      const item = node('div', 'evaluation-case');
      const label = node('strong', '', caseId);
      const view = node('button', 'button button-secondary', '查看各臂证据');
      view.type = 'button';
      view.addEventListener('click', () => loadEvaluationCase(jobId, caseId, generation, selectionEpoch)
        .catch((error) => showApiError(error, '读取单题证据失败：')));
      append(item, label, view);
      cases.append(item);
    }
    $('#evaluation-per-case').replaceChildren(cases);
  }

  async function loadEvaluationCase(jobId, caseId, generation, selectionEpoch) {
    const { body } = await api(`/api/admin/evaluations/${encodeURIComponent(jobId)}/result?case_id=${encodeURIComponent(caseId)}`);
    if (generation !== state.evaluationGeneration || selectionEpoch !== state.evaluationSelectionEpoch
        || jobId !== state.evaluationSelectedId) return;
    const target = $('#evaluation-per-case');
    const detail = node('section', 'evaluation-case');
    detail.append(node('h3', '', `${caseId} · ${body.query || ''}`));
    for (const [profile, result] of Object.entries(body.profiles || {})) {
      const arm = node('article', 'evaluation-arm-detail');
      arm.append(node('strong', '', `${profileLabel(profile)} · 返回证据 ${result.selected?.length || 0}`));
      for (const evidence of result.selected || []) {
        const card = node('div', 'evaluation-evidence');
        append(card,
          node('small', '', `${evidence.source_path || evidence.source_id || '来源未知'}:${evidence.start_line ?? '?'}-${evidence.end_line ?? '?'}`),
          node('pre', '', evidence.text || ''));
        arm.append(card);
      }
      detail.append(arm);
    }
    const old = target.querySelector(`[data-case-detail="${CSS.escape(caseId)}"]`);
    detail.dataset.caseDetail = caseId;
    if (old) old.replaceWith(detail); else target.prepend(detail);
  }

  function navigate(page) {
    const target = $(`#page-${page}`);
    if (!target) return;
    $$('.page-view').forEach((view) => view.classList.toggle('active', view === target));
    $$('.nav-item').forEach((item) => item.classList.toggle('active', item.dataset.page === page));
    $('#current-page-title').textContent = target.dataset.title || '管理中心';
    if (page === 'jobs' && state.connected && !state.jobs.length) loadJobs({ quiet: true }).catch(showApiError);
    if (page === 'files' && state.connected && !state.files.length) loadFiles({ quiet: true }).catch(showApiError);
    if (page === 'calls' && state.connected) loadCalls({ offset: state.callOffset, quiet: true }).catch((error) => showApiError(error, '读取调用日志失败：'));
    if (page === 'search' && state.connected) refreshAuthorizedCatalog().catch((error) => showApiError(error, '更新资料库列表失败：'));
    if (page === 'releases' && state.connected) loadReleaseState().catch((error) => showApiError(error, '读取发布版本失败：'));
    if (page === 'evaluations' && state.connected) loadEvaluations().catch((error) => showApiError(error, '读取评测任务失败：'));
    if (window.matchMedia('(max-width: 850px)').matches) window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  function wireEvents() {
    window.addEventListener('wenjin-account-ready', () => {
      if (window.WenjinIdentity?.mode !== 'accounts') return;
      clearSensitiveState();
      syncReleaseAccess();
      state.token = null;
      const user = window.WenjinIdentity.user;
      const avatar = $('.avatar');
      if (avatar && user) { avatar.textContent = user.role === 'admin' ? '管' : user.username.slice(0, 1); avatar.setAttribute('aria-label', user.role === 'admin' ? '管理员' : '只读用户'); }
      renderAccountMenu();
      refreshAll({ quiet: true }).catch((error) => showApiError(error));
    });
    window.addEventListener('wenjin-user-directory-ready', () => { renderEvaluationJobs(); renderEvaluationAudit(); });
    window.addEventListener('wenjin-grants-changed', (event) => {
      const affected = event.detail?.user_ids || [];
      const currentUserId = window.WenjinIdentity?.user?.user_id;
      if (!currentUserId || !affected.includes(currentUserId)) return;
      const revokedLibrary = event.detail?.allowed === false ? event.detail?.library_id : null;
      const selectedLibrary = $('#search-library').value;
      if (revokedLibrary && (selectedLibrary === revokedLibrary || state.lastSearchTarget?.library_id === revokedLibrary)) {
        state.searchRevision += 1;
        state.lastSearch = null;
        state.lastSearchLibraryId = null;
        state.lastSearchTarget = null;
        $('#results-meta').replaceChildren();
        $('#results-meta').classList.add('hidden');
        $('#evidence-list').replaceChildren(evidenceEmpty('资料库授权已撤销', '旧证据已清除，请选择仍可访问的资料库。'));
        $('#evidence-state').textContent = '授权已撤销';
        $('#evidence-state').className = 'evidence-state';
        if (selectedLibrary === revokedLibrary) {
          $('#search-library').value = '';
          $('#search-source').value = '';
          $('#search-document').value = '';
        }
      }
      loadLibraries({quiet: true}).catch((error) => showApiError(error, '更新资料库列表失败：'));
    });
    window.addEventListener('wenjin-access-catalog-invalidated', () => {
      // 迟到的授权写入不携带旧账号/授权详情，只清除当前页面证据并重读当前账号目录。
      if (!state.connected || !window.WenjinIdentity?.user) return;
      state.searchRevision += 1;
      state.libraryLoadRevision += 1;
      state.lastSearch = null;
      state.lastSearchLibraryId = null;
      state.lastSearchTarget = null;
      state.searchTarget = null;
      $('#results-meta').replaceChildren();
      $('#results-meta').classList.add('hidden');
      $('#evidence-list').replaceChildren(evidenceEmpty('资料库授权状态已更新', '目录正在刷新，请重新选择资料库后检索。'));
      $('#evidence-state').textContent = '目录正在刷新';
      $('#evidence-state').className = 'evidence-state';
      $('#search-library').value = '';
      $('#search-source').value = '';
      $('#search-document').value = '';
      loadLibraries({quiet: true}).catch((error) => showApiError(error, '更新资料库列表失败：'));
    });
    window.addEventListener('focus', () => refreshAuthorizedCatalog().catch((error) => showApiError(error, '更新资料库列表失败：')));
    $('#search-library').addEventListener('focus', () => refreshAuthorizedCatalog().catch((error) => showApiError(error, '更新资料库列表失败：')));
    $$('.nav-item').forEach((button) => button.addEventListener('click', () => navigate(button.dataset.page)));
    $('.brand').addEventListener('click', (event) => { event.preventDefault(); navigate('overview'); });
    $$('[data-goto]').forEach((button) => button.addEventListener('click', () => navigate(button.dataset.goto)));
    $$('[data-action="upload"]').forEach((button) => button.addEventListener('click', () => chooseUpload()));
    $('#open-auth').addEventListener('click', openConnectionSettings);
    $('#account-connect').addEventListener('click', openConnectionSettings);
    $('#account-disconnect').addEventListener('click', disconnectLegacyConnection);
    $('#account-switch-account').addEventListener('click', switchAccountOrSignOut);
    $('#account-logout').addEventListener('click', switchAccountOrSignOut);
    $('#banner-connect').addEventListener('click', openConnectionSettings);
    $('#dismiss-banner').addEventListener('click', () => $('#connection-banner').classList.add('hidden'));
    $('#auth-form').addEventListener('submit', (event) => {
      if (event.submitter?.id !== 'connect-button') return;
      event.preventDefault();
      connectWithToken($('#admin-token').value.trim());
    });
    $('#disconnect-button').addEventListener('click', disconnectLegacyConnection);
    $('#refresh-files').addEventListener('click', () => loadFiles().catch((error) => showApiError(error)));
    $('#file-classification-filter').addEventListener('change', renderFiles);
    $('#file-type-filter').addEventListener('change', renderFiles);
    $('#refresh-jobs').addEventListener('click', () => loadJobs({ offset: 0 }).catch((error) => showApiError(error)));
    $('#file-search').addEventListener('input', renderFiles);
    $('#file-status-filter').addEventListener('change', renderFiles);
    $('#job-search').addEventListener('input', renderJobs);
    $('#job-kind-filter').addEventListener('change', renderJobs);
    $('#job-status-filter').addEventListener('change', renderJobs);
    $('#jobs-prev').addEventListener('click', () => changeJobPage(-1));
    $('#jobs-next').addEventListener('click', () => changeJobPage(1));
    $('#lookup-job').addEventListener('click', lookupJob);
    $('#job-id-lookup').addEventListener('keydown', (event) => { if (event.key === 'Enter') { event.preventDefault(); lookupJob(); } });
    $('#search-form').addEventListener('submit', runSearch);
    $('#search-library').addEventListener('change', () => { $('#search-source').value = ''; $('#search-document').value = ''; renderSearchSources(); });
    $('#search-source').addEventListener('change', () => { $('#search-document').value = ''; renderSearchDocuments(); });
    $('#search-document').addEventListener('change', () => {});
    $('#search-query').addEventListener('input', () => { $('#query-length').textContent = `${$('#search-query').value.length} / 2000`; });
    $('#upload-cancel-button').addEventListener('click', () => $('#upload-dialog').close());
    $('#upload-close-button').addEventListener('click', () => $('#upload-dialog').close());
    $('#upload-dialog').addEventListener('cancel', () => { /* Escape只关闭对话框，不会提交上传。 */ });
    $('#choose-upload-file').addEventListener('click', () => {
      const libraryId = $('#upload-library').value;
      if (!libraryId) { toast('请选择新文件归属的资料库', 'error'); return; }
      state.uploadLibraryId = libraryId;
      $('#upload-dialog').close();
      startUploadPicker(null);
    });
    $('#create-library-button').addEventListener('click', async () => {
      const button = $('#create-library-button');
      const displayName = $('#new-library-name').value.trim();
      if (!displayName) { toast('请先填写资料库名称', 'error'); return; }
      const generation = state.generation;
      button.disabled = true;
      try {
        const { body } = await api('/api/admin/managed-libraries', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ display_name: displayName }),
        });
        if (generation !== state.generation || !state.connected) return;
        await Promise.all([loadManagedLibraries(), loadLibraries({ quiet: true })]);
        if (generation !== state.generation) return;
        const library = body?.library;
        if (library?.library_id) {
          state.uploadLibraryId = library.library_id;
          $('#upload-library').value = library.library_id;
        }
        $('#new-library-name').value = '';
        window.dispatchEvent(new CustomEvent('wenjin-managed-library-created', { detail: { library_id: library?.library_id } }));
        toast(`已创建资料库「${library?.display_name || displayName}」；尚未授予读取权限`);
      } catch (error) { if (generation === state.generation) showApiError(error, '创建资料库失败：'); }
      finally { if (generation === state.generation) button.disabled = false; }
    });
    $('#refresh-calls').addEventListener('click', () => loadCalls({ offset: 0 }).catch((error) => showApiError(error, '读取调用日志失败：')));
    $('#release-library').addEventListener('change', () => {
      state.releaseLibraryId = $('#release-library').value;
      state.releaseSnapshot = null;
      renderReleasePage();
      loadReleaseState().catch((error) => showApiError(error, '读取发布版本失败：'));
    });
    $('#release-refresh').addEventListener('click', () => loadReleaseState().catch((error) => showApiError(error, '读取发布版本失败：')));
    $('#release-prepare-form').addEventListener('submit', prepareRelease);
    $('#evaluation-submit-form').addEventListener('submit', submitEvaluation);
    $('#evaluation-template').addEventListener('change', updateEvaluationSubmitState);
    $('#evaluation-refresh').addEventListener('click', () => loadEvaluations({refreshResults: true}).catch((error) => showApiError(error, '刷新评测任务失败：')));
    $('#evaluation-job-rows').addEventListener('click', (event) => {
      const button = event.target.closest('[data-evaluation-action]');
      if (button) evaluationAction(button.dataset.evaluationAction, button.dataset.jobId)
        .catch((error) => showApiError(error, '评测操作失败：'));
    });
    $('#call-type-filter').addEventListener('change', callFiltersChanged);
    $('#call-status-filter').addEventListener('change', callFiltersChanged);
    $('#call-target-filter').addEventListener('input', () => {
      if (state.callFilterTimer) window.clearTimeout(state.callFilterTimer);
      state.callFilterTimer = window.setTimeout(callFiltersChanged, 380);
    });
    $('#call-retrieval-filter').addEventListener('input', () => {
      if (state.callFilterTimer) window.clearTimeout(state.callFilterTimer);
      state.callFilterTimer = window.setTimeout(callFiltersChanged, 380);
    });
    $('#call-target-filter').addEventListener('keydown', (event) => { if (event.key === 'Enter') { event.preventDefault(); if (state.callFilterTimer) window.clearTimeout(state.callFilterTimer); callFiltersChanged(); } });
    $('#call-retrieval-filter').addEventListener('keydown', (event) => { if (event.key === 'Enter') { event.preventDefault(); if (state.callFilterTimer) window.clearTimeout(state.callFilterTimer); callFiltersChanged(); } });
    $('#calls-prev').addEventListener('click', () => changeCallPage(-1));
    $('#calls-next').addEventListener('click', () => changeCallPage(1));
    $$('[data-close]').forEach((button) => button.addEventListener('click', () => $(`#${button.dataset.close}`).close()));
    $('#detail-dialog').addEventListener('close', () => { state.jobLookup = null; state.selectedFile = null; });
    window.addEventListener('online', () => { if (!state.connected) refreshAll({ quiet: true }); });
  }

  if (window.WenjinIdentity) {
    window.WenjinIdentity.setClearPageState(() => clearSensitiveState({ disconnected: true }));
    await window.WenjinIdentity.ready;
    syncReleaseAccess();
    if (window.WenjinIdentity.mode === 'accounts') {
      state.token = null;
      $('#open-auth').classList.add('hidden');
      $('#identity-logout').classList.remove('hidden');
      const user = window.WenjinIdentity.user;
      const avatar = document.querySelector('#account-avatar');
      avatar.textContent = user?.role === 'admin' ? '管' : user?.username?.slice(0, 1) || '用';
      avatar.setAttribute('aria-label', user?.role === 'admin' ? '管理员' : '只读用户');
    }
  }
  wireEvents();
  renderAll();
  renderAccountMenu();
  setConnection('idle');
  refreshAll({ quiet: true }).then((connected) => {
    if (!connected && !$('#connection-banner').classList.contains('hidden')) $('#banner-title').textContent = '无法连接管理服务';
  });
})();
