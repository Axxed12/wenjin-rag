# 新版无状态 MCP 启动、并存与回退

这是2026-10-09、技术叙事截止日2026-07-15之后的个人迭代。旧入口继续使用`2025-11-25`。新版实现与每批实测状态见[开发记录](../artifacts/iterations/2026-10-09-mcp-stateless/development-log.md)、[独立测试记录](../artifacts/iterations/2026-10-09-mcp-stateless/test-development-log.md)和[方案/验收清单](../artifacts/iterations/2026-10-09-mcp-stateless/phase-1-plan.md)。

## 版本与入口

| 项目 | 旧入口 | 新入口 |
|---|---|---|
| 协议 | 2025-11-25、initialize/initialized、会话 | 2026-07-28、逐请求meta、server/discover |
| Python SDK / 框架 | 主lock：mcp1.29.0 / FastMCP3.4.4 | 独立lock：mcp2.3.0 / FastMCP4.1.0 |
| 原生进程 | wenjin.mcp_server | wenjin.mcp.stateless_server |
| 默认路径 | /mcp | /mcp-stateless |
| 下载路径 | /files/... | /mcp-stateless/files/... |
| 默认新增端口 | 旧部署配置不变 | 18768；cloud仅私网expose |

独立依赖位于[mcp-stateless/pyproject.toml](../mcp-stateless/pyproject.toml)和[uv.lock](../mcp-stateless/uv.lock)。主依赖/旧服务不升级；FastMCP3与SDK2实际resolver冲突。新版启动会检查SDK/框架/协议身份。新入口限定现代协议；旧客户端继续连接旧地址。

新版每个请求的`params._meta`需要`io.modelcontextprotocol/protocolVersion="2026-07-28"`和`io.modelcontextprotocol/clientCapabilities`；HTTP还需要对应协议/方法/调用名称头。由官方SDK/框架处理发现、类型/能力/结果/HTTP元数据；薄入口gate约束新服务支持集，工具适配复用现有检索与取件逻辑。个人身份来自Bearer及IdentityStore，clientInfo/工具参数不作为身份。

## 原生启动

以下是操作者命令，执行前准备显式运行配置、已初始化且版本兼容的身份库、管理HTTP桥和匹配的只读索引/原文。服务不会创建身份库或账号。新版默认不自动读取项目`.env`；模型配置可通过进程环境或显式`--env-file`提供，监听/identity/bridge等启动变量由进程环境或CLI参数提供。

新版环境默认不安装torch/transformers/本地embedding adapter，使用现有模型API的remote配置：`WENJIN_MODEL_PROVIDER=remote`、模型URL/CA/token以及明确`EMBEDDING_MODEL_ID`/`RERANKER_MODEL_ID`，同时配置`QDRANT_URL`/`QDRANT_API_KEY`。模型/API职责与跨主机限制沿用[双部署说明](dual-deployment.md)。本轮没有验证真实模型或真实语料。

在仓库根执行：

```powershell
uv sync --project mcp-stateless --locked
.\scripts\run_mcp_stateless.ps1 --project-root C:\WenjinRuntime `
  --identity-db C:\WenjinRuntime\identity.sqlite `
  --host 127.0.0.1 --port 18768
```

`C:\WenjinRuntime`是需要操作者替换的运行根示例。配置`WENJIN_FILE_MANAGEMENT_URL`指向唯一管理HTTP；account mode桥接传本次个人Bearer，不给MCP管理token。管理页面登录Cookie/CSRF/session维持原行为。原生静态Bearer兼容模式需显式`WENJIN_MCP_API_KEY`与对应旧ACL；本轮账号验收以个人credential为主。

跨主机入口经过HTTPS。非loopback明文仅在部署者明确设置`WENJIN_MCP_ALLOW_PRIVATE_HTTP=1`时允许，表示选择私有代理网络；`FORWARDED_ALLOW_IPS`需精确可信代理地址，禁止`*`。管理桥私网HTTP有独立opt-in，见双部署说明。

## Docker 与 HTTPS 并存

新增[独立Dockerfile](../deploy/Dockerfile.mcp-stateless)、[Compose overlay](../deploy/compose.stateless.yaml)和[新版代理配置](../deploy/Caddyfile.stateless)。overlay叠加现有base+cloud profile，新增mcp-stateless进程；管理HTTP、Qdrant、旧MCP继续原职责。模型token由既有with-secrets读取，MCP不获取file_admin_token。数据/资料挂载只读。

