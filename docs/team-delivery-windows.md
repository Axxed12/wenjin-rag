# Windows Docker Desktop 离线交付与隔离恢复

本文提供 Windows 10/11 上的离线资产核验、停写备份、隔离恢复和受限服务诊断命令。本轮已在 Windows Docker Desktop 上完成 source-bound 镜像构建与烟测、隔离 Docker 管理/MCP 业务请求和 Qdrant Server 快照恢复；干净目标机离线导入/启动、宿主机端口与跨机访问及真实模型运行仍未验收。Docker 本轮使用合成模型 API，不加载 GPU 模型。

## 离线资产清单

先在联网准备机根据本轮冻结源码生成 backend、model-node、stateless MCP 三个 source-bound 镜像，并保存为 tar；再把 Compose 文件、锁文件、wheelhouse 或模型权重放进交付目录。三镜像构建脚本默认只准备上下文，只有显式 `--build` 和冻结 source hash 才会构建；它核对缓存基础镜像 ID，以 `--network=none` 构建，不加载 GPU 模型。构建后的 import/`--help` 烟测和 Docker 业务联验需单独记录。每个资产用明确的名称和版本标记；Docker Desktop 与 Engine 版本从目标兼容矩阵的核准信息手工记录。下面所有版本/tag均为占位示例，须替换为本轮实际测量/冻结值；它们不代表已验证的交付版本。该工具不读取 `.env`、token、私钥或 credentials 文件；这类路径也会被拒绝加入清单。

```powershell
uv run python scripts/operations/build_delivery_images.py `
  --source-root . `
  --output 'E:\delivery\frozen-image-build' `
  --expect-source-sha256 '<source-hash-from-freeze-record>' `
  --build

docker save --output 'E:\delivery\wenjin-backend.tar' 'wenjin-backend:delivery-<source-hash-prefix>'
docker save --output 'E:\delivery\wenjin-model-node.tar' 'wenjin-model-node:delivery-<source-hash-prefix>'
docker save --output 'E:\delivery\wenjin-mcp-stateless.tar' 'wenjin-mcp-stateless:delivery-<source-hash-prefix>'
```

```powershell
$delivery = 'E:\delivery\wenjin'
New-Item -ItemType Directory -Force $delivery | Out-Null
docker save --output "$delivery\wenjin-backend.tar" 'wenjin-backend:<delivery-tag>'
Copy-Item 'deploy\compose.base.yaml','deploy\compose.local.yaml','uv.lock' $delivery

uv run python scripts/team_delivery.py manifest `
  --root $delivery `
  --output "$delivery\delivery-manifest.json" `
  --host-target windows11 `
  --docker-desktop-version '<measured-desktop-version>' `
  --docker-engine-version '<measured-engine-version>' `
  --asset 'backend-image@wenjin-backend:delivery-<source-hash-prefix>=wenjin-backend.tar' `
  --asset 'model-node-image@wenjin-model-node:delivery-<source-hash-prefix>=wenjin-model-node.tar' `
  --asset 'mcp-stateless-image@wenjin-mcp-stateless:delivery-<source-hash-prefix>=wenjin-mcp-stateless.tar' `
  --asset 'compose-base@<delivery-tag>=compose.base.yaml' `
  --asset 'compose-local@<delivery-tag>=compose.local.yaml' `
  --asset 'python-lock@<delivery-tag>=uv.lock'
```

在目标机把清单与资产复制到同一 staging 目录后核验。给定期望的每个资产版本可以再加 `--expect-version name=version`；`--host-target windows10` 或 `windows11` 必须与目标机对应。任何缺文件、大小或 SHA256 不符、路径越界、资产版本不符都会失败关闭。

```powershell
uv run python scripts/team_delivery.py verify-manifest `
  --manifest .\delivery-manifest.json `
  --root . `
  --host-target windows11 `
  --expect-version 'backend-image=wenjin-backend:delivery-<source-hash-prefix>'
