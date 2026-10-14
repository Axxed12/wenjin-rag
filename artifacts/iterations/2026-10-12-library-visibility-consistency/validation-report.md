# 资料库分类与读取权限独立验收

日期：2026-10-13  
范围：临时 SQLite 身份库、合成私域索引、内存 Qdrant、Starlette TestClient；未读取或修改真实授权、服务状态或正式资料。

## 结论

资料库的 `public/private` 分类目前是身份库中的展示元数据，不参与授权计算。写入分类使用 revision CAS，并记录身份审计；不创建或修改 grant。reader 即使面对 `public` 分类也必须先获得逐库 grant；得到 grant 后，分类为 `private` 仍能检索和读取原文。活动有效资源上的 admin 无需 grant 即可读取；资源不活动时，即使 caller 的角色是 admin，访问快照也不放行。

目录 API 对 admin 展示活动与非活动来源的管理元数据，并通过 `readable/searchable` 标明是否可以实际读取；reader 目录只展示允许访问的来源。新实现没有增加私域 `create_router` 的候选目标集合。现有原生 MCP 账号模式对已登记的有效活动库使用 IdentityStore 当前角色与授权快照：reader 仍按 grant，admin 遵循管理员可读所有有效库规则；这属于授权身份域统一，不是扩大未迁移的私域 route。

## 检查与证据

代码只读审查了 `src/wenjin/identity/store.py`、`src/wenjin/management_http/identity_http.py`、`src/wenjin/management_http/app.py`、`src/wenjin/retrieval/legacy_catalog.py`、`src/wenjin/mcp/tools.py` 和 `src/wenjin/mcp/download.py`。重点核对分类表独立存储、CAS/管理员身份检查、每请求重新读取身份/资源/grant、admin bypass 先受 active resource 限制，以及 HTTP/MCP/search/download 的授权入口。

定向命令：

```text
uv run pytest -q tests/test_library_visibility_consistency_contract.py tests/test_private_catalog_identity.py tests/test_identity_store.py tests/test_identity_http.py tests/test_mcp_identity_http.py tests/test_mcp_release_identity_tcp.py
```

结果：`22 passed, 14 warnings`（20.39s）。warnings 是 Starlette `BlockingPortal` 和 Qdrant `recreate_collection` 弃用提示。首轮新写的快照合同测试将两条资源都登记为 active，因而自身的 inactive 断言失败；修正临时 fixture 后同一完整定向组通过，业务代码未因此修改。

```text
uv run ruff check tests/test_library_visibility_consistency_contract.py
uv run ruff format --check tests/test_library_visibility_consistency_contract.py
```

结果：两项通过。

本轮新增窄合同 `tests/test_library_visibility_consistency_contract.py` 使用临时数据库覆盖：reader 和首次登录必须改密的 admin 不能分类；停用 admin 的旧 Principal 不能分类；已停用账号、必须改密账号读取时被拒；admin 降为 reader 后旧 Principal 的下一请求按新角色重新计算；root admin 读取 inactive resource 被拒。

复用的隔离 HTTP/服务合同 `tests/test_private_catalog_identity.py` 覆盖：无 grant reader 无法看到私域目录、检索及读取正文；先改成 `public` 仍不放行，grant 后改成 `private` 仍可读；无 grant admin 能读取有效私域索引的管理 HTTP 搜索与正文，并用个人 MCP 凭据通过目录、搜索和正文读取；撤权后 reader 受限。该文件同时验证裸 target、无效 route schema、目标歧义以及 dense collection 缺失时，私域来源不会进入 active/searchable 状态。`tests/test_identity_store.py` 覆盖分类审计、grant 集不变、CAS 冲突、admin active-resource 读取与 inactive-resource 拒绝、schema v1 到 v2 迁移。

## 未覆盖范围

- 无效索引的 route/collection 失效由 legacy catalog 层合同证明不活动；本轮没有额外搭建完整管理 HTTP 栈来对无效索引再做一次 admin search/download 请求。HTTP 实际读取拒绝依赖 `synchronize_resources()` 只把 `is_active && searchable` 的 shared source 登记为 active，再由 IdentityStore 在请求中 fail closed。
- 原生 MCP 的 MCP 2025-11-25 TCP initialize/session 生命周期未在本轮新增运行；复用的 `tests/test_mcp_identity_http.py` 与 `tests/test_mcp_release_identity_tcp.py` 定向组通过，其中前者覆盖账号身份桥接，后者覆盖状态化 TCP 路由、grant 撤销和原文访问。本轮新加的 admin 私域检索/正文断言是同进程 HTTP MCP bridge，不等于新增原生工具路由集合。
- 未运行完整回归、冻结题集、真实服务或浏览器验收。Qdrant 是进程内 `:memory:` 客户端，资料库与账号均为 pytest 临时合成数据。
