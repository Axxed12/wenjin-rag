/* 账户登录与管理UI；CSRF值只保存在本页内存。 */
(() => {
  const state = { mode: 'legacy', user: null, csrf: null, clearPageState: null, ready: null, generation: 0, reauth: null, users: [], usersLoaded: false, sessionPolicy: null };
  const $ = (selector) => document.querySelector(selector);
  const h = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
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
    if (generation !== state.generation) throw Object.assign(new Error('账号状态已变化，已忽略旧请求。'), {status: 0, stale: true});
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
    state.user = null;
    state.csrf = null;
    state.users = [];
    state.usersLoaded = false;
    $('#identity-admin-panel').classList.add('hidden');
    $('#identity-self-panel').classList.add('hidden');
    document.querySelectorAll('[data-page="files"],[data-page="jobs"],[data-page="calls"],[data-page="overview"],[data-page="roadmap"],[data-page="releases"],[data-page="evaluations"]').forEach((item) => item.classList.add('hidden'));
    $('#identity-current-user').textContent = '';
    $('#identity-users').replaceChildren();
    $('#identity-grants').replaceChildren();
    $('#identity-audit').replaceChildren();
    $('#my-credentials').replaceChildren();
    $('#credential-reveal').replaceChildren();
    $('#credential-reveal').classList.add('hidden');
    if (state.clearPageState) state.clearPageState();
  }

  function setUser(user) {
    if (state.user?.user_id !== user.user_id) {
      state.users = [];
      state.usersLoaded = false;
    }
    state.generation += 1;
    state.user = user;
    $('#identity-self-panel').classList.remove('hidden');
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
    $('#identity-password-dialog').close();
    try { await request('/api/auth/logout', {method: 'POST'}); } catch {}
    clearState();
    await loginPrompt();
  }

  async function refreshCsrf() {
    const body = await request('/api/auth/csrf', {method: 'POST'});
    state.csrf = body.csrf_token;
  }

  async function loadCredentials() {
    const generation = state.generation;
    const body = await request('/api/auth/credentials');
    if (generation !== state.generation) return;
    const host = $('#my-credentials');
    host.replaceChildren();
    const table = document.createElement('table');
    table.className = 'data-table';
    table.innerHTML = '<thead><tr><th>名称</th><th>前缀</th><th>创建时间</th><th>有效期</th><th>状态</th><th></th></tr></thead>';
    const rows = document.createElement('tbody');
    for (const item of body.credentials || []) {
      const tr = document.createElement('tr');
      tr.innerHTML = `<td>${h(item.label)}</td><td><code>${h(item.token_prefix)}</code></td><td>${h(item.created_at)}</td><td>${h(item.expires_at || '不自动过期')}</td><td>${item.revoked_at ? '已撤销' : '有效'}</td><td></td>`;
      if (!item.revoked_at) {
        const button = document.createElement('button'); button.className = 'button button-secondary'; button.textContent = '撤销';
        button.onclick = async () => { await request(`/api/auth/credentials/${encodeURIComponent(item.credential_id)}`, {method: 'DELETE'}); await loadCredentials(); };
        tr.lastElementChild.append(button);
      }
      rows.append(tr);
    }
    table.append(rows); host.append(table);
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
      const creds = document.createElement('button'); creds.className = 'button button-secondary'; creds.textContent = 'MCP凭据';
      creds.onclick = async () => {
        const label = prompt(`为 ${user.username} 签发个人MCP凭据的名称`);
        if (label) {
          const issued = await request(`/api/admin/users/${user.user_id}/credentials`, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({label})});
          alert(`请立即保存此凭据（仅显示一次）：\n${issued.credential.token}`);
        }
        const listed = await request(`/api/admin/users/${user.user_id}/credentials`);
        const active = listed.credentials.filter((item) => !item.revoked_at);
        if (active.length) {
          const choice = prompt(`有效凭据：\n${active.map((item) => `${item.label} · ${item.token_prefix} · ${item.credential_id}`).join('\n')}\n输入要撤销的credential_id；留空取消`);
          if (choice && active.some((item) => item.credential_id === choice)) await request(`/api/admin/credentials/${encodeURIComponent(choice)}`, {method:'DELETE'});
        }
      };
      actions.append(role, active, rename, reset, creds); userRows.append(tr);
    }
    userTable.append(userRows); userHost.append(userTable);
    const create = $('#identity-create-user');
    create.onsubmit = async (event) => {
      event.preventDefault(); const form = new FormData(create);
      await request('/api/admin/users', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:form.get('username'),password:form.get('password'),role:form.get('role')})});
      create.reset(); await loadAdmin();
    };
    const libraryRows = (resourceBody.resources || []).filter((item) => item.resource_kind === 'library' && item.active);
    const grantHost = $('#identity-grants'); grantHost.replaceChildren();
    const grantTable = document.createElement('table'); grantTable.className = 'grant-table data-table';
    grantTable.innerHTML = '<thead><tr><th>逻辑资料库</th><th>用户授权</th></tr></thead>';
    const grantBody = document.createElement('tbody');
    for (const library of libraryRows) {
      const tableRow = document.createElement('tr');
      const libraryCell = document.createElement('td');
      const title = document.createElement('h3');
      title.textContent = library.display_name || library.resource_id;
      title.title = `资料库编号：${library.resource_id}`;
      title.setAttribute('aria-label', `资料库：${title.textContent}`);
      libraryCell.append(title);
      const permissionCell = document.createElement('td');
      const details = document.createElement('details'); details.className = 'grant-user-picker';
      const current = grants.filter((grant) => grant.resource_kind === 'library' && grant.resource_id === library.resource_id && grant.principal_kind === 'user');
      const allowedCount = current.filter((grant) => grant.allowed).length;
      const summary = document.createElement('summary'); summary.textContent = `管理用户授权 · ${allowedCount} 人已允许`;
      details.append(summary);
      const form = document.createElement('form'); form.className = 'identity-form grant-batch-form';
      const label = document.createElement('label'); label.textContent = '选择一个或多个用户';
      const select = document.createElement('select'); select.multiple = true; select.size = Math.min(6, Math.max(3, users.length)); select.setAttribute('aria-label', `为${library.resource_id}选择用户`);
      for (const user of users.filter((item) => item.active)) {
        const option = new Option(`${user.username}（${user.role === 'admin' ? '管理员' : '只读用户'}）`, user.user_id);
        select.add(option);
      }
      label.append(select);
      const grantButton = document.createElement('button'); grantButton.className = 'button button-primary'; grantButton.textContent = '授予读取';
      form.append(label, grantButton);
      const accessList = document.createElement('div'); accessList.className = 'grant-access-list';
      const knownUsers = new Map(users.map((user) => [user.user_id, user]));
      for (const grant of current) {
        const user = knownUsers.get(grant.principal_id);
        const row = document.createElement('div'); row.className = 'grant-access-row';
        const userLabel = user ? `${user.username}${user.role === 'admin' ? ' · 管理员' : ''}` : '已删除账号';
        const name = document.createElement('span'); name.textContent = userLabel;
        if (!user) { const detail = document.createElement('small'); detail.textContent = grant.principal_id; name.append(' ', detail); }
        const stateLabel = document.createElement('span'); stateLabel.className = grant.allowed ? 'grant-allowed' : 'grant-denied'; stateLabel.textContent = grant.allowed ? '允许读取' : '显式拒绝';
        row.append(name, stateLabel);
        if (user) {
          const change = document.createElement('button'); change.type = 'button'; change.className = 'button button-secondary';
          change.textContent = grant.allowed ? '撤销此权限' : '重新允许';
          change.addEventListener('click', async () => {
            const operationGeneration = state.generation;
            try {
              await request('/api/admin/grants', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({resource_kind:'library',resource_id:library.resource_id,user_id:user.user_id,allowed:!grant.allowed,expected_revision:grant.revision})});
              if (operationGeneration !== state.generation) return;
              window.dispatchEvent(new CustomEvent('wenjin-grants-changed', {detail:{user_ids:[user.user_id], library_id:library.resource_id, allowed:!grant.allowed}}));
            } catch (error) {
              if (operationGeneration === state.generation && error.status !== 0) $('#identity-grants-status').textContent = error.message;
            }
            if (operationGeneration === state.generation) await loadAdmin();
          });
          row.append(change);
        }
        accessList.append(row);
      }
      if (!current.length) accessList.append(Object.assign(document.createElement('p'), {className:'privacy-note',textContent:'目前没有已记录的用户授权。'}));
      form.addEventListener('submit', async (event) => {
        event.preventDefault();
        const selected = Array.from(select.selectedOptions, (option) => option.value);
        if (!selected.length) { $('#identity-grants-status').textContent = '请先选择至少一位用户。'; return; }
        const batchGeneration = state.generation;
        const stillCurrent = () => batchGeneration === state.generation && state.user?.role === 'admin';
        grantButton.disabled = true;
        const failures = [], changed = [];
        try {
          for (const userId of selected) {
            if (!stillCurrent()) return;
            const existing = current.find((grant) => grant.principal_id === userId);
            if (existing?.allowed) continue;
            const user = knownUsers.get(userId);
            try {
              await request('/api/admin/grants', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({resource_kind:'library',resource_id:library.resource_id,user_id:userId,allowed:true,expected_revision:existing?.revision ?? 0})});
              if (!stillCurrent()) return;
              changed.push(userId);
            } catch (error) {
              if (!stillCurrent() || error.status === 0 || error.stale || error.status === 401) return;
              failures.push(`${user?.username || '所选用户'}：${error.message}`);
            }
          }
          if (!stillCurrent()) return;
          window.dispatchEvent(new CustomEvent('wenjin-grants-changed', {detail:{user_ids:changed, library_id:library.resource_id}}));
          $('#identity-grants-status').textContent = failures.length
            ? `部分授权未保存：${failures.join('；')}。页面已重新读取服务端授权状态。`
            : changed.length ? `已为${changed.length}位用户授予读取权限。` : '所选用户已拥有此资料库读取权限。';
        } finally {
          grantButton.disabled = false;
          if (stillCurrent()) await loadAdmin();
        }
      });
      details.append(form, accessList);
      permissionCell.append(details);
      tableRow.append(libraryCell, permissionCell);
      grantBody.append(tableRow);
    }
    if (!libraryRows.length) grantHost.textContent = '当前没有活动资料库资源。';
    else { grantTable.append(grantBody); grantHost.append(grantTable); }
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
      event.preventDefault(); const form = new FormData(event.currentTarget);
      const result = await request('/api/auth/credentials', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({label:form.get('label'),expires_in_seconds:Number(form.get('expires')) || null})});
      $('#credential-reveal').textContent = result.credential.token;
      $('#credential-reveal').classList.remove('hidden'); event.currentTarget.reset(); await loadCredentials();
    };
    $('#identity-logout').onclick = signOut;
    $('#identity-force-logout').onclick = signOut;
    $('#identity-password-form').addEventListener('submit', (event) => { event.currentTarget.elements.new_password.setCustomValidity(''); });
    document.querySelectorAll('[data-page="identity"]').forEach((button) => button.addEventListener('click', () => {
      loadCredentials().catch((error) => { $('#my-credentials').textContent = error.message; });
      loadAdmin().catch((error) => { $('#identity-audit').textContent = error.message; });
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