```

纯 Docker 离线目标机无需预装 Python、uv 或主项目依赖。先通过独立可信渠道取得 manifest 文件 SHA256；PowerShell 核对 manifest 和镜像 tar 的 hash 后加载镜像，再用已加载镜像内的标准库校验器离线核对其余资产：

```powershell
$trustedManifestSha256 = '<trusted-channel-manifest-sha256>'
$manifestSha256 = (Get-FileHash .\delivery-manifest.json -Algorithm SHA256).Hash.ToLowerInvariant()
if ($manifestSha256 -ne $trustedManifestSha256.ToLowerInvariant()) { throw 'manifest SHA256不匹配' }
$manifest = Get-Content .\delivery-manifest.json -Raw | ConvertFrom-Json
$imageAsset = $manifest.assets | Where-Object { $_.name -eq 'backend-image' }
if (-not $imageAsset) { throw 'manifest缺少backend-image条目' }
$imageTarSha256 = (Get-FileHash .\wenjin-backend.tar -Algorithm SHA256).Hash.ToLowerInvariant()
if ($imageTarSha256 -ne $imageAsset.sha256) { throw '镜像tar SHA256不匹配' }
docker load --input .\wenjin-backend.tar
$deliveryRoot = (Resolve-Path .).Path
docker run --rm --network none `
  --mount "type=bind,source=$deliveryRoot,target=/delivery,readonly" `
  'wenjin-backend:delivery-<source-hash-prefix>' `
  python scripts/team_delivery.py verify-manifest `
    --manifest /delivery/delivery-manifest.json `
    --root /delivery `
    --host-target windows11 `
    --expect-version 'backend-image=wenjin-backend:delivery-<source-hash-prefix>'
```

将示例的 manifest 可信 SHA256、三个镜像 tag 和目标 Windows 版本替换为实际冻结值。镜像中的标准库脚本提供离线资产校验；它不会替代目标机容器启动、业务请求或模型验证。

此清单的 SHA256 用来发现传输损坏或版本错配，不是数字签名，也不能证明制作者身份。通过可信渠道单独核对 `delivery-manifest.json` 的 SHA256；部署方仍须检查 Docker Desktop、驱动、磁盘空间、模型文件和容器GPU支持等机器前提。离线容器可按清单顺序 `docker load --input .\wenjin-backend.tar`，先 `docker compose ... config --quiet`，确认镜像引用和路径后再启动隔离项目。

本机已有的模型文件与缓存镜像只读盘点见 [local-asset-inventory.json](../artifacts/iterations/2026-10-17-team-delivery/local-asset-inventory.json)。该文件记录版本/内容 hash，不导出模型或镜像；它不是摆渡包，也不是目标机离线启动通过证据。本轮冻结源码的 SHA256 为 `639794c5b660851559d2fc0894d80356f9528f381e90cfe027e58c33463a2ab6`（207 个源码文件），backend、model-node 和 stateless MCP 均已使用缓存基础镜像在 `--network=none` 下构建；三镜像 ID、tag 与 smoke 结果见 [docker-acceptance.json](../artifacts/iterations/2026-10-17-team-delivery/docker-evidence/docker-acceptance.json)。Backend/stateless MCP 验证 `/app` 项目根、冻结模块路径和模块 hash，并通过 CLI `--help`；model-node 仅通过 import 烟测，不代表推理通过。

## 停写备份与新目录恢复

只对已停写、已停止的服务做备份。先停止管理/MCP写入者和 Qdrant，再将本地文件组件与持久化 Qdrant 数据目录作为显式 `--include` 加入归档。若服务使用 Docker 命名卷，先在维护窗口把已停止的卷导出到受控文件目录，再将该导出目录作为组件；本脚本不直接读写 Docker 卷或 Docker socket。

典型组件包括身份及文件管理账本、managed 原文、managed 版本 manifest/BM25、共享 manifest/raw/config/release/index registry 与 BM25、release catalog、以及停止状态下的 Qdrant 持久化目录。不同项目拓扑使用不同路径，操作者需按 `WENJIN_PROJECT_ROOT`、managed raw 根和 Qdrant 实际持久化位置逐项确认完整性。`.env`、secret、私钥不进入归档；身份 SQLite 含凭据哈希、会话与授权记录，归档应放在受控且加密的存储位置。本工具不加密归档。本轮实际 snapshot drill 只把快照导入到已确认无 collection 的新 Qdrant Server。恢复 helper 不执行目标空库检查，因此只能用于新建空 server；它不是覆盖现有 collection 的恢复工具，不能对带数据的 server 执行。

