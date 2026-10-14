# MCP Docker部署与离线交付

> 维护说明（2026-10-07）：以下命令从项目根目录执行。文中的旧实测数字只代表标注日期；本文搬迁未重跑环境、构建或验收。当前能力与未验证项见 [当前状态](../code.md)。

最后更新：2026-09-17。当前实测环境为Windows + Docker Desktop Linux容器 + NVIDIA GPU。
Linux目标机可使用相同镜像，但仍需复验驱动、NVIDIA Container Toolkit、挂载路径和权限。

## 1. 构建

```powershell
docker compose build mcp
```

镜像固定Python 3.12.3、uv 0.11.3，并用根目录`uv.lock`安装Linux依赖。模型不进入镜像，
运行时只读挂载。当前镜像`wenjin-rag-mcp:0.1.0`实测约15.04GB，摘要：
`sha256:6c2f7abc32f4359042f4ffa388e592d64d11143fc98c8ccaf80258b31abdfa7d`。

首次构建需要下载PyTorch和CUDA运行库，必须在外网环境完成；内网交付导入已构建镜像。

## 2. 配置

`.env`至少包含：

```dotenv
QDRANT_API_KEY=随机密钥
EMBEDDING_MODEL_PATH=E:/Models/Qwen3-Embedding-0.6B
RERANKER_MODEL_PATH=E:/Models/Qwen3-Reranker-0.6B
MCP_BIND_ADDRESS=127.0.0.1
MCP_PORT=8765
MCP_ALLOWED_HOSTS=127.0.0.1:8765,localhost:8765
```

跨机器测试时将`MCP_BIND_ADDRESS`改为`0.0.0.0`，并把服务器实际`IP:port`加入
`MCP_ALLOWED_HOSTS`。这只开放监听和Host校验，不等于TLS或用户鉴权；正式内网建议放在受控
反向代理后。

容器挂载：

- Embedding和Reranker目录：只读；
- `data/indexes`：只读，含BM25和active注册表；
- `corpora`、`raw_data`和`configs`：只读；其中`raw_data`不进入Git，通过外网下载或人工摆渡准备；
- 根文件系统：只读；
- `/tmp`：1GiB tmpfs，供LlamaIndex/HuggingFace临时缓存。

**镜像里不带访问策略，也不带非公开语料**（`.dockerignore` 排除 `configs/access/`、
`corpora-private/`、`configs/indexes-private/`）。两条后果：

- `configs/access/` 必须由挂载提供。不挂载时策略快照为空，判定是"默认拒绝所有人"：
  容器能起来、健康检查也过，但检索、列表、下载全部 403。这是 fail closed，不是故障。
- **非公开语料不在镜像支持范围内**。镜像不烘焙 `corpora-private/`，也不给它默认路径；
  要服务私有语料必须显式挂载 `corpora-private/` 和 `configs/indexes-private/` 两处，
  并确认 `configs/access/` 里对应的策略已授权。

开发期不要反复重建镜像；发布内容冻结后统一构建一次。

## 3. 启停与健康

首次克隆时Git中没有生成的SQLite/Qdrant索引。先按README启动Qdrant并完成双索引构建和
active验证，再启动完整服务。已有索引的部署可以直接执行：

```powershell
docker compose up -d
docker compose ps
docker compose logs -f mcp
docker compose stop mcp
```

预期Qdrant和MCP均为`healthy`。MCP健康检查只验证8765端口监听；功能验收仍需调用客户端：

```powershell
uv run python scripts/test_mcp_client.py "Viewer 的 run() 做什么？"
```

日志使用Docker `local`驱动，单文件10MiB、保留3个文件。`mcp_tool_audit` JSON默认只保存
Query/Session哈希，不保存Query正文。响应`request_id`应与日志一致。

## 4. GPU与离线检查

```powershell
docker run --rm --gpus all --entrypoint python wenjin-rag-mcp:0.1.0 `
  -c "import torch; from mcp.types import LATEST_PROTOCOL_VERSION; print(torch.cuda.is_available(), torch.cuda.get_device_name(0), LATEST_PROTOCOL_VERSION)"
```

预期GPU为可用，协议为`2025-11-25`。运行容器设置`HF_HUB_OFFLINE=1`和
`TRANSFORMERS_OFFLINE=1`，不会从Hub下载模型。

## 5. TLS与Bearer鉴权

生成本机自签名证书（仅用于开发）：

```powershell
uv run python scripts/generate_dev_certificate.py --output-dir artifacts/tls `
  --hostname localhost --ip 127.0.0.1
```

在`.env`配置至少24字符随机密钥、客户端ID和证书路径，然后叠加安全覆盖：

```powershell
docker compose -f compose.yaml -f compose.secure.yaml up -d
```

客户端需要同时提供Bearer和CA证书：

```powershell
uv run python scripts/test_mcp_client.py "Viewer 的 run() 做什么？" `
  --url https://127.0.0.1:8765/mcp `
  --ca-cert artifacts/tls/server.crt
```

API key可用`WENJIN_MCP_API_KEY`环境变量或`--api-key`传入。缺失/错误密钥应返回401，正确密钥
才建立MCP Session。服务端剥离客户端伪造的身份Header，根据配置注入客户端ID；审计只保存
客户端ID哈希。401输出`mcp_auth_denied`结构化事件，不记录Authorization。

当前是一把密钥对应一个客户端ID，适合小团队MVP，不具备多用户、角色、动态吊销和轮换中心。
正式环境使用内网CA证书或在认证反向代理终止TLS；不要使用提交仓库的自签名私钥。

## 6. 镜像摆渡

外网机导出：

```powershell
docker save wenjin-rag-mcp:0.1.0 -o wenjin-rag-mcp-0.1.0.tar
Get-FileHash -Algorithm SHA256 wenjin-rag-mcp-0.1.0.tar
docker save qdrant/qdrant:v1.19.1 -o qdrant-1.19.1.tar
Get-FileHash -Algorithm SHA256 qdrant-1.19.1.tar
```

内网机核对SHA256后导入：

```powershell
docker load -i wenjin-rag-mcp-0.1.0.tar
docker load -i qdrant-1.19.1.tar
```

还必须一并摆渡并校验：两个模型目录、`data/indexes`、语料包、Compose文件和`.env`模板。
镜像不能代替Qdrant数据卷快照或原始语料。

## 7. 已知边界

- 当前完成本机自签名TLS和单Bearer身份验证，尚未完成另一台机器、企业CA或多用户鉴权。
- 镜像约15GB；需预留导出tar、Docker层和模型目录空间。
- active注册表切换后需重启MCP容器加载新目标。
- 容器内索引挂载为只读；构建和管理员切换当前在宿主机执行，再重启容器。
- 首次构建曾发现虚拟环境PATH和只读缓存目录问题，现已分别通过`PATH`和`/tmp`修复。
