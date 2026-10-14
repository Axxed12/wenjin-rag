# MCP 凭据 UI 与后台独立验收

日期：2026-10-14

## 结论

隔离后端合同与最终 Node 合同通过。后台个人/管理员代 reader 签发、reader 身份不提升、跨用户权限、立即撤销和到期拒绝符合当前实现；凭据列表只返回安全元数据，持久化保存 token hash，不支持重显原 token。UI 合同确认一次性交付、北京时间显示和刷新失败时保留交付 token。

## 后端实现审查

- `/api/auth/credentials` 仅当前账号列出/签发；`/api/admin/users/{user_id}/credentials` 与 `/api/admin/credentials/{credential_id}` 由管理员路由保护。reader 跨用户列出/撤销由 store 拒绝。
- 签发要求活跃账号；目标账号必须先完成首次改密。个人签发绑定当前用户，管理员代签按目标用户当前 role 解析，不附加签发者管理员能力。有效期限定 60 秒至 365 天，过期判断为 `now >= expires_at`。
- 撤销、账号停用和密码重置会使对应 MCP 凭据无法解析。列表仅投影 `credential_id/token_prefix/user_id/label/created_at/expires_at/revoked_at`，不含原 token/hash；`IssuedCredential.token` 只在签发响应可见，并配置为 repr 隐藏。

## 验证证据

后端隔离定向命令：

```text
uv run pytest -q tests/test_identity_store.py::test_web_and_mcp_tokens_are_separate_and_both_expire tests/test_identity_store.py::test_password_reset_and_disable_revoke_existing_sessions_and_mcp_tokens tests/test_identity_store.py::test_issued_secret_is_not_repr_list_or_audit_output tests/test_identity_store.py::test_reader_can_revoke_only_own_credentials_and_logout_is_scoped tests/test_admin_issued_reader_mcp_contract.py::test_admin_issued_reader_credential_keeps_reader_identity_and_live_grants tests/test_identity_http.py::test_identity_http_scopes_auth_grants_and_revocation_across_admin_and_mcp_routes
```

结果：`6 passed, 6 warnings`。新增的 `tests/test_admin_issued_reader_mcp_contract.py` 使用合成身份和隔离 HTTP app，覆盖管理员代 reader 签发、reader 无 grant 拒绝/有 grant 成功、身份仍为 reader、管理员与 reader 列表安全字段、reader 跨用户列出/撤销拒绝、自行撤销后 MCP 请求 401。没有访问真实资料或真实身份数据库。

UI 定向命令：

```text
node --test tests/admin_credential_ui_behavior.test.mjs tests/admin_identity_auth_behavior.test.mjs tests/admin_frontend_catalog_refresh.test.mjs
```

结果：`40/40 passed`。凭据 UI 合同覆盖停用/待首次改密账号不可签发、按目标用户列出状态与北京时间、表格不包含完整 token、签发后列表刷新失败时交付区保留 token、无 Clipboard API 时选中供手动复制、隐藏后清空交付内容及撤销目标凭据。独立竞态合同验证迟到的管理员列表/签发响应不会回填切换后的用户或账号、reader 仅签发自己的凭据、列表刷新不重签。

Ruff：`uv run ruff check tests/test_admin_issued_reader_mcp_contract.py` 与 `uv run ruff format --check tests/test_admin_issued_reader_mcp_contract.py` 均通过。

早期新增 UI 测试曾因过早检查异步 DOM 渲染而失败；修正等待条件并加入独立竞态合同后，最终 40 项组合命令全部通过。

## 已知边界

如果目标用户或账号在服务端成功签发后、浏览器收到响应前切换，UI 会丢弃迟到的完整 token，不回填新账号/用户界面，也不会隐式撤销。服务端凭据此时可能仍存在但原 token 无法再取回；管理员需回原账号按列表前缀核对并按需撤销。这是本轮确认的产品行为边界，后端授权合同未失败。

本轮没有浏览器手工验收、没有连接运行中的服务、没有在真实身份库签发或撤销，也没有更改生产凭据或资料授权。
