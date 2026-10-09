# 文件管理 HTTP 与 CLI

本文件说明文件级管理入口。它运行独立的 Starlette/uvicorn HTTP 服务，不注册进默认 MCP；MCP 的检索 ACL 不会被复用为删除授权。

## 启动与配置

```powershell
uv run python -m wenjin.file_management serve --root <project-root> --host 127.0.0.1 --port 8777
```

`--root`是项目根目录，用于读取既有语料inventory。SQLite、managed版本manifest、BM25索引、staging和管理器内部账本写入 `<project-root>/data/file-management/`；managed原文副本位于 `<project-root>/raw_data/file-management-<file_id>/<version>/`；dense向量保存在Qdrant的文件专属collection。默认root为代码所在项目根目录，也可用 `WENJIN_PROJECT_ROOT`指定。请从项目根目录运行下方`uv run`命令；Qdrant 与本地Embedding配置从`<project-root>/.env`及进程环境读取：`QDRANT_URL`、`QDRANT_API_KEY`、`EMBEDDING_MODEL_PATH`。相对Embedding路径按project root解析。`list/status/delete`不初始化Embedding模型；首次导入或managed search时延迟创建并复用本地适配器。默认只绑定loopback；绑定非loopback地址时必须配置 `WENJIN_FILE_ADMIN_TOKEN`。token由服务进程环境或`serve --token`参数提供，不从`.env`自动读取；设置token后每个HTTP接口都要求 `Authorization: Bearer <token>`。管理CLI同样只调用HTTP服务，不会另建manager实例。

## HTTP资源

同一服务提供浏览器管理界面：打开 `/admin/`（根路径 `/` 会跳转）。界面沿用下列 API，凭据只保存在页面内存。使用方法与功能边界见 [管理界面说明](admin-frontend.md)。

| 操作 | 请求 |
|---|---|
| 文件清单 | `GET /api/files` |
| 状态 | `GET /api/files/{file_id}` |
| 活动双索引/原文对账 | `GET /api/files/{file_id}/resources`，返回原文Hash校验、BM25行数、Qdrant点数和collection |
| 上传 | `POST /api/files`，body为原始文件bytes，`X-File-Name`为UTF-8文件名（CLI会做百分号编码） |
| 替换 | `PUT /api/files/{file_id}`，同上传格式 |
| 删除 | `DELETE /api/files/{file_id}` |
| adopt legacy | `POST /api/files/{legacy_id}/adopt` |
| 任务状态 | `GET /api/jobs/{job_id}` |
| 任务历史分页 | `GET /api/jobs?limit=100&offset=0`，`limit` 范围1–200，`offset`为非负整数；列表省略资源明细，按任务ID查看详情 |
| 页面配置摘要 | `GET /api/admin/config`，仅返回上传上限及是否要求管理认证 |
| 重试 | `POST /api/jobs/{job_id}/retry` |
| 检索 | `POST /api/search`，JSON `{ "file_id": "...", "query": "...", "version": "...", "top_k": 5, "context_char_budget": 8000 }`（后三项可省略，版本缺省时取同一active snapshot） |
| 原文下载 | `GET /api/files/{file_id}/download`，多原文时用 `?path=<manifest登记的相对路径>` |

上传上限默认100 MiB，可用 `--max-upload-bytes` 调整。服务先用随机内部名流式接收到 `<project-root>/data/file-management/.incoming`，完成持久提交后删除这份临时文件；客户端文件名从不拼接本地路径。

异步提交返回HTTP 202和持久任务状态：`phase`、`progress`、`retryable`、`reason`、`resources`均来自核心状态记录；重启时服务自动续跑挂起任务。同一进程最多运行一个job，状态查询仍可处理。任务阶段发生冲突或当前文件为legacy时返回409；未知文件/任务返回404；上传过大返回413。

## 文件与权限边界

