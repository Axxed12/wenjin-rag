# 问津 RAG / MCP 统一验收流程

> 维护说明（2026-10-07）：以下命令从项目根目录执行。文中的旧实测数字只代表标注日期；本文搬迁未重跑环境、构建或验收。当前能力与未验证项见 [当前状态](../code.md)。

本清单用于统一验收当前本机MVP。切换演示最终会回退原索引；运行前确认Qdrant正常，且不要
同时运行另一个管理员切换命令。

## 1. 环境和自动测试

```powershell
uv sync --locked
docker compose up -d
uv pip check
uv run pytest -q
uv run ruff check ingest.py scripts src evaluation tests
docker compose config --quiet
```

预期：主项目测试、Ruff、主环境依赖和Compose配置全部通过。

## 2. active索引

```powershell
uv run python scripts/manage_indexes.py status
uv run python scripts/manage_indexes.py validate configs/indexes/osg-3.6.5-blocks.json
```

预期：active为`osg-3.6.5-blocks-dualhash-003`；BM25和Qdrant均307，且双Hash元数据均307，
维度1024、状态green、manifest
SHA256一致。当前revision至少为5；继续运行演示后递增属于正常审计历史。

## 3. 真实新增文件、切换和回退

```powershell
uv run python scripts/demo_content_update.py
```

预期：

- 候选chunk、BM25行数和Qdrant points三者一致，且比active至少增加1；
- `sentinel_found=true`；
- 四个`regressions[].passed`全部为true；
- `active_after_rollback=osg-3.6.5-blocks-dualhash-003`；
- `after_rollback.new_document_absent=true`；
- `candidate_assets_cleaned=true`、`status=PASS`。

再次运行`manage_indexes.py status`确认active仍是原target。演示会增加switch/rollback事件，不
保留候选资产。

### 修改与删除专项

```powershell
uv run python scripts/demo_modify_delete.py
```

预期总`status=PASS`，两个case均`candidate_assets_cleaned=true`：

- modify：候选双索引中的`README.md`chunk数量一致，`sentinel_found=true`，旧Viewer问题通过；
- delete：被删文件在候选Qdrant/BM25中均为0，来源清单不含它，回退后双索引均恢复；
- 最终`active_after_all_cases=osg-3.6.5-blocks-dualhash-003`。

## 4. 正式CLI检索

```powershell
uv run python scripts/search_docs.py "Viewer 的 run() 做什么？" --top-k 2
```

预期包含`include/osgViewer/Viewer:82-115`附近证据和active `index_target_id`，并声明分数只是
排序信号、当前不生成答案。

## 5. 状态化MCP和stdout审计

终端A：

```powershell
uv run python -m wenjin.mcp_server
```

若8765已占用，先确认是否已有服务；不要直接结束未知进程。隔离验收可用`--port 8766`。

终端B：

```powershell
uv run python scripts/test_mcp_client.py "Viewer 的 run() 做什么？"
```

预期：32字符Session ID、工具`search_docs`、协议`2025-11-25`、响应含32字符`request_id`。
终端A出现`event=mcp_tool_audit`的单行JSON；request_id与响应一致，默认只有Query/Session哈希，
不应出现原始问题正文。完成后在终端A按Ctrl+C。

## 6. Claude Code规则

根目录`CLAUDE.md`已固定证据规则。项目级MCP配置有效时正常提问即可；详细四题见
`docs/claude-code-acceptance.md`。第二轮结果已经4/4通过，不需要每次粘贴严格提示词。

## 7. 产物

- `artifacts/runs/content-update-switch-rollback-001.json`
- `artifacts/runs/content-modify-delete-001.json`
- `artifacts/runs/index-switch-rollback-001.json`
- `artifacts/runs/mcp-audit-stdout-001-response.json`
- `artifacts/runs/claude-code-acceptance-002-review.md`
- `docs/development-log.md`

## 8. Docker MCP

```powershell
docker compose build mcp
docker compose up -d
docker compose ps
uv run python scripts/test_mcp_client.py "Viewer 的 run() 做什么？"
docker compose logs --tail 50 mcp
docker compose stop mcp
```

预期MCP/Qdrant均healthy，客户端Top-1为Viewer头文件，容器stdout包含相同request_id审计行。
完整配置、端口冲突和离线镜像摆渡见`docs/docker-deployment.md`。

## 9. TLS与鉴权

按`docs/docker-deployment.md`生成开发证书并配置安全Compose覆盖。至少检查：

1. 没有Bearer返回401；
2. 错误Bearer返回401；
3. 正确Bearer和受信证书能建立32字符Session并调用`search_docs`；
4. stdout成功审计含`client_id_sha256`但不含密钥；
5. 401日志含`mcp_auth_denied`且不含Authorization值。

当前验收仍是本机自签名TLS MVP，不代表跨机器、企业CA、目标内网OS、并发或生产验收。