身份卷需要允许SQLite维护WAL/SHM sidecar文件：实际验收发现WAL模式无sidecars时，整个身份目录filesystem只读会令鉴权失败。新版采用专用`query_only`连接禁止应用账户/授权写操作，不初始化或迁移身份库，身份挂载允许SQLite维护sidecars；这与资料/索引的filesystem只读不同。不为新版切换数据库journal mode，也不使用immutable缓存热身份库。该处理的最终复验状态见当轮记录。

复用独立部署环境配置，并从新增`deploy/.env.stateless.example`复制`deploy/.env.stateless`，设置外部Host allowlist及新版proxy地址。默认proxy子网`172.30.250.0/29`，edge/management/legacy/modern分别为`.2/.3/.4/.5`；冲突时同时调整整个地址组。cloud-edge先按专属前缀路由新版MCP/取件，再路由旧/mcp与/files，管理页兜底。

在计划部署的独立stack上先渲染并构建：

```powershell
docker compose --project-directory . --env-file deploy/.env.cloud `
  --env-file deploy/.env.stateless -p wenjin-cloud `
  -f deploy/compose.base.yaml -f deploy/compose.cloud.yaml `
  -f deploy/compose.stateless.yaml config --quiet
docker build -f deploy/Dockerfile.mcp-stateless `
  -t wenjin-mcp-stateless:2026-10-09 .
```

首次部署按双部署说明准备管理、Qdrant、模型节点与显式账户授权后，才启动整个新stack。为已有兼容stack增加新版时，核对挂载与secret身份，再用上述相同Compose参数执行`up -d --no-deps mcp-stateless cloud-edge`；避免重启其他服务。本文命令没有在现有运行stack执行。配置渲染/镜像构建成功分别记录，不能代替TLS/客户端/检索验收。

## 客户端切换与回退

客户端新版地址为`https://<PUBLIC_DOMAIN>/mcp-stateless`，旧版保持`https://<PUBLIC_DOMAIN>/mcp`。沿用当前个人MCP凭据与资料库grant；每次HTTP请求与下载重新核验。旧Mcp-Session-Id/Last-Event-ID在新版被忽略，不产生/回显会话，不用于跨用户主体选择。调用业务失败为isError=true；协议错误包含请求id、规范代码和HTTP状态。

独立验收客户端是官方TypeScript Client2.3.1，使用pin2026-07-28；[客户端说明](../tests/acceptance/mcp-stateless-client/README.md)及[第二批实际请求/响应](../artifacts/iterations/2026-10-09-mcp-stateless/sdk2-network-phase2.json)给出已执行证据。该harness限制loopback与合成短效凭据，HTTPS通过`NODE_EXTRA_CA_CERTS`显式信任临时CA。客户端产品GUI支持情况没有据此验收。

回退顺序：把客户端URL切回旧/mcp；停止新增mcp-stateless服务；用原base+cloud配置恢复cloud-edge原Caddyfile并仅重建代理容器。保留新旧服务数据与个人授权，不重建索引，不改schema，不回退主依赖；不执行`down -v`。新版依赖目录/镜像可保留供复查，回退不要求删除它们。

## 实测边界

四批实现与合成联验完成，见[交付汇总](../artifacts/iterations/2026-10-09-mcp-stateless/acceptance-summary.md)。独立依赖/API与真实loopback SDK三RPC/协议负例通过；第三批实际SDK检索、跨用户隔离、撤权、shared release固定和managed旧版本拒绝有输入输出。旧SDK继续初始化/调用，新旧客户端连接错误入口均失败，新客户端没有自动降级。

第四批使用正式镜像/CLI、临时Qdrant与合成remote embedding/rerank，经Caddy HTTPS由官方SDK搜索成功；取件200且字节/hash匹配、no-store；撤grant后下一次列表空/下载403，重授后恢复。WAL无sidecars首次鉴权、query_only拒写、无writer读段主库hash不变均通过。真实管理create_app经HTTPS保持Secure/HttpOnly Cookie，错误Origin/缺CSRF403，正确CSRF签发201。旧代理路径仅测试合成upstream转发，未在本批执行旧MCP的TLS握手。

第三批使用确定性向量器和关闭的重排器；第四批实际调用合成模型API的embedding/rerank。没有执行真实模型、真实语料、冻结评测、WAN/云主机、GUI客户端或生产验收。官方SDK对非字符串protocolVersion的默认错误码与规范不一致，本项目新版入口增加局部Malformed meta校验并已通过负例复验；不能将此项目适配当作SDK自身的完整一致性认证。临时容器/网络及最终联验目录已清理；早期三处临时SQLite文件因自动审批拒绝删除而保留，见[清理记录](../artifacts/iterations/2026-10-09-mcp-stateless/cleanup-evidence.md)。
