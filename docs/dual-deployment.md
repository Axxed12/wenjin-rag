# 双部署操作说明

本文面向本机维护者和部署操作者，说明两种Docker拓扑、启动方式、远程模型链路与备份边界。根目录`compose.yaml`仍是旧兼容入口；新配置位于`deploy/`，不要把两种stack同时接到同一Qdrant/数据目录。

2026-10-14用户授权将新版MCP2026-07-28个人迭代整合到当前代码快照；旧MCP仍遵循2025-11-25。新版路径、secret/下载隔离、追加启动与回退见[新版MCP说明](mcp-stateless.md)，本轮兼容状态见[整合记录](../artifacts/iterations/2026-10-14-mcp-stateless-integration/development-log.md)，来源SDK/容器证据见[2026-10-09历史记录](../artifacts/iterations/2026-10-09-mcp-stateless/development-log.md)。该新增能力不回写到技术叙事截止日前。

## 拓扑与职责

本机完整部署由管理HTTP、状态化MCP、Qdrant和GPU模型节点组成。管理HTTP是受管文件、版本、身份、任务与SQLite状态的唯一管理进程；MCP通过它的HTTP桥访问受管状态，不另起manager。共享语料原文、registry、BM25和manifest只读挂载；受管文件原文使用独立的`WENJIN_MANAGED_RAW_DATA_ROOT`，在新stack中保存在`wenjin-data`卷。Qdrant只在Docker私有数据网内提供服务。

云端stack由轻量管理HTTP、轻量MCP、同stack Qdrant和HTTPS边缘代理组成；本机GPU节点只提供embedding与rerank API，云端通过VPN/私网路由及TLS访问。两边有不同Docker宿主时，Compose bridge不能跨机器互通；`model-egress`也不构成隧道。必须先让云宿主的VPN/私网路由能到达模型节点，再为该地址配置私有DNS与受信CA。SSH反向转发若只绑定宿主机`127.0.0.1`，容器通常不能把它当自己的loopback访问。

```text
本机：浏览器/Agent -> loopback管理HTTP或MCP -> 管理HTTP桥 / Qdrant -> GPU模型节点
云端：公网TLS -> Caddy -> 管理HTTP或MCP -> 内网Qdrant
                                             -> VPN/TLS -> 本机GPU模型节点
```

模型API负责embedding与rerank，不负责索引存储或检索。MCP返回带来源的证据，最终回答由调用它的客户端Agent生成。Query改写和回答生成仍由独立配置负责，本轮没有把所有LLM统一到模型节点。

## 前置条件与secret

本机完整配置需要Docker Compose、NVIDIA容器工具链、GPU以及本机embedding和reranker权重。云端示例目前只是一套配置模板；云主机和域名尚未购买。2核/2GB只是计划目标，本项目没有对此规格作容量保证。MinerU保持在已有容器，不由本配置安装。

从仓库根目录按用途复制`deploy/.env.local.example`、`deploy/.env.cloud.example`或GPU节点上的`deploy/.env.model-node.example`，分别保存为`deploy/.env.local`、`deploy/.env.cloud`和`deploy/.env.model-node`，再按实际路径编辑。Windows模板中的模型路径`E:/Models/...`来自当前主机的目录；其他机器按实际位置修改。模板只含路径、地址和稳定模型ID，不含secret值。为每个stack创建独立、未提交的secret文件。Windows PowerShell可在仓库根执行下面命令生成两份随机token；脚本不打印token：

```powershell
New-Item -ItemType Directory -Force 'deploy/secrets' | Out-Null
uv run python -c "from pathlib import Path; import secrets; [Path(p).write_text(secrets.token_hex(32), encoding='ascii') for p in ('deploy/secrets/model_api_token','deploy/secrets/file_admin_token')]"
```

Linux宿主可这样生成并限制权限：

```sh
install -d -m 0700 deploy/secrets
umask 077
openssl rand -hex 32 > deploy/secrets/model_api_token
openssl rand -hex 32 > deploy/secrets/file_admin_token
chown 10001:10001 deploy/secrets/model_api_token deploy/secrets/file_admin_token
chmod 0600 deploy/secrets/model_api_token deploy/secrets/file_admin_token
```

容器以UID/GID 10001运行，Linux上的文件绑定应由该UID可读；不要通过`0644`开放secret。Windows Docker Desktop的文件权限映射与原生Linux不同，需在目标主机复验。token不会输出到日志。模型API的`/v1/health`和`/v1/identity`同样需要Bearer token。

## 本机完整Docker

准备模型路径、token secret，并保证这些共享目录存在且与当前registry/BM25/向量版本匹配。启动配置只映射管理页面和MCP到宿主loopback，不发布Qdrant或模型API：

