# 独立测试记录

## 2026-10-14：集成源码与合同复验

- 测试对象：集成 worktree `codex/integrate-mcp-stateless`，基线 main `90a7e1f`；新版来源 commit `20f50b7`，开发兼容修复冻结于 `39d348e`。本轮文档快照后工作树 HEAD 为 `a5acdc4`（后续 `5c2a9f2`、`a5acdc4` 仅恢复/标注历史文档，没有改 `src/`）；测试源码身份固定为集成 `src/`。本轮 `git hash-object` 得到 `stateless_server.py=cce87b36b92699dc243ce5d16e571c7e60a1a6df`、`stateless_tools.py=91685da0d5e926bf053c950c03cd15abc8c44e03`、`stateless_identity.py=1e59503f97d280d5f701c39d4fd21f7a893ab731`。main `src/`、服务、数据库、语料和索引未改。
- 依赖环境：在 `mcp-stateless/.venv` 使用 `uv sync --locked --project mcp-stateless`，Python `3.12.3`、FastMCP `4.1.0`、MCP SDK / `mcp-types` `2.3.0`，安装锁定 runtime 依赖136项。清华镜像首次遇到 SQLAlchemy wheel 下载128.2秒超时，随后出现 wheel streaming `Invalid zip file structure` 重试；设置 `UV_CONCURRENT_DOWNLOADS=2`、`UV_HTTP_TIMEOUT=120`、`UV_HTTP_RETRIES=8` 后锁定同步完成。用 `uv pip install --python mcp-stateless/.venv/Scripts/python.exe --index-url https://pypi.org/simple pytest` 仅为该临时环境补 pytest 9.1.1，未改 `mcp-stateless/uv.lock`、main `uv.lock` 或 main 环境。客户端在隔离目录运行 `npm ci`，锁定 `@modelcontextprotocol/client` 2.3.1。
- 新增合同：`tests/test_mcp_stateless_current_contracts.py` 检查 FastMCP typed `ToolResult` 成功结果的协议/取件路径翻译，三类 queue/read-timeout 结构化错误语义；再通过新版 `/mcp-stateless` 实际 HTTP JSON-RPC `tools/call` 调用正式 `register_tools.search_docs`，fake retrieval service 注入 `ModelProviderError(model_queue_full/model_queue_timeout)`，验证响应 `isError=true`、错误码/detail、`evidence_status=not_retrieved` 与新版协议标签。没有调用真实模型或GPU。
- WAL 身份合同：在临时 SQLite 中通过现有 `IdentityStore` 建 WAL 库并持有 writer 产生 `-wal`/`-shm` sidecars；`StatelessIdentityStore` 对已提交记录可读，逐连接 `query_only=1`，DDL 被拒，读请求前后主库 SHA-256 一致，`initialize()` 被拒。测试数据由 pytest `tmp_path` 管理。
- 集成验证命令：在集成 worktree 设置 `PYTHONPATH=<integration-worktree>/src`、`MCP_MODERN_PYTHON=<integration-worktree>/mcp-stateless/.venv/Scripts/python.exe`，运行：`python -m pytest -q tests/test_mcp_stateless_current_contracts.py tests/test_mcp_stateless_protocol.py tests/test_mcp_stateless_identity_release.py tests/test_mcp_stateless_era_isolation.py`。结果 **16 passed，6 subtests passed，4 warnings，0 skipped**。4个 warning 是合成 Qdrant fixture 使用 `recreate_collection` 的上游弃用提示。identity/release合同通过 Node 官方 TS SDK 2.3.1 的真实 loopback网络discover/list/call，覆盖Alice/Bob个人凭据、ACL即时撤销、credential revoke、shared release A/B绑定、managed版本替换与下载源SHA。
- 新旧协议隔离：上述 `test_mcp_stateless_era_isolation.py` 使用集成现代环境和主目录既有 legacy环境，**1项已包含在16项结果中**；现代SDK探测旧 `/mcp` 不回退 `initialize`，旧SDK `initialize` 不获新版 `/mcp-stateless` 接受。主目录 legacy依赖仅只读使用，版本为 FastMCP `3.4.4` / MCP SDK `1.29.0`。
- 受影响主分支合同：从 main `E:\日常任务\rag-project\.venv\Scripts\python.exe`（FastMCP `3.4.4` / MCP SDK `1.29.0`）定向执行下列 pytest 节点；7个节点因其中一个参数化合同展开为8项，结果 **8 passed，5 warnings**。未运行全量历史测试或冻结评测。

  ```powershell
  $py = 'E:\日常任务\rag-project\.venv\Scripts\python.exe'
  & $py -m pytest -q `
    tests/test_model_api_admission_contract.py::test_model_api_queue_full_and_timeout_are_explicit_http_and_provider_errors `
    tests/test_model_api_admission_contract.py::test_managed_http_model_errors_reach_mcp_as_typed_tool_errors `
    tests/test_model_api_admission_contract.py::test_managed_bridge_read_timeout_reaches_mcp_as_typed_tool_error `
    tests/test_model_api_admission_contract.py::test_fastmcp_streamable_http_wire_sets_is_error_and_structured_code `
    tests/test_identity_http.py::test_identity_http_scopes_auth_grants_and_revocation_across_admin_and_mcp_routes `
    tests/test_identity_store.py::test_reader_can_revoke_only_own_credentials_and_logout_is_scoped `
    tests/test_identity_store.py::test_password_reset_and_disable_revoke_existing_sessions_and_mcp_tokens
  ```

  执行目录为 main repo 根；上述环境的 Python 解释器显式为 `E:\日常任务\rag-project\.venv\Scripts\python.exe`。
- 修复的测试夹具问题：第一轮新合同总组有1项失败，原因是 synthetic `Settings` fixture 漏设必需 `QDRANT_API_KEY`；补 synthetic 值后该组通过。随后增加真实 HTTP wire 和 WAL-sidecar 合同，首次单测因遗漏 `io` import 失败；补 import 后新合同模块 **8 passed**，并以最终组合命令重新验收。未发现业务代码回归。
- 清理核验：FastAPI/uvicorn 合成loopback服务在 `finally` 中停止，TestClient关闭，Node SDK客户端为子进程；测试进程结束后没有遗留 credential environment variable，credential 值未写入报告。pytest 默认保留 `tmp_path`，所以此前“删除合成SQLite/发布索引”的记录不准确。只读检查 `C:\Users\Onebiter\AppData\Local\Temp\pytest-of-Onebiter` 时，`pytest-1129`、`pytest-1130`、`pytest-1131` 下仍有本轮合同使用的 `test_stateless_identity_adapte0\identity.sqlite`（每个目录1个 synthetic DB）；本轮搜索 handler 的 `test_stateless_search_handler_0/_1` 目录为空。相同的测试临时目录名和DB也出现在 `pytest-current` 下。没有清理 pytest 共享目录或这些保留文件，避免触碰其他测试轮次资源；该残留是合成身份库，不包含真实凭据或业务数据。隔离 `mcp-stateless/.venv` 与 SDK `node_modules` 保留在集成 worktree 供复验，属于依赖目录且被忽略，不是运行数据。
- 验收边界：全部输入、token、数据库、HTTP服务和索引均为临时合成对象。未运行旧缓存Docker镜像、容器TLS、新镜像构建、GPU、真实服务/资料/身份库或生产验收；不把2026-10-09历史容器结果当作本轮证据。