管理器自有文件以活动版本快照提供检索和原文下载。`FileRecord`提供人类可读名称、`file_id`、active version、状态和chunk数；列表中legacy行显式标为 `managed=false`、`legacy_unmanaged`，不暴露共享文件系统路径。

## 两个终端中的操作顺序

在终端1启动服务并保持运行；`serve`是前台命令：

```powershell
uv run python -m wenjin.file_management serve
```

在终端2执行CLI。上传、替换、删除和重试都会返回任务记录；用其中的job_id调用`job`，直到任务进入完成或失败状态。以下ID是占位值，实际操作时替换为命令返回的ID：

```powershell
uv run python -m wenjin.file_management upload .\资料.txt
uv run python -m wenjin.file_management job <upload-job-id>
uv run python -m wenjin.file_management list
uv run python -m wenjin.file_management replace <file-id> .\资料-新版.txt
uv run python -m wenjin.file_management job <replace-job-id>
uv run python -m wenjin.file_management delete <file-id>
uv run python -m wenjin.file_management job <delete-job-id>
# 只有失败任务显示 retryable=true 时才重试
uv run python -m wenjin.file_management retry <failed-job-id>
uv run python -m wenjin.file_management job <retry-job-id>
```

显式adopt会通过已登记manifest和原文SHA校验后，把一份副本导入隔离的managed索引。旧共享raw/manifest/index原样保留；随后管理器的替换或删除只会影响新副本。尚未adopt的legacy不能通过管理器搜索、替换或删除。JobRecord.resources用于检查此次任务触及的受管理资源和清理状态，不包含绝对路径或正文。

### 删除范围

删除一个managed文件会清理文件管理器登记的全部历史版本和失败/暂存资源：managed原文副本、版本manifest目录、BM25数据库及sidecar、对应Qdrant collections、文件专属managed索引目录，以及历史import job的staging目录。任何版本目录内的manager-owned副本也随版本目录清理；当前没有独立备份服务。外部备份和历史artifact不检查、不删除。调用者上传前在本机保留的源文件不受影响。

Legacy原asset、manifest、旧索引和旧route继续保留，可能仍可被旧入口检索。SQLite保留不含正文的任务记录、清理账本和文件tombstone，因此“已删除”表示受管原文与索引资源按job账本清理完毕，不表示SQLite中所有关联元数据都被抹除。

管理入口本机可用，但暂不提供TLS、多人身份、审计后台或远端账号生命周期管理；需要远程使用时应由可信TLS反向代理承载，且必须设置独立管理Bearer。

## MCP发现和检索managed上传

文件管理器由一个服务进程独占 `data/file-management/manager.lock`。MCP只通过HTTP读取它的实时文件列表和检索结果，不会在自己的进程内创建第二个 `FileManagementService`。MCP不提供上传、替换、删除、adopt或重试工具；这些操作仍走本机管理CLI/HTTP API。

在MCP服务进程环境配置：

```dotenv
WENJIN_FILE_MANAGEMENT_URL=http://127.0.0.1:8777
WENJIN_FILE_ADMIN_TOKEN=<独立的文件管理Bearer凭据>
WENJIN_MCP_API_KEY=<MCP调用方Bearer凭据>
WENJIN_MCP_CLIENT_ID=claude-code
```

`.env.example`只是模板；`mcp_server.main()`从进程环境读取这些变量，不会因为复制模板就自动导出。启动两个服务的PowerShell终端都要设置同一份文件管理Bearer；MCP调用方Bearer另设一份：

```powershell
$env:WENJIN_FILE_MANAGEMENT_URL = "http://127.0.0.1:8777"
$env:WENJIN_FILE_ADMIN_TOKEN = "<同一份文件管理Bearer凭据>"
# MCP服务进程另外设置调用Agent使用的凭据：
$env:WENJIN_MCP_API_KEY = "<独立的MCP调用方Bearer凭据>"
$env:WENJIN_MCP_CLIENT_ID = "claude-code"
```