```powershell
docker compose --project-directory . --env-file deploy/.env.local `
  -p wenjin-local -f deploy/compose.base.yaml -f deploy/compose.local.yaml config --quiet
docker compose --project-directory . --env-file deploy/.env.local `
  -p wenjin-local -f deploy/compose.base.yaml -f deploy/compose.local.yaml up -d --build
docker compose --project-directory . --env-file deploy/.env.local `
  -p wenjin-local -f deploy/compose.base.yaml -f deploy/compose.local.yaml ps
```

管理页默认是`http://127.0.0.1:18777/admin/`，MCP Streamable HTTP默认是`http://127.0.0.1:18765/mcp`。首次启动后在管理容器中运行本地引导命令，密码由CLI交互读取，不作为命令行参数保存：

```powershell
docker compose --project-directory . --env-file deploy/.env.local `
  -p wenjin-local -f deploy/compose.base.yaml -f deploy/compose.local.yaml `
  exec management python -m wenjin.file_management bootstrap-admin `
  --identity-db /var/lib/wenjin/identity.sqlite --username admin
```

健康检查只能证明HTTP进程可响应，不能证明index已授权或证据正确。首次登录的admin仍须显式给账户授予资料库读取权限。MCP保留`2025-11-25`状态化`initialize`、`notifications/initialized`、能力协商及可选`MCP-Session-Id`语义。

Compose中的`WENJIN_MCP_ALLOW_PRIVATE_HTTP=1`只表达部署者选择了私有容器网络；它不会验证网络安全。账户模式MCP监听非loopback明文时默认启动拒绝。`WENJIN_FILE_MANAGEMENT_ALLOW_PRIVATE_HTTP=1`是独立的服务桥opt-in，只由MCP容器使用，以便在Compose internal data-plane调用唯一管理HTTP；未设置时远程明文桥仍默认拒绝。不要将MCP容器端口直接发布到LAN/公网；跨主机访问必须经过TLS代理。若使用代理，`FORWARDED_ALLOW_IPS`只列可信代理精确地址，禁止`*`。

## 云端backend与本机GPU节点

云端使用`compose.base.yaml`叠加`compose.cloud.yaml`，通过Caddy终止公共HTTPS。应用和Qdrant不发布宿主端口，Qdrant留在internal data-plane；只有Caddy发布80/443。DNS、TLS证书、用户认证/账户策略和主机防火墙需由部署方配置。云端模型URL必须是容器可路由的私有/VPN HTTPS地址，CA文件通过`WENJIN_MODEL_API_CA_BUNDLE`显式信任，HTTP客户端不使用环境代理。Compose中的`model-egress`仅用于该宿主的出站路由，不会跨机器连接Windows GPU。

GPU节点可单独运行`compose.model-node.yaml`。模型服务容器自身不发布宿主端口；`model-edge`通过TLS反代提供接口，默认只绑定`127.0.0.1:18766`。跨机器时只可将监听绑定到受防火墙保护的VPN网卡，并令backend通过VPN可达的DNS名访问。证书SAN、`MODEL_NODE_TLS_NAME`、`WENJIN_MODEL_API_URL`与CA必须匹配。不要将Qdrant或未加TLS的模型API直接暴露公网。

先在GPU主机启动模型节点。跨机器时，先配置VPN地址、防火墙、证书和CA，再将`.env.model-node`中的`MODEL_NODE_BIND_ADDRESS`改为VPN网卡地址；默认loopback只供同宿主诊断：

```powershell
docker compose --project-directory . --env-file deploy/.env.model-node `
  -p wenjin-model-node -f deploy/compose.model-node.yaml config --quiet
docker compose --project-directory . --env-file deploy/.env.model-node `
  -p wenjin-model-node -f deploy/compose.model-node.yaml up -d --build
docker compose --project-directory . --env-file deploy/.env.model-node `
  -p wenjin-model-node -f deploy/compose.model-node.yaml ps
```

然后在云主机设置`WENJIN_MODEL_API_URL`为模型节点实际私网HTTPS URL，`MODEL_NODE_CA_BUNDLE_FILE`指向该证书链的CA。将`PUBLIC_DOMAIN`改成已配置DNS的域名，确保`WENJIN_MCP_ALLOWED_HOSTS`匹配外部Host。默认`proxy-plane`的`172.30.250.0/29`保留`.2`给Caddy、`.3`给管理HTTP、`.4`给MCP；若网段与现有Docker网络冲突，必须同时换子网和这三个静态地址，再启动backend、MCP、Qdrant和Caddy：

