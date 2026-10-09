# 新版无状态 MCP 交付与验收汇总

本轮为2026-10-09个人迭代，位于独立worktree分支`codex/mcp-stateless-20261009`；不是2026-07-15之前已经具备的能力。旧入口`/mcp`、主依赖及RAG/身份/管理核心保持接手基线。新增入口`/mcp-stateless`，独立FastMCP4.1.0/Python SDK2.3.0环境；客户端为官方TypeScript Client2.3.1，固定2026-07-28，不自动降级。

| 批次 | 已取得的实际证据 | 状态/边界 |
|---|---|---|
| 一 | [方案和官方依据](phase-1-plan.md)、[实际resolver/API probe](dependency-evidence/fastmcp-sdk-compatibility.md) | 完成；FastMCP3/SDK2不能同环境，选独立进程 |
| 二 | [官方SDK实际TCP discover/list/call](sdk2-network-phase2.json)、[协议错误合同](phase2-3-targeted-tests.txt) | 完成；使用合成目录/身份，无真实模型 |
| 三 | [身份、授权、检索、取件和版本合同](sdk2-identity-release-phase3.json)、[新旧SDK/入口隔离](protocol-era-isolation.json) | 完成；真实管理HTTP桥、临时账户、合成向量；旧SDK初始化/调用保持可用 |
| 四 | [构建身份](dependency-evidence/build-log.md)、[Compose/Caddy配置检查](dependency-evidence/deployment-config-validation.md)、[正式CLI/容器HTTPS SDK检索](docker-launcher-tls-runtime.json) | 完成；取件字节/hash/no-store、WAL首次鉴权/query_only拒写/热撤权、管理Cookie/CSRF均通过 |

最终镜像为`wenjin-mcp-stateless:2026-10-09`，ID `sha256:1f00cf6f96f22b68302845bad5eb8dbd2e44e976c720edf5029d9849d709fa62`。正式CLI最终联验实际访问独立Qdrant和合成remote-model API，embedding/rerank各一次；该证据证明调用链，不证明真实模型效果。最终定向协议文件6 passed/6 subtests，正式容器合同1 passed；新旧SDK隔离、第三批合成业务链及定向lint/旧环境collection检查分别记录，不累计为检索准确率。

正式容器样例使用index-registry target，取件URL不含release_id；immutable release A/B绑定由第三批release-catalog合同证明。Caddy加载本轮真实部署配置，旧路径只验证合成upstream转发，不声称旧协议TLS握手已联验；旧SDK的真实初始化/session/list/call由独立TCP合同证明。

协议依赖不能单独保证项目合同：SDK对非字符串protocolVersion的错误码需要本项目局部校验；新版factory拒绝无凭据配置。WAL身份库filesystem只读的[失败证据](docker-tls-wal-readonly-failure.json)已保留，新版[query_only适配](dependency-evidence/wal-query-only-probe.json)禁止应用身份SQL写入，仅允许SQLite维护sidecar文件，不切换数据库journal mode或使用immutable读取热授权库。

启动、切换和回退操作见[操作说明](../../../docs/mcp-stateless.md)。问题、原因、处理和未完成项见[开发记录](development-log.md)、[实现记录](implementation-log.md)、[独立测试记录](test-development-log.md)与[审核记录](review-log.md)。[原目录/核心/文档链接检查](root-review-checks.json)与[临时环境清理记录](cleanup-evidence.md)单独列出。

没有执行真实模型、真实语料、冻结评测、WAN/云主机、GUI客户端或生产部署验收。原开发目录未切换/reset/stash，未修改实际账户、授权、数据库、索引或运行服务。已有三个临时SQLite目录因自动审批拒绝删除而保留，不能声称全部临时文件已清理。