```powershell
uv run python scripts/team_delivery.py backup `
  --root 'E:\wenjin\project' `
  --include data/file-management `
  --include data/indexes `
  --include data/releases `
  --include raw_data `
  --include corpora `
  --include configs/release-profile.json `
  --include qdrant-local `
  --output 'F:\protected-backups\wenjin-2026-10-17.zip'

uv run python scripts/team_delivery.py verify-backup `
  --bundle 'F:\protected-backups\wenjin-2026-10-17.zip'

uv run python scripts/team_delivery.py restore `
  --bundle 'F:\protected-backups\wenjin-2026-10-17.zip' `
  --destination 'E:\restore-drill\wenjin-copy'
```

输出目录必须位于项目 root 之外；恢复目标必须不存在，工具拒绝覆盖。恢复先验证归档 manifest、成员路径、大小和 SHA256，再写入同盘暂存目录，最后原子改名。可变身份、文件管理器和 release/catalog 协调库使用 SQLite backup API 并做 `quick_check`；已封存的 BM25/索引 SQLite 按字节复制，以保留 release 校验所需的原始文件 hash。跨身份库、文件管理器、release catalog、registry、原文和 Qdrant 的时间点一致性仍要求操作前停写全部写者。

备份 manifest 记录原项目 root，并只接受已部署的两种 managed 原文布局：原生 `raw_data` 与 Docker `data/file-management/raw_data`。恢复会对 managed `versions` 表中四个产品固定字段（`raw_root`、`source_path`、`manifest_path`、`bm25_path`）执行严格路径绑定校验并重定位到新 root。目录形状不匹配、缺失 managed 版本资产或 SQLite schema 不兼容时会拒绝恢复。其他自定义配置中的绝对路径不会被猜测或改写，须由维护者检查。

## 管理/MCP 受限启动记录

`launch-service` 仅提供 `management`（loopback 18777）与旧版状态化 MCP（loopback 18765）两个入口。它会保持前台运行直到进程退出；正常关闭用 Ctrl+C。记录只含入口名、loopback地址、端口、route、PID、开始/结束UTC时间和退出码。完整命令和进程环境不写入记录；输出日志会遮蔽常见凭据字段和从环境变量识别的 token/password/secret 值。

management launcher 显式遵循既有 CLI 配置：设置 `WENJIN_IDENTITY_DB` 时使用该身份库，未设置时保留旧的无本地账户模式；`WENJIN_FILE_ADMIN_TOKEN` 只从子进程环境读取。legacy MCP launcher 显式传 `--project-root`，并沿用子进程的身份库与 API key 环境配置；命令、token 值和完整环境不会写入诊断记录。

```powershell
uv run python scripts/team_delivery.py launch-service `
  --service management `
  --python .\.venv\Scripts\python.exe `
  --project-root 'E:\wenjin\project' `
  --output-dir 'E:\protected-diagnostics\management-run-2026-10-17'

uv run python scripts/team_delivery.py launch-service `
  --service legacy-mcp `
  --python .\.venv\Scripts\python.exe `
  --project-root 'E:\wenjin\project' `
  --output-dir 'E:\protected-diagnostics\mcp-run-2026-10-17'
```

此 launcher 是新进程入口，不会附加、停止或接管已有监听进程。需确认端口空闲后再运行。故障退出时保留 `run-record.json` 与有限 `service-redacted.log`；诊断有价值，但日志脱敏不等于日志无敏感业务内容，交付前仍需人工查看并按内部留存策略保护。

Docker 诊断器只接受调用者提供的 container ID 与启动时写入的 `com.wenjin.delivery.run_id` 标签相同的临时容器；仅白名单字段进入报告，不收集容器环境变量。`--include-logs` 可选，最多保留 200 行并做常见密钥字段脱敏。对不属于本轮启动记录的容器会拒绝读取日志。