```powershell
docker compose --project-directory . --env-file deploy/.env.cloud `
  -p wenjin-cloud -f deploy/compose.base.yaml -f deploy/compose.cloud.yaml config --quiet
docker compose --project-directory . --env-file deploy/.env.cloud `
  -p wenjin-cloud -f deploy/compose.base.yaml -f deploy/compose.cloud.yaml up -d --build
docker compose --project-directory . --env-file deploy/.env.cloud `
  -p wenjin-cloud -f deploy/compose.base.yaml -f deploy/compose.cloud.yaml ps
```

云端首次管理员使用同一CLI交互引导：

```powershell
docker compose --project-directory . --env-file deploy/.env.cloud `
  -p wenjin-cloud -f deploy/compose.base.yaml -f deploy/compose.cloud.yaml `
  exec management python -m wenjin.file_management bootstrap-admin `
  --identity-db /var/lib/wenjin/identity.sqlite --username admin
```

启动后分别检查`https://<PUBLIC_DOMAIN>/api/health`、管理页登录与MCP的`initialize`、`notifications/initialized`及带`MCP-Session-Id`的后续请求。若云backend容器无法通过实际VPN路由解析并连接模型节点TLS地址，健康接口可响应也不表示检索可用；不要改成未加密HTTP或关闭证书验证绕过。

云端和模型节点可以由不同操作者按同一模型ID与`/v1/identity`指纹核对，但不能只以维度相同推断模型一致。请求与响应均校验模型ID、权重资产hash、服务实现hash和协议身份；不匹配时请求失败，不回退到云端本地模型。`WENJIN_MODEL_API_TOKEN`在backend和模型服务间使用同一外部secret；`WENJIN_FILE_ADMIN_TOKEN`只挂给管理服务，不共享给MCP。

每个模型API进程/副本把embedding或rerank的单次调用送入自己的单worker FIFO；query/document embedding和rerank共用该队列。身份响应广告`active=1`、最多8个等待位和30秒排队预算。FIFO按单次调用的实际入队顺序，不代表整条query公平；多个模型服务副本也没有共享队列或统一仲裁。backend模型客户端默认read timeout为90秒，必须大于服务端广告的等待时间，connect/pool分别为5秒；remote模式下MCP管理桥默认read timeout为210秒、connect/pool也单独配置，且配置校验要求bridge read严格大于模型客户端read。可通过`WENJIN_MODEL_API_*_TIMEOUT_SECONDS`与`WENJIN_FILE_MANAGEMENT_*_TIMEOUT_SECONDS`覆盖。210秒只是这条桥的读取预算，不是整条查询SLA；多库检索可能依次发出多个模型调用，并等待管理端检索锁。模型准入满或排队超时分别返回`model_queue_full`/`model_queue_timeout`，不自动重试。

## 数据恢复与升级

`wenjin-data`包括索引发布/候选等受管数据，`wenjin-identity`保存身份SQLite，`wenjin-artifacts`保存任务及其检查点；`qdrant-data`保存向量。另需备份共享的BM25、registry、corpora manifest与raw原文。它们必须是同一版本组；只恢复`data/indexes`或只挂一个新建的空Qdrant卷，都不能恢复现有可检索库。SQLite备份须在一致性快照中包含需要的WAL状态，发布索引、原文、任务结果和Qdrant快照也需按同一时间点协调。

下面的PowerShell示例先停stack，再把Docker命名卷归档到本机目录。将`$project`设为实际Compose项目名；共享只读目录`corpora/`、`configs/`、`data/indexes/`与`raw_data/`还需在同一维护窗口做文件系统快照。secret、TLS私钥与CA单独加密备份，不放入普通卷归档：

```powershell
$project = 'wenjin-cloud'
$backupDir = (New-Item -ItemType Directory -Force '.\backups\wenjin-cloud').FullName
docker compose --project-directory . --env-file deploy/.env.cloud `
  -p $project -f deploy/compose.base.yaml -f deploy/compose.cloud.yaml stop
