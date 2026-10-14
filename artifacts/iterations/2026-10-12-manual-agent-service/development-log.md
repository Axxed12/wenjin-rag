# 本机手动Agent服务启动（2026-10-12）

用户要求启动管理、MCP和模型服务，以便手动验证个人鉴权及三客户端使用。未修改业务实现或启用实验两路；正式单路模型队列保持active1、等待8、30秒。

## 当前拓扑

- 模型：Docker项目`wenjin-manual`，容器`wenjin-manual-model`，仅loopback18766，缓存ML镜像`wenjin-model-node:dual-smoke-20261008`加当前正式src只读挂载；两份0.6B模型权重只读，各一份实例。镜像是缓存依赖基底，不声称正式完整model-node镜像从零构建验收。
- 管理：主uv环境隐藏后台进程，loopback18777，项目root为当前仓库。
- MCP：主uv环境隐藏后台进程，loopback18765/mcp，正确可执行模块为`wenjin.mcp_server`；`wenjin.mcp.server`不包含模块main调用，直接-m不会启动服务。
- 管理/MCP通过远程模型客户端连接唯一模型节点；现有Qdrant保持运行，未复制或迁移索引。现有受管资料目录继续由唯一管理进程服务；没有上传、删除、授权等业务操作。
- 两服务共用新身份库`data/file-management/manual-agent-identity.sqlite`。旧账号库不修改。无首位管理员、无自动授权；用户交互设置首位管理员密码后再创建测试用户。
- 模型token文件在忽略的`deploy/secrets/manual-agent/model_api_token`，不输出内容。

## 命令与检查

启动：`uv run --no-sync python artifacts/iterations/2026-10-12-manual-agent-service/start_services.py`

检查：`uv run --no-sync python artifacts/iterations/2026-10-12-manual-agent-service/verify_services.py`

模型加载前GPU空闲10152MiB，宿主可用9744600KiB；检查时无既有管理/MCP/模型实例，端口未占用。第一轮原生服务因未显式设置远程模型ID而退出；补齐两项ID后管理启动成功。异常日志的配置input_value已脱敏。MCP入口由无main调用的模块改为兼容可执行入口后启动成功；业务源码未改。

原始脱敏检查见[startup-checks.json](startup-checks.json)，模型身份见[model-identity.json](model-identity.json)，启动配置见[compose.json](compose.json)，进程记录见[processes.json](processes.json)。管理健康/页面200，无凭据模型和MCP401，真实embedding1024维与reranker有限分数检查。尚未创建账号，故本轮未验证带个人凭据的MCP初始化/检索与权限矩阵，不把端口监听当端到端验收。

## 用户下一步

在仓库根终端执行：

```powershell
uv run --no-sync python -m wenjin.file_management bootstrap-admin --identity-db .\data\file-management\manual-agent-identity.sqlite --username admin
```

交互输入密码后打开`http://127.0.0.1:18777/admin/`。管理员仍须显式读取授权，reader个人MCP token不等于模型内部token。

模型容器restart unless-stopped；管理/MCP为本轮后台进程，没有安装Windows开机服务，不宣称重启自动恢复。服务按用户请求保留运行，不执行实验cleanup。停止前验证进程命令与根目录，不能按不明PID误杀其他服务；当前进程入口/监听PID可由processes.json与Get-NetTCPConnection核对。