```powershell
uv run python scripts/team_delivery.py diagnose-container `
  --container-id $TemporaryContainerId `
  --run-id $RunIdFromStartRecord `
  --output '.\diagnostics\temporary-container.json' `
  --include-logs
```

状态码、OOMKilled、restart count、启动/结束时间及受控服务日志有助于定位退出层级；它们本身仍不能证明业务根因。2026-10-14 原生 18777/18765 曾在后续检查时无监听，旧日志未提供退出原因；当轮恢复服务只证明按原配置恢复后能响应，不证明原退出原因。

## 当前验收边界

本轮隔离 Docker 联验与剩余边界见 [docker-acceptance.json](../artifacts/iterations/2026-10-17-team-delivery/docker-evidence/docker-acceptance.json)、[Qdrant Server snapshot manifest](../artifacts/iterations/2026-10-17-team-delivery/docker-evidence/qdrant-server-snapshot-manifest.json) 和 [恢复后业务报告](../artifacts/iterations/2026-10-17-team-delivery/docker-evidence/restored-business-report.json)。本机 Docker Engine 为 29.5.2，使用合成 API、内部专用网络和本轮 Compose 项目；没有发布可用来证明宿主机或跨机访问的端口。

| 项目 | 本轮结果 |
|---|---|
| 三个 source-bound 镜像 | 已构建并完成限定 smoke；每个 ID/tag/source SHA 记录在验收 JSON。没有执行模型推理。 |
| Docker managed API | 恢复后 health/login 成功；授权 reader 检索活动 managed v2；旧版本返回 409；无 library grant 的 reader 返回 403。 |
| Docker MCP | FastMCP Streamable HTTP client 实际列出 `list_corpora`、`list_managed_files`、`search_docs`；reader 只检索到 managed v2；admin 检索到已激活 OSG release A，未混入 B。 |
| 应用备份恢复 | 42 个文件校验并恢复，bundle SHA256 见验收 JSON；重定位 2 个 managed raw version。源/恢复身份库的三位合成账户密码哈希字段逐用户一致；恢复后登录使用原 seed 测试凭据的内存派生值，凭据未写入报告。 |
| Qdrant Server | 新目标在恢复前确认 0 collections；导入 5 个 snapshot 后恢复 5 个 collection，各含 1 个合成 point。操作只适用于新空 server，不代表对已有 server 的覆盖恢复。 |
| 离线目标机 | 未在干净 Windows 10/11 Docker Desktop 目标机执行 tar 导入、manifest 校验和 Compose 启动；文档中的 bootstrap 是可执行步骤，不是本轮已验收结果。 |
| 网络与模型 | Docker 内部 DNS/loopback 业务调用通过；未验证宿主机 published port、跨机访问、GPU 或真实模型推理。 |

snapshot manifest、账户比较只保留哈希相等的布尔结果，业务报告不含密码或 token。首轮 MCP 启动缺失 OSG registry 的失败被保留；补入合成 registry 与真实 `ReleaseService.prepare/activate` 资产后重新做 snapshot/backup/restore。恢复 HTTP 首次 403 的原因是测试请求漏传同源 `Origin`，增加正确 Origin 后登录通过；这不是身份数据缺失。恢复 reader 的 OSG 访问未被放宽，shared release 请求使用恢复后的管理员身份。

六类候选研发问题见 [candidate-questions.md](../artifacts/iterations/2026-10-17-team-delivery/candidate-questions.md)，机器可读清单见同目录 JSON。问题、答案与来源均需人工审查，不属于冻结集或黄金集。

清理时仅移除本轮 `run_id=495afbb07c3849c4b2a00fffb7a08bc0` Compose 容器、网络和卷；三张 source-bound 交付镜像作为本地可导出资产保留，不是运行部署。现有项目容器、基础镜像及模型文件未触碰。删除临时目录中的合成凭据文件被自动审批策略阻止，未尝试其他删除方式；文件路径与状态见验收 JSON，内容未写入证据。