foreach ($volume in @('wenjin-data','wenjin-identity','wenjin-artifacts','qdrant-data')) {
  $sourceVolume = "${project}_${volume}"
  docker volume inspect $sourceVolume *> $null
  if ($LASTEXITCODE -ne 0) { throw "源卷不存在，停止备份：$sourceVolume" }
  $archive = Join-Path $backupDir "$volume.tar.gz"
  $helper = docker create --volume "${sourceVolume}:/volume:ro" alpine:3.20 `
    tar -czf /tmp/volume.tar.gz -C /volume .
  docker start --attach $helper | Out-Null
  if ($LASTEXITCODE -ne 0) { docker rm $helper | Out-Null; throw "卷归档失败：$volume" }
  docker cp "${helper}:/tmp/volume.tar.gz" $archive
  docker rm $helper | Out-Null
}
```

恢复演练必须使用新Compose项目名、新命名卷和隔离的共享目录副本。先创建`<restore-project>_<volume>`空卷，再用一次性容器将归档解入卷；确认`docker volume inspect`显示新前缀后，使用`-p <restore-project>`启动。先登录、验证发布完整性和MCP来源，再切流量。不要让恢复stack挂接生产卷或写生产Qdrant。缺少配套Qdrant快照或共享只读资产时，恢复stack保持隔离并标为不完整。

以下PowerShell片段把上面的归档恢复到空前缀为`wenjin-restore`的卷。恢复目录仍需在对应`.env`中指向隔离副本，绝不能指向原生产共享目录：

```powershell
$restoreProject = 'wenjin-restore'
foreach ($volume in @('wenjin-data','wenjin-identity','wenjin-artifacts','qdrant-data')) {
  $targetVolume = "${restoreProject}_${volume}"
  $existingVolumes = docker volume ls --format '{{.Name}}'
  if ($existingVolumes -contains $targetVolume) { throw "恢复目标卷已存在，拒绝覆盖：$targetVolume" }
  docker volume create $targetVolume | Out-Null
  $helper = docker create --volume "${targetVolume}:/restore" alpine:3.20 sh -c 'sleep 600'
  docker start $helper | Out-Null
  docker cp (Join-Path $backupDir "$volume.tar.gz") "${helper}:/tmp/restore.tar.gz"
  docker exec $helper tar -xzf /tmp/restore.tar.gz -C /restore
  docker rm -f $helper | Out-Null
}
docker volume ls --filter "name=${restoreProject}_"
```

新部署可直接使用`data/file-management/raw_data`作为受管原文根。已有受管数据库若记录了另一原文根，服务会拒绝以新路径启动；挂载旧`data`卷并设置新变量不会自动搬迁。迁移时先停止唯一manager，按原目录完整备份；复制原文到目标根后逐文件校验记录的hash，再按受控迁移步骤调整根绑定并在隔离副本启动验证。若尚无迁移工具，继续使用旧`root/raw_data`布局，不要手改SQLite路径。

通过独立Docker volume或存档备份恢复后，应先在隔离stack执行管理健康检查、身份登录、registry和发布完整性检查，再开放MCP流量。恢复也要校验MCP与管理使用相同的identity数据库、数据卷和模型身份；只恢复程序镜像不恢复业务状态。

## 本轮已验证与未验证

本轮local、cloud、model-node三套Compose组合使用合成路径和secret通过`docker compose config --quiet`。`wenjin-backend:2026-10-08`按锁文件常规构建成功（Python 3.12.3、UID 10001、135个runtime包，无torch/transformers推理栈）；容器`/tmp`在UID 10001下的写入检查通过。MCP私网HTTP监听与管理桥分别由独立显式开关控制，默认拒绝；`tests/test_mcp_server_proxy_config.py` 4项通过。cloud proxy-plane固定edge/management/MCP为`.2/.3/.4`，避免动态地址占用可信代理地址。

隔离cloud Compose使用正式backend镜像和Caddy完成TLS登录/MCP合同：`tests/test_deployment_tls_proxy_acceptance.py` 1 passed，错Origin返回403；同源登录设置Secure+HttpOnly cookie；个人MCP凭据经CSRF签发；`initialize`协商`2025-11-25`、返回session，随后`notifications/initialized`与同session `tools/list`成功。该合同不覆盖检索、上传或真实账户。Core Agent另用缓存ML依赖基底的源码镜像完成一次隔离CUDA embedding/rerank API请求，模型容器、网络和临时token均已清理；这是协议单例冒烟，不是质量评测，也不是完整`deploy/Dockerfile` model-node目标从零构建。命名卷marker归档/新卷恢复通过；只验证小文件往返，不是业务状态、Qdrant与共享资产的完整恢复演练。详细证据见[本轮部署日志](../artifacts/iterations/2026-10-08-dual-deployment/development-log.md)、[独立测试记录](../artifacts/iterations/2026-10-08-dual-deployment/test-development-log.md)和[审核记录](../artifacts/iterations/2026-10-08-dual-deployment/review-log.md)。

尚未验证原生Linux上UID 10001读取0600 secret、从零构建完整ML目标、跨宿主VPN/WAN和真实云服务器、2核/2GB容量，以及完整数据集备份恢复。当前云模板不表示已采购、部署或公网开放云服务器；本地Docker TLS合同不能替代远端部署验收。