`WENJIN_FILE_ADMIN_TOKEN`只由MCP服务器发往管理HTTP服务，绝不作为工具参数或结果输出。它与调用Agent的`WENJIN_MCP_API_KEY`分开设置；MCP中间件将通过认证的调用映射为`WENJIN_MCP_CLIENT_ID`，该client_id必须与ACL白名单完全一致。缺少管理token时managed MCP工具fail-closed并返回不可用状态。管理API即使只绑定loopback，也建议配置管理Bearer，使MCP和CLI都使用同一受保护入口。不要把真实凭据写入版本控制或MCP参数。

每个managed文件的ACL corpus身份固定为`managed-file-<file_id>`，`file_id`在replace期间保持不变。授权先查文件精确策略；精确策略存在时以它为准（显式deny会覆盖共享策略），只有精确策略不存在才继承`managed-files`策略。两者都没有时默认拒绝。namespace允许一次性授权现有client_id给后续上传文件，而不触碰既有OSG、其他语料或79条legacy策略。精确策略可用于单独撤回某个文件的共享授权。

管理员可在确认授权对象后，创建内部namespace策略。以下PowerShell示例把`claude-code`替换为部署实际使用的`WENJIN_MCP_CLIENT_ID`；它会写入一个新的managed namespace策略，不应覆盖现有语料策略：

```powershell
$accessDir = Join-Path (Get-Location).Path "configs\access"
New-Item -ItemType Directory -Force -Path $accessDir | Out-Null
$policyPath = Join-Path $accessDir "managed-files.json"
if (Test-Path -LiteralPath $policyPath) { throw "策略已存在，请先检查现有授权：$policyPath" }
$policy = @{
    schema_version = 1
    corpus_id = "managed-files"
    visibility = "internal"
    allowed_clients = @("claude-code")
    note = "授权指定MCP client读取新上传的managed文件"
}
$policy | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $policyPath -Encoding utf8
```

项目会在每个请求加载完整ACL快照。策略文件缺失、损坏或身份冲突时拒绝读取，不回退到旧快照。显式`visibility=public`加`allowed_clients=["*"]`仍遵循项目现有公开策略语义；managed文件建议使用上面的internal白名单，`internal`策略不能使用`*`。

上传任务完成后，Agent按以下顺序调用只读MCP工具：

1. 调用`list_managed_files`，只会看到ACL允许且当前active的managed文件；返回`file_id`、版本、version_id和片段数。legacy共享文件、待处理上传队列和已删除文件不在此列表。
2. 调用`search_docs`，参数示例：

```json
{
  "query": "这个文件如何配置坐标系？",
  "file_id": "<list_managed_files返回的file_id>",
  "top_k": 5,
  "context_char_budget": 8000
}
```

可选`version`必须等于检索请求的active版本。active版本和检索在管理器同一把读锁下校验；replace提交切换后发起的新请求只查新版本，delete完成后发起的新请求不再发现该文件或得到证据。切换前已经开始的检索按它锁内取得的snapshot完成，已返回给客户端的证据不会被撤回。MCP结果保留`file_id`、实际`version_id`和`version`，每条正文证据带来源定位与active原文SHA256。当前不提供managed原文下载链接；结果中的上游`http(s)`来源URL（若有）只是出处，不是上传文件取件入口。`search_docs`仍只返回证据，不生成答案；分数只是排序信号。

2026-10-07管理HTTP补充：`GET /api/admin/libraries`只读展示既有资料及受管文件；`POST /api/admin/search`先选库、版本来源和可选文档，必传目录的唯一`version_id`，过期返回409。原`POST /api/search`兼容旧调用，可选传`version_id`；新管理页面始终传入。`GET /api/admin/calls`独立查询管理HTTP与可读取的部分MCP审计，不是后台作业历史。新上传`POST /api/files?library_id=<已有库身份>`指定逻辑归属，省略时归入`managed-files`，同库内判重；不合并共享索引，不自动adopt。详情见[管理界面说明](admin-frontend.md)。
