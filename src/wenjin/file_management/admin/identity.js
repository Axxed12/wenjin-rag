/* 账户登录与管理UI；CSRF值只保存在本页内存。 */
(() => {
  const state = { mode: 'legacy', user: null, csrf: null, clearPageState: null, ready: null, generation: 0, reauth: null, signingOut: false, users: [], usersLoaded: false, sessionPolicy: null, adminCredentialSelectionRevision: 0, adminCredentialListRevision: 0, adminCredentialPending: false, personalCredentialPending: false };
  const $ = (selector) => document.querySelector(selector);
  const deliveryRevisions = new Map([['credential',0],['admin-credential',0]]);
  const h = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const beijingDateTime = (value) => {
    if (!value) return '—';
    const date = new Date(value);
    if (!Number.isFinite(date.getTime())) return '—';
    const parts = new Intl.DateTimeFormat('en-CA', {timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'}).formatToParts(date);
    const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
    return `${values.year}-${values.month}-${values.day} ${values.hour}:${values.minute}:${values.second}`;
  };
  function clearCredentialDelivery(prefix) {
    deliveryRevisions.set(prefix, (deliveryRevisions.get(prefix) || 0) + 1);
    const panel = $(`#${prefix}-delivery`);
    const token = $(`#${prefix}-token`);
    const meta = $(`#${prefix}-delivery-meta`);
    const copyStatus = $(`#${prefix}-copy-status`);
    if (token) token.value = '';
    if (meta) meta.textContent = '';
    if (copyStatus) copyStatus.textContent = '';
    panel?.classList.add('hidden');
  }
  function showCredentialDelivery(prefix, credential, owner, label) {
    const token = $(`#${prefix}-token`);
    const panel = $(`#${prefix}-delivery`);
    if (!token || !panel) return;
    deliveryRevisions.set(prefix, (deliveryRevisions.get(prefix) || 0) + 1);
    token.value = credential.token;
    $(`#${prefix}-delivery-meta`).textContent = `所属账号：${owner}；名称：${label}；用途：Agent 使用部署提供的 MCP 地址并通过 Bearer 凭据连接；到期：${credential.expires_at ? beijingDateTime(credential.expires_at) : '不自动过期'}`;
    $(`#${prefix}-copy-status`).textContent = '请现在安全保存完整凭据；本页面关闭或切换账号后无法恢复。';
    panel.classList.remove('hidden');
    token.focus(); token.select();
  }
  async function copyCredential(prefix) {
    const token = $(`#${prefix}-token`);
    const status = $(`#${prefix}-copy-status`);
    if (!token?.value) return;
    const value = token.value; const generation = state.generation; const revision = deliveryRevisions.get(prefix);
    const stillCurrent = () => generation === state.generation && revision === deliveryRevisions.get(prefix) && token.value === value;
    try {
      const clipboard = window.navigator?.clipboard;
      if (!clipboard?.writeText) throw new Error('clipboard unavailable');
      await clipboard.writeText(value);
      if (stillCurrent()) status.textContent = '凭据已复制到剪贴板；请按组织要求安全保存。';
    } catch {
      if (!stillCurrent()) return;
      token.focus(); token.select();
      status.textContent = '无法自动复制。完整凭据已选中，请按 Ctrl+C（或在移动设备上使用复制菜单）；完成后请清除交付区。';
    }
  }
  function credentialStatus(item) {
    if (item.revoked_at) return '已撤销';
    if (item.expires_at) {
      const expires = new Date(item.expires_at);
      if (Number.isFinite(expires.getTime()) && expires.getTime() <= Date.now()) return '已过期';
    }
    return '有效';
  }
  const identityActionLabel = (action) => ({
    bootstrap_first_admin:'创建首位管理员', user_created:'创建用户', user_role_changed:'调整用户角色',
    user_renamed:'修改用户名', user_activation_changed:'启用或停用用户', user_password_reset:'重置用户密码',
    login_failed:'登录失败', login_succeeded:'登录成功', logout:'退出登录', password_change_failed:'修改密码失败',
    password_changed:'修改密码', mcp_credential_issued:'签发个人连接凭据', mcp_credential_revoked:'撤销个人连接凭据',
    web_session_revoked:'撤销登录会话', resource_catalog_synced:'同步资料库目录', resource_grant_changed:'调整资料库读取权限',
  }[action] || action || '未知操作');

  async function request(path, options = {}) {
    const generation = state.generation;
    const headers = new Headers(options.headers || {});
    const method = (options.method || 'GET').toUpperCase();
    if (method !== 'GET' && method !== 'HEAD') {
      headers.set('Origin', window.location.origin);
      if (state.csrf) headers.set('X-CSRF-Token', state.csrf);
    }
    const response = await fetch(path, {...options, method, headers, credentials: 'same-origin', cache: 'no-store'});
    const payload = (response.headers.get('content-type') || '').includes('application/json') ? await response.json() : null;
    if (generation !== state.generation) throw Object.assign(new Error('账号状态已变化，已忽略旧请求。'), {status: 0, stale: true, responseOk: response.ok, responseStatus: response.status});
    if (!response.ok) {
      const messages = {
        current_password_invalid: '当前密码不正确，请重新输入。',
        unauthorized: '登录已过期，请重新登录。',
        forbidden: '当前账号没有执行此操作的权限。',
      };
      throw Object.assign(new Error(payload?.detail || messages[payload?.error] || `请求失败（HTTP ${response.status}）`), {status: response.status, code: payload?.error});
    }
    return payload;
  }

  function clearState() {
    state.generation += 1;
    state.adminCredentialSelectionRevision += 1;
    state.adminCredentialListRevision += 1;
    state.adminCredentialPending = false;
    state.personalCredentialPending = false;
    state.user = null;
    state.csrf = null;
    state.users = [];
    state.usersLoaded = false;
    $('#identity-admin-panel').classList.add('hidden');
    $('#identity-self-panel').classList.add('hidden');
    $('#identity-session-panel').classList.add('hidden');
    document.querySelectorAll('[data-page="files"],[data-page="jobs"],[data-page="calls"],[data-page="overview"],[data-page="roadmap"],[data-page="releases"],[data-page="evaluations"]').forEach((item) => item.classList.add('hidden'));
    $('#identity-current-user').textContent = '';
    $('#identity-users').replaceChildren();
    $('#identity-grants').replaceChildren();
    $('#identity-audit').replaceChildren();
    $('#my-credentials').replaceChildren();
    $('#admin-user-credentials').replaceChildren();
    $('#admin-credential-user').value = '';
    $('#credential-status').textContent = '';
    $('#admin-credential-status').textContent = '';
    $('#admin-credential-target-note').textContent = '请选择所属用户。';
    clearCredentialDelivery('credential');
    clearCredentialDelivery('admin-credential');
    $('#credential-form').querySelector('button').disabled = false;
    $('#admin-credential-form').querySelector('button').disabled = true;
    $('#admin-credential-user').disabled = false;
    $('#admin-credential-refresh').disabled = true;
    if (state.clearPageState) state.clearPageState();
  }

  function setUser(user) {
    clearCredentialDelivery('credential');
    clearCredentialDelivery('admin-credential');
    $('#credential-status').textContent = '';
    $('#admin-credential-status').textContent = '';
    state.adminCredentialSelectionRevision += 1;
    state.adminCredentialListRevision += 1;
    state.adminCredentialPending = false;
    state.personalCredentialPending = false;
    if (state.user?.user_id !== user.user_id) {
      state.users = [];
      state.usersLoaded = false;
    }
    state.generation += 1;
    state.user = user;
    // 管理员统一通过“用户 MCP 凭据”选择账号管理，包括自己的账号，避免出现两套容易混淆的列表。
    $('#identity-self-panel').classList.toggle('hidden', user.role === 'admin');
    $('#identity-session-panel').classList.remove('hidden');
    $('#identity-current-user').replaceChildren();
    $('#identity-current-user').append(document.createTextNode(user.username));
    const roleBadge = document.createElement('span');
    roleBadge.className = user.role === 'admin' ? 'identity-role-badge identity-role-admin' : 'identity-role-badge';
    roleBadge.textContent = user.role === 'admin' ? '管理员' : '只读用户';
    $('#identity-current-user').append(' ', roleBadge);
    $('#identity-admin-panel').classList.toggle('hidden', user.role !== 'admin');
    $('#identity-empty-grants').classList.add('hidden');
    document.querySelectorAll('[data-page="files"],[data-page="jobs"],[data-page="calls"],[data-page="overview"],[data-page="roadmap"]').forEach((item) => {
      item.classList.toggle('hidden', user.role !== 'admin');
    });
    if (user.role === 'reader') {
      document.querySelectorAll('[data-page="search"]').forEach((item) => item.classList.remove('hidden'));
    }
    window.dispatchEvent(new CustomEvent('wenjin-account-ready', {detail: user}));
  }

  function renderSessionPolicy() {
    const target = $('#identity-session-policy');
    if (!target || !state.sessionPolicy) return;
    const idleMinutes = Math.round(Number(state.sessionPolicy.idle_seconds) / 60);
    const absoluteHours = Number(state.sessionPolicy.absolute_seconds) / 3600;
    target.textContent = `登录最长${absoluteHours}小时；连续${idleMinutes}分钟没有经过认证的服务请求会过期。页面轮询或其他认证请求可能延长空闲计时，密码修改会撤销此账号现有会话和个人MCP凭据。`;
  }

  async function loginPrompt() {
    const dialog = $('#identity-login-dialog');
    const form = $('#identity-login-form');
    $('#identity-login-error').textContent = '';
    if (!dialog.open) dialog.showModal();
    return new Promise((resolve) => {
      const submit = async (event) => {
        event.preventDefault();
        const button = $('#identity-login-submit');
        button.disabled = true;
        try {
          const result = await request('/api/auth/login', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({username: form.elements.username.value, password: form.elements.password.value})});
          state.csrf = result.csrf_token;
          form.reset();
          dialog.close();
          form.removeEventListener('submit', submit);
          if (result.must_change_password) {
            state.pendingPasswordUser = result.user;
            const changed = await forcedPasswordPrompt();
            resolve(Boolean(changed && state.user));
          } else {
            setUser(result.user);
            resolve(true);
          }
        } catch (error) {
          if (error.status !== 0) $('#identity-login-error').textContent = error.status === 429 ? '尝试次数过多，请稍后重试。' : error.message || '用户名或密码错误。';
        } finally { button.disabled = false; }
      };
      form.addEventListener('submit', submit);
      dialog.addEventListener('cancel', (event) => event.preventDefault(), {once: true});
    });
  }

  async function forcedPasswordPrompt() {
    const dialog = $('#identity-password-dialog');
    const form = $('#identity-password-form');
    $('#identity-password-error').textContent = '';
    dialog.showModal();
    return new Promise((resolve) => {
      const submit = async (event) => {
        event.preventDefault();
        try {
          const result = await request('/api/auth/password', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({old_password: form.elements.old_password.value, new_password: form.elements.new_password.value})});
          const username = result.user.username || state.pendingPasswordUser?.username;
          const newPassword = form.elements.new_password.value;
          form.reset();
          dialog.close();
          form.removeEventListener('submit', submit);
          const login = await request('/api/auth/login', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username,password:newPassword})});
          state.csrf = login.csrf_token;
          state.pendingPasswordUser = null;
          setUser(login.user);
          resolve(true);
        } catch (error) {
          if (dialog.open) $('#identity-password-error').textContent = error.status === 0 ? '' : error.message;
          else {
            state.csrf = null;
            state.pendingPasswordUser = null;
            $('#identity-login-error').textContent = error.status === 0 ? '' : '密码已更新，请使用新密码登录。';
            await loginPrompt();
            resolve(true);
          }
        }
      };
      form.addEventListener('submit', submit);
      $('#identity-force-logout').onclick = async () => { form.removeEventListener('submit', submit); await signOut(); resolve(false); };
      dialog.addEventListener('cancel', (event) => event.preventDefault(), {once: true});
    });
  }

  async function signOut() {
    if (state.signingOut) return;
    state.signingOut = true;
    try {
      $('#identity-password-dialog').close();
      // 先发出携带当前 CSRF 的撤销请求，再立即推进代次并清理页面；慢响应不能让旧证据留在界面或重新回填。
      const logoutRequest = request('/api/auth/logout', {method: 'POST'}).catch(() => {});
      clearState();
      await logoutRequest;
      await loginPrompt();
    } finally { state.signingOut = false; }
  }

  async function refreshCsrf() {
    const body = await request('/api/auth/csrf', {method: 'POST'});
    state.csrf = body.csrf_token;
  }

  function renderCredentialTable(host, credentials, targetUserId, adminManaged) {
    host.replaceChildren();
    if (!credentials.length) {
      const empty = document.createElement('p'); empty.className = 'credential-empty'; empty.textContent = '该账号暂无 MCP 凭据。'; host.append(empty); return;
    }
    const table = document.createElement('table'); table.className = 'data-table credential-table';
    const thead = document.createElement('thead'); const heading = document.createElement('tr');
    for (const label of ['名称','安全前缀','状态','时间（北京时间）','操作']) {
      const th = document.createElement('th'); th.textContent = label;
      if (label === '操作') th.className = 'credential-action';
      heading.append(th);
    }
    thead.append(heading); const tbody = document.createElement('tbody');
    for (const item of credentials) {
      const row = document.createElement('tr');
      const status = credentialStatus(item);
      for (const [field, value] of [['credential-name', item.label], ['credential-prefix', item.token_prefix], ['credential-state', status]]) {
        const cell = document.createElement('td'); cell.className = field; cell.textContent = value || '—';
        if (field === 'credential-prefix') { const code = document.createElement('code'); code.textContent = item.token_prefix || '—'; cell.replaceChildren(code); }
        if (field === 'credential-state') cell.className = `credential-state ${status === '有效' ? 'is-active' : status === '已撤销' ? 'is-revoked' : 'is-expired'}`;
        row.append(cell);
      }
      const dates = document.createElement('td'); dates.className = 'credential-dates';
      dates.textContent = `创建：${beijingDateTime(item.created_at)}\n到期：${item.expires_at ? beijingDateTime(item.expires_at) : '不自动过期'}`
        + (item.revoked_at ? `\n撤销：${beijingDateTime(item.revoked_at)}` : '');
      row.append(dates);
      const actions = document.createElement('td'); actions.className = 'credential-action';
      if (!item.revoked_at) {
        const revoke = document.createElement('button'); revoke.type = 'button'; revoke.className = 'button button-secondary'; revoke.textContent = '撤销凭据';
        revoke.onclick = async () => {
          const owner = adminManaged ? (state.users.find((user) => user.user_id === targetUserId)?.username || '所选用户') : (state.user?.username || '当前账号');
          if (!window.confirm(`确认撤销“${item.label}”（前缀 ${item.token_prefix}）？\n所属账号：${owner}\n该凭据之后将不能连接 MCP。`)) return;
          const generation = state.generation;
          const selectionRevision = state.adminCredentialSelectionRevision;
          revoke.disabled = true;
          try {
            const endpoint = adminManaged ? '/api/admin/credentials/' : '/api/auth/credentials/';
            await request(`${endpoint}${encodeURIComponent(item.credential_id)}`, {method:'DELETE'});
            if (generation !== state.generation || (adminManaged && (selectionRevision !== state.adminCredentialSelectionRevision || $('#admin-credential-user').value !== targetUserId))) return;
            const statusHost = $(adminManaged ? '#admin-credential-status' : '#credential-status');
            statusHost.textContent = `已撤销“${item.label}”（${item.token_prefix}）。`;
            if (adminManaged) await loadAdminCredentials(targetUserId); else await loadCredentials();
          } catch (error) {
            if (error.stale || generation !== state.generation) return;
            revoke.disabled = false;
            (adminManaged ? $('#admin-credential-status') : $('#credential-status')).textContent = `撤销失败：${error.message}`;
          }
        };
        actions.append(revoke);
      } else actions.textContent = '—';
      row.append(actions); tbody.append(row);
    }
    table.append(thead, tbody); host.append(table);
  }

  async function loadCredentials() {
    if (!state.user || state.user.role === 'admin') return false;
    const generation = state.generation;
    const revision = (state.personalCredentialListRevision || 0) + 1;
    state.personalCredentialListRevision = revision;
    try {
      const body = await request('/api/auth/credentials');
      if (generation !== state.generation || revision !== state.personalCredentialListRevision) return false;
      renderCredentialTable($('#my-credentials'), body.credentials || [], state.user?.user_id, false);
      return true;
    } catch (error) {
      if (!error.stale && generation === state.generation) $('#my-credentials').textContent = `读取凭据列表失败：${error.message}`;
      return false;
    }
  }

  async function loadAdminCredentials(userId = $('#admin-credential-user').value) {
    if (!userId || state.user?.role !== 'admin') return false;
    const generation = state.generation;
    const selectionRevision = state.adminCredentialSelectionRevision;
    const revision = ++state.adminCredentialListRevision;
    $('#admin-credential-refresh').disabled = true;
    try {
      const body = await request(`/api/admin/users/${encodeURIComponent(userId)}/credentials`);
      if (generation !== state.generation || selectionRevision !== state.adminCredentialSelectionRevision
          || revision !== state.adminCredentialListRevision || $('#admin-credential-user').value !== userId) return false;
      renderCredentialTable($('#admin-user-credentials'), body.credentials || [], userId, true);
      return true;
    } catch (error) {
      if (!error.stale && generation === state.generation && selectionRevision === state.adminCredentialSelectionRevision
          && $('#admin-credential-user').value === userId) {
        $('#admin-user-credentials').textContent = `读取所选用户凭据失败：${error.message}`;
      }
      return false;
    } finally {
      if (generation === state.generation && selectionRevision === state.adminCredentialSelectionRevision
          && $('#admin-credential-user').value === userId && !state.adminCredentialPending) $('#admin-credential-refresh').disabled = false;
    }
  }

  function renderAdminCredentialUsers(users) {
    const select = $('#admin-credential-user');
    const previousId = select.value;
    select.replaceChildren();
    const placeholder = document.createElement('option'); placeholder.value = ''; placeholder.textContent = '选择用户'; select.append(placeholder);
    for (const user of users) {
      const option = document.createElement('option'); option.value = user.user_id;
      option.textContent = `${user.username} · ${user.active ? '启用' : '停用'}${user.must_change_password ? ' · 待首次改密' : ''}`;
      select.append(option);
    }
    select.value = users.some((user) => user.user_id === previousId) ? previousId : '';
    if (!select.value && previousId) {
      state.adminCredentialSelectionRevision += 1;
      state.adminCredentialListRevision += 1;
      clearCredentialDelivery('admin-credential');
      $('#admin-user-credentials').replaceChildren();
    }
    updateAdminCredentialTarget();
  }

  function updateAdminCredentialTarget() {
    const user = state.users.find((item) => item.user_id === $('#admin-credential-user').value);
    const blocked = !user || !user.active || user.must_change_password;
    const button = $('#admin-credential-form').querySelector('button');
    button.disabled = blocked || state.adminCredentialPending;
    $('#admin-credential-refresh').disabled = !user || state.adminCredentialPending;
    if (!user) $('#admin-credential-target-note').textContent = '请选择所属用户。';
    else if (!user.active) $('#admin-credential-target-note').textContent = `${user.username}账号已停用，后端不允许签发；仍可查看或撤销已有凭据。`;
    else if (user.must_change_password) $('#admin-credential-target-note').textContent = `${user.username}需先完成首次密码修改，之后才能签发 MCP 凭据。`;
    else if (user.role === 'admin') $('#admin-credential-target-note').textContent = `${user.username}是管理员：该凭据绑定管理员身份，可读取所有有效资料库。请勿将管理员账号凭据分发给普通用户；MCP 地址由部署提供，Agent 使用 Bearer 凭据连接。`;
    else $('#admin-credential-target-note').textContent = `凭据将绑定到 ${user.username}（${user.user_id}），用途为 Agent 使用部署提供的 MCP 地址并通过 Bearer 凭据连接；资料库读取仍按该普通用户的授权决定。`;
  }

  async function loadAdmin() {
    if (state.user?.role !== 'admin') return;
    const generation = state.generation;
    const [userBody, resourceBody, auditBody] = await Promise.all([
      request('/api/admin/users'), request('/api/admin/resources'), request('/api/admin/identity-audit?limit=100'),
    ]);
    if (generation !== state.generation || state.user?.role !== 'admin') return;
    const users = userBody.users || [];
    state.users = users;
    state.usersLoaded = true;
    window.dispatchEvent(new CustomEvent('wenjin-user-directory-ready'));
    const grants = resourceBody.grants || [];
    const userHost = $('#identity-users'); userHost.replaceChildren();
    const userTable = document.createElement('table'); userTable.className = 'data-table';
    userTable.innerHTML = '<thead><tr><th>用户名</th><th>角色</th><th>状态</th><th>首次改密</th><th>管理</th></tr></thead>';
    const userRows = document.createElement('tbody');
    for (const user of users) {
      const tr = document.createElement('tr');
      tr.innerHTML = `<td>${h(user.username)}</td><td>${user.role === 'admin' ? '<span class="identity-role-badge identity-role-admin">管理员</span>' : '只读用户'}</td><td>${user.active ? '启用' : '停用'}</td><td>${user.must_change_password ? '是' : '否'}</td><td></td>`;
      const actions = tr.lastElementChild;
      const role = document.createElement('button'); role.className = 'button button-secondary'; role.textContent = user.role === 'admin' ? '设为只读用户' : '设为管理员';
      role.onclick = async () => { await request(`/api/admin/users/${user.user_id}`, {method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({role:user.role === 'admin' ? 'reader' : 'admin',expected_revision:user.revision})}); await loadAdmin(); };
      const active = document.createElement('button'); active.className = 'button button-secondary'; active.textContent = user.active ? '停用' : '启用';
      active.onclick = async () => { await request(`/api/admin/users/${user.user_id}`, {method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({active:!user.active,expected_revision:user.revision})}); await loadAdmin(); };
      const rename = document.createElement('button'); rename.className = 'button button-secondary'; rename.textContent = '改名';
      rename.onclick = async () => { const username = prompt('输入新的用户名', user.username); if (username) { await request(`/api/admin/users/${user.user_id}`, {method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({username,expected_revision:user.revision})}); await loadAdmin(); } };
      const reset = document.createElement('button'); reset.className = 'button button-secondary'; reset.textContent = '重置密码';
      reset.onclick = async () => { const password = prompt('输入新临时密码（用户下次登录须修改）'); if (password) { await request(`/api/admin/users/${user.user_id}/reset-password`, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({new_password:password,expected_revision:user.revision})}); await loadAdmin(); } };
      actions.append(role, active, rename, reset); userRows.append(tr);
    }
    userTable.append(userRows); userHost.append(userTable);
    renderAdminCredentialUsers(users);
    const create = $('#identity-create-user');
    create.onsubmit = async (event) => {
      event.preventDefault(); const form = new FormData(create);
      await request('/api/admin/users', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:form.get('username'),password:form.get('password'),role:form.get('role')})});
      create.reset(); await loadAdmin();
    };
    const libraryRows = (resourceBody.resources || []).filter((item) => item.resource_kind === 'library' && item.active);
    const grantHost = $('#identity-grants'); grantHost.replaceChildren();
    const adminNotice = document.createElement('p');
    adminNotice.className = 'privacy-note grant-admin-note';
    adminNotice.textContent = '管理员可读取所有有效资料库；普通用户按逐库授权。公开/私有分类不改变读取权限。';
    grantHost.append(adminNotice);
    const knownUsers = new Map(users.map((user) => [user.user_id, user]));
    const readerUsers = users.filter((user) => user.role === 'reader');
    for (const library of libraryRows) {
      const current = grants.filter((grant) => grant.resource_kind === 'library'
        && grant.resource_id === library.resource_id && grant.principal_kind === 'user');
      const currentByUser = new Map(current.map((grant) => [grant.principal_id, grant]));
      const grantedReaders = readerUsers.filter((user) => currentByUser.get(user.user_id)?.allowed === true).length;
      const card = document.createElement('details'); card.className = 'grant-library-card';
      const header = document.createElement('summary'); header.className = 'grant-library-summary';
      const title = document.createElement('h3'); title.textContent = library.display_name || library.resource_id;
      title.title = `资料库编号：${library.resource_id}`;
      const count = document.createElement('span'); count.className = 'grant-library-count';
      count.textContent = `${grantedReaders} 人已授权`;
      const editHint = document.createElement('span'); editHint.className = 'grant-edit-hint'; editHint.textContent = '编辑授权';
      const chevron = document.createElement('span'); chevron.className = 'grant-chevron'; chevron.setAttribute('aria-hidden', 'true');
      header.append(title, count, editHint, chevron);
      card.append(header);

      const checklist = document.createElement('div'); checklist.className = 'grant-user-checklist';
      const checkboxRows = [];
      for (const user of readerUsers) {
        const existing = currentByUser.get(user.user_id);
        const allowed = existing?.allowed === true;
        const row = document.createElement('label'); row.className = 'grant-user-checkbox-row';
        const checkbox = document.createElement('input'); checkbox.type = 'checkbox'; checkbox.checked = allowed;
        checkbox.value = user.user_id;
        checkbox.disabled = !user.active && !allowed;
        checkbox.setAttribute('aria-label', `${user.username}，${user.active ? '启用' : '停用'}`);
        const name = document.createElement('span'); name.className = 'grant-user-name'; name.textContent = user.username;
        const status = document.createElement('small'); status.className = user.active ? 'grant-user-status active' : 'grant-user-status inactive';
        status.textContent = user.active ? '启用' : (allowed ? '停用 · 可撤销' : '停用');
        row.append(checkbox, name, status); checklist.append(row);
        checkboxRows.push({ user, checkbox, existing, initial: allowed });
        checkbox.addEventListener('change', () => {
          if (!user.active && !allowed && checkbox.checked) {
            checkbox.checked = false;
            $('#identity-grants-status').textContent = `“${user.username}”账号已停用，不能新增读取授权；启用后可再授权。`;
          }
          updateGrantButtons();
        });
      }
      if (!readerUsers.length) checklist.append(Object.assign(document.createElement('p'), {className:'privacy-note',textContent:'当前没有普通用户账号。'}));
      const unknown = current.filter((grant) => !knownUsers.has(grant.principal_id));
      if (unknown.length) {
        const unknownDetails = document.createElement('details'); unknownDetails.className = 'grant-unknown-details';
        const unknownSummary = document.createElement('summary');
        unknownSummary.textContent = `${unknown.length} 条账号目录中未匹配的授权；本次不会修改`;
        const unknownList = document.createElement('ul');
        for (const grant of unknown) {
          const item = document.createElement('li');
          item.textContent = `${grant.principal_id} · ${grant.allowed ? '允许读取' : '显式拒绝'}（保留）`;
          unknownList.append(item);
        }
        unknownDetails.append(unknownSummary, unknownList); checklist.append(unknownDetails);
      }
      card.append(checklist);

      const actions = document.createElement('div'); actions.className = 'grant-editor-actions';
      const save = document.createElement('button'); save.type = 'button'; save.className = 'button button-primary'; save.textContent = '保存修改';
      const cancel = document.createElement('button'); cancel.type = 'button'; cancel.className = 'button button-secondary'; cancel.textContent = '取消';
      const hasChanges = () => checkboxRows.some(({ checkbox, initial }) => checkbox.checked !== initial);
      const updateGrantButtons = () => { save.disabled = !hasChanges(); };
      updateGrantButtons();
      cancel.addEventListener('click', () => {
        for (const { checkbox, initial } of checkboxRows) checkbox.checked = initial;
        updateGrantButtons();
        $('#identity-grants-status').textContent = '已恢复为上次读取的服务端授权状态。';
      });
      save.addEventListener('click', async () => {
        if (save.disabled) return;
        const changes = checkboxRows.filter(({ checkbox, initial }) => checkbox.checked !== initial);
        if (!changes.length) return;
        const operationGeneration = state.generation;
        const stillCurrent = () => operationGeneration === state.generation && state.user?.role === 'admin';
        const failures = [], changed = [];
        let refreshFailed = false;
        for (const button of [save, cancel]) button.disabled = true;
        for (const { checkbox } of checkboxRows) checkbox.disabled = true;
        try {
          for (const { user, checkbox, existing } of changes) {
            if (!stillCurrent()) return;
            const allowed = checkbox.checked;
            if (allowed && !user.active) {
              failures.push(`${user.username}：账号已停用，不能新增授权`);
              continue;
            }
            try {
              await request('/api/admin/grants', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({resource_kind:'library',resource_id:library.resource_id,user_id:user.user_id,allowed,expected_revision:existing?.revision ?? 0})});
              if (!stillCurrent()) return;
              changed.push({user_id:user.user_id, allowed});
              window.dispatchEvent(new CustomEvent('wenjin-grants-changed', {detail:{user_ids:[user.user_id], library_id:library.resource_id, allowed}}));
            } catch (error) {
              if (error.stale) {
                // 账号已切换时不再发布旧账号授权详情；已提交的迟到写入只要求新账号刷新目录。
                if (error.responseOk) window.dispatchEvent(new CustomEvent('wenjin-access-catalog-invalidated'));
                return;
              }
              if (!stillCurrent()) return;
              if (error.status === 401) {
                failures.push(`${user.username}：登录会话已失效，请重新登录后刷新授权清单`);
                break;
              }
              failures.push(`${user.username}：${error.status === 409 ? '授权已被其他管理员更新，请刷新后核对' : error.message || '保存失败'}`);
              if (!error.status || error.status === 0) break;
            }
          }
          if (!stillCurrent()) return;
          let reloadError = null;
          try { await loadAdmin(); } catch (error) { reloadError = error; }
          if (!stillCurrent()) return;
          if (reloadError) {
            refreshFailed = true;
            $('#identity-grants-status').textContent = `授权操作已结束，但未能重新读取服务端状态（${reloadError.message}）；当前清单已锁定，请刷新后再编辑。`;
            for (const { checkbox } of checkboxRows) checkbox.disabled = true;
            save.disabled = true; cancel.disabled = true;
          } else if (failures.length) {
            $('#identity-grants-status').textContent = `部分授权未保存：${failures.join('；')}。已重新读取服务端当前状态。`;
          } else {
            $('#identity-grants-status').textContent = changed.length
              ? `已保存 ${changed.length} 项普通用户授权修改，并刷新了服务端状态。`
              : '没有需要保存的授权修改。';
          }
        } finally {
          if (stillCurrent() && !refreshFailed) {
            for (const { user, checkbox, initial } of checkboxRows) checkbox.disabled = !user.active && !initial;
            cancel.disabled = false;
            updateGrantButtons();
          }
        }
      });
      actions.append(save, cancel);
      card.append(actions);
      grantHost.append(card);
    }
    if (!libraryRows.length) grantHost.append(Object.assign(document.createElement('p'), {className:'privacy-note',textContent:'当前没有活动资料库资源。'}));
    const auditHost = $('#identity-audit'); auditHost.replaceChildren();
    const auditTable = document.createElement('table'); auditTable.className = 'data-table';
    auditTable.innerHTML = '<thead><tr><th>时间</th><th>操作者</th><th>动作</th><th>目标</th><th>结果/摘要</th></tr></thead>';
    const auditRows = document.createElement('tbody');
    for (const event of auditBody.events || []) {
      const tr = document.createElement('tr');
      const actor = event.actor_user_id ? (users.find((user) => user.user_id === event.actor_user_id)?.username || `已删除账号（${event.actor_user_id}）`) : '系统';
      tr.innerHTML = `<td>${h(event.occurred_at)}</td><td>${h(actor)}</td><td title="${h(event.action)}">${h(identityActionLabel(event.action))}<small class="job-id">${h(event.action)}</small></td><td>${h(event.target_kind)}:${h(event.target_id || '')}</td><td><code>${h(JSON.stringify(event.metadata || {}))}</code></td>`;
      auditRows.append(tr);
    }
    auditTable.append(auditRows); auditHost.append(auditTable);
  }

  async function initialize() {
    const mode = await request('/api/auth/mode'); state.mode = mode.mode;
    state.sessionPolicy = mode.session_policy || null;
    renderSessionPolicy();
    if (state.mode !== 'accounts') {
      document.querySelectorAll('[data-page="identity"]').forEach((item) => item.classList.add('hidden'));
      $('#page-identity').classList.add('hidden');
      return state;
    }
    if (mode.bootstrap_open) $('#identity-login-hint').textContent = '尚未创建管理员。请在服务主机运行 `python -m wenjin.file_management bootstrap-admin --identity-db <绝对路径> --username <用户名>`。';
    try {
      const me = await request('/api/auth/me');
      await refreshCsrf();
      if (me.user.must_change_password) {
        state.pendingPasswordUser = me.user;
        await forcedPasswordPrompt();
      } else setUser(me.user);
    } catch {
      clearState();
      await loginPrompt();
    }
    $('#credential-form').onsubmit = async (event) => {
      event.preventDefault();
      if (state.personalCredentialPending || !state.user) return;
      const formElement = event.currentTarget; const form = new FormData(formElement);
      const button = formElement.querySelector('button'); const generation = state.generation;
      state.personalCredentialPending = true; button.disabled = true;
      $('#credential-status').textContent = '正在签发个人 MCP 凭据…';
      try {
        const result = await request('/api/auth/credentials', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({label:form.get('label'),expires_in_seconds:Number(form.get('expires')) || null})});
        if (generation !== state.generation || !state.user) return;
        showCredentialDelivery('credential', result.credential, state.user.username, form.get('label'));
        formElement.reset();
        const loaded = await loadCredentials();
        if (generation !== state.generation) return;
        $('#credential-status').textContent = loaded
          ? '凭据已签发，列表已刷新。完整凭据只显示在上方交付区。'
          : '凭据已签发，但列表刷新失败；上方的一次性交付区仍保留完整凭据，可点“刷新列表”重试。';
      } catch (error) {
        if (!error.stale && generation === state.generation) $('#credential-status').textContent = `签发失败：${error.message}`;
      } finally {
        if (generation === state.generation) { state.personalCredentialPending = false; button.disabled = false; }
      }
    };
    $('#credential-copy').onclick = () => copyCredential('credential');
    $('#credential-hide').onclick = () => clearCredentialDelivery('credential');
    $('#credential-refresh').onclick = async () => {
      const button = $('#credential-refresh'); button.disabled = true;
      await loadCredentials();
      if (state.user) button.disabled = false;
    };
    $('#admin-credential-user').addEventListener('change', () => {
      state.adminCredentialSelectionRevision += 1;
      state.adminCredentialListRevision += 1;
      clearCredentialDelivery('admin-credential');
      $('#admin-credential-status').textContent = '';
      $('#admin-user-credentials').replaceChildren();
      updateAdminCredentialTarget();
      if ($('#admin-credential-user').value) loadAdminCredentials().catch(() => {});
    });
    $('#admin-credential-form').onsubmit = async (event) => {
      event.preventDefault();
      if (state.adminCredentialPending || state.user?.role !== 'admin') return;
      const userId = $('#admin-credential-user').value;
      const user = state.users.find((item) => item.user_id === userId);
      if (!user || !user.active || user.must_change_password) { updateAdminCredentialTarget(); return; }
      const formElement = event.currentTarget; const form = new FormData(formElement);
      const button = formElement.querySelector('button');
      const generation = state.generation; const selectionRevision = state.adminCredentialSelectionRevision;
      state.adminCredentialPending = true; $('#admin-credential-user').disabled = true; updateAdminCredentialTarget();
      $('#admin-credential-status').textContent = `正在为 ${user.username} 签发 MCP 凭据…`;
      try {
        const result = await request(`/api/admin/users/${encodeURIComponent(userId)}/credentials`, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({label:form.get('label'),expires_in_seconds:Number(form.get('expires')) || null})});
        if (generation !== state.generation || state.user?.role !== 'admin') return;
        if (selectionRevision !== state.adminCredentialSelectionRevision || $('#admin-credential-user').value !== userId) {
          $('#admin-credential-status').textContent = '签发请求已结束，但所属用户已切换，出于安全原因未显示完整凭据。如需确认，请回到原用户列表检查并按需撤销，再重新签发。';
          return;
        }
        showCredentialDelivery('admin-credential', result.credential, user.username, form.get('label'));
        formElement.reset();
        const loaded = await loadAdminCredentials(userId);
        if (generation !== state.generation || selectionRevision !== state.adminCredentialSelectionRevision || $('#admin-credential-user').value !== userId) return;
        $('#admin-credential-status').textContent = loaded
          ? `已为 ${user.username} 签发凭据，列表已刷新。完整凭据只显示在上方交付区。`
          : `已为 ${user.username} 签发凭据，但列表刷新失败；上方的一次性交付区仍保留完整凭据，可点“刷新列表”重试。`;
      } catch (error) {
        if (!error.stale && generation === state.generation && selectionRevision === state.adminCredentialSelectionRevision) {
          $('#admin-credential-status').textContent = `签发失败：${error.message}`;
          if (error.status === 403 || error.status === 409) {
            $('#admin-credential-target-note').textContent = `${user.username}当前不可签发。请检查账号是否停用或仍需完成首次密码修改；界面不会绕过服务端限制。`;
          }
        }
      } finally {
        if (generation === state.generation) { state.adminCredentialPending = false; $('#admin-credential-user').disabled = false; updateAdminCredentialTarget(); }
      }
    };
    $('#admin-credential-copy').onclick = () => copyCredential('admin-credential');
    $('#admin-credential-hide').onclick = () => clearCredentialDelivery('admin-credential');
    $('#admin-credential-refresh').onclick = () => loadAdminCredentials().catch(() => {});
    $('#identity-logout').onclick = signOut;
    $('#identity-force-logout').onclick = signOut;
    $('#identity-password-form').addEventListener('submit', (event) => { event.currentTarget.elements.new_password.setCustomValidity(''); });
    document.querySelectorAll('[data-page="identity"]').forEach((button) => button.addEventListener('click', () => {
      loadCredentials().catch((error) => { $('#my-credentials').textContent = error.message; });
      loadAdmin().catch((error) => { $('#identity-audit').textContent = error.message; });
      if ($('#admin-credential-user').value) loadAdminCredentials().catch(() => {});
    }));
    window.addEventListener('wenjin-managed-library-created', () => {
      if (state.user?.role === 'admin') loadAdmin().catch((error) => { $('#identity-grants-status').textContent = error.message; });
    });
    await loadCredentials();
    if (state.user?.role === 'admin') await loadAdmin();
    return state;
  }

  window.WenjinIdentity = {
    ready: null,
    get mode() { return state.mode; },
    get user() { return state.user; },
    get csrf() { return state.csrf; },
    get users() { return state.users; },
    get usersLoaded() { return state.usersLoaded; },
    usernameFor(userId) {
      const user = state.users.find((item) => item.user_id === userId);
      if (user) return user.username;
      return state.usersLoaded ? `已删除账号（${userId}）` : `账号名称尚未读取（${userId}）`;
    },
    get sessionPolicy() { return state.sessionPolicy; },
    setClearPageState(callback) { state.clearPageState = callback; },
    signOut,
    switchAccount: signOut,
    async unauthorized() {
      if (state.reauth) return state.reauth;
      state.reauth = (async () => {
        clearState();
        await loginPrompt();
        await refreshCsrf();
        await loadCredentials();
        if (state.user?.role === 'admin') await loadAdmin();
      })();
      try { await state.reauth; } finally { state.reauth = null; }
    },
  };
  window.WenjinIdentity.ready = initialize().catch((error) => { console.error('identity init failed', error); return state; });
})();
