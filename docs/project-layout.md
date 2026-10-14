# 项目目录与结构重构状态

本文描述当前源码职责和 2026-10-07 四批重构状态。它不把目标结构写成已完成事实；每批验收标准见[范围与验收清单](../artifacts/iterations/2026-10-07-structure-refactor/scope.md)，实测及失败历史见[总体开发记录](../artifacts/iterations/2026-10-07-structure-refactor/development-log.md)和[入口开发记录](../artifacts/iterations/2026-10-07-structure-refactor/entry-development-log.md)。

## 当前职责入口

| 路径 | 当前职责 | 兼容与状态 |
|---|---|---|
| `src/wenjin/management_http/` | 管理 HTTP app 装配，以及 files/jobs/search/logs/assets/call_logs handler 和窄 HTTP 原语 | 权威实现；`file_management/api.py` 仍兼容导出 `create_app` 等公开入口；静态 UI 资源暂留 `file_management/admin/`。 |
| `src/wenjin/mcp/` | MCP runtime、工具、协议/transport server、取件槽位、managed HTTP client | 权威实现；`mcp_server.py` 保留旧公开导入及 `python -m` CLI 转发，`managed_mcp.py` 保留旧导入兼容。协议仍按 `2025-11-25` 状态化生命周期。 |
| `src/wenjin/file_management/` | 受管文件 service 协调、CLI、持久生命周期领域组件，以及 UI 静态资源 | `repository.py`、`jobs.py`、`versions.py`、`cleanup.py`、`clock.py` 承担拆出的领域职责；service 仍唯一创建/关闭 manager、connection、锁和事务。`api.py`、`call_logs.py` 为兼容层。 |
| `src/wenjin/ingestion/` | 正式包内文档加载入口和解析适配 | 根 `ingest.py` 保留兼容入口。安装态 wheel 从仓库外且无源码/editable finder 时验证导入与显式 manifest/raw-root Hash；不等于空环境依赖安装。 |
| `src/wenjin/retrieval/` | 检索和只读 legacy catalog 查询边界 | canonical `LegacySearchService` 与 `StaleSnapshotError` 位于此处；旧 `file_management.legacy_search`、models 异常导出保持兼容。 |
| `scripts/operations/`, `scripts/labs/`, `scripts/demos/`, `scripts/support/` | 日常操作、实验、演示及支撑脚本 | core 批次已完成分类，旧入口保留薄兼容；评测质量能力仍以 `evaluation/` 为专属边界。 |
| `evaluation/quality/`, `evaluation/experiments/` | 质量评测与策略/诊断实验 | 旧顶层入口保留兼容 shim；数据集、fixtures、Ragas 环境和历史 artifacts 留在原位置。 |
| `tests/acceptance/`, `tests/support/` | 外部验收入口与验收支撑工具 | 旧根路径保留需要的静态兼容 shim；`quick`/`integration`/`live` marker 映射 57 个测试模块。 |
| `artifacts/iterations/` | 每轮日志、验收数据、历史实验材料 | 旧 artifact 保留原路径；结构整理不搬运行数据，也不重写历史证据为当前轮重测。 |

## 四批进度

| 批次 | 目标 | 当前状态 |
|---|---|---|
| A：管理 UI | 隔离 helper + MarkerEmbedder 完成真实浏览器 upload→job→search→replace（检查旧 version 冲突和新版证据）→delete，且 390px 可操作无横向溢出 | **用户手工反馈确认本轮目录刷新结果**。用户先说“完整流程及390px都试过”，后明确确认“已确认新上传文件无需刷新就会出现在检索调试中”。这是用户反馈，非Agent观察且无新增截图；未把未逐项描述的409断言登记为实测。最新JS另有6项行为测试和隔离HTTP静态SHA证据，旧工具阻塞记录仍保留于[本轮开发记录](../artifacts/iterations/2026-10-07-upload-catalog-refresh/development-log.md)。 |
| B：HTTP/MCP | 职责拆分、旧公开导入/启动兼容、行为合同和安装资源复核 | 新包、兼容 wrapper 与定向回归完成；独立 HTTP handler review、隔离 stateful TCP 和已安装 wheel smoke 通过。TCP 使用 fake retrieval/临时 ACL 和数据，不证明检索质量；wheel 复用现有依赖环境，不是 clean install。 |
| C：生命周期与查询 | 单一管理器、jobs/versions/replace-delete cleanup 共锁事务、retrieval legacy 归属、MCP ACL/session 不变 | `repository/jobs/versions/cleanup/clock` 拆分已落地；service 独占 manager、connection、锁与事务。生命周期/snapshot/shared-model定向 16 passed，静态 ownership review 通过；没有生产或跨进程并发验收。legacy retrieval canonical 路径已调整。 |
| D：脚本、包、测试与资产 | scripts/evaluation/tests 边界、正式包不依赖仓库根 ingest、运行资产说明 | 目录/兼容入口和 marker 分类已落地；57模块映射34 quick/22 integration/1 live；collect-only 为644项、未执行测试。集中回归首轮636 passed/8 failed，修复后11项相关合同定向复验通过，没有重跑全组；保护资产14/14匹配。marker仅区分成本，不保证副作用隔离。审计/TLS沿用现有环境变量。 |

## 不随源码重构搬移的内容

语料、原始文件、索引、manager 数据库、`.env`、真实 ACL、运行时 registry、日志/证书和旧 artifacts 均未作为结构迁移目标；保护集合ACL/registry/manifest 14/14 SHA与基线相同。最终回归曾误写旧 `artifacts/corpus-acceptance-002/batch1-2-acceptance.json`；事故副本保留，原 tracked 文件已按事故前 HEAD 恢复且 blob/hash 与 HEAD 一致，详见总日志。部署说明需区分宿主机与容器系统，目标平台可为内网 Windows 或 Linux。

修改、验收与已知限制以 iteration 日志为准；本目录表里的“已落地”只描述源码职责变化，不自动表示浏览器 UI、目标平台安装或生产验收通过。
