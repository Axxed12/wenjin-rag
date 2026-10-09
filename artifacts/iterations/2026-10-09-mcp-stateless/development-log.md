# MCP 2026-07-28 无状态协议个人迭代

## 2026-10-09 接手、隔离与第一批设计

问题：聊天worktree位于旧HEAD，缺少当前未提交的管理/身份/部署模块；E盘原开发目录大量未提交修改。原因：这些功能尚未提交到原Git HEAD。处理：只读复制代码/文档到独立worktree、建立基线提交842f1a2，再开发。证据：[快照hash](handoff-snapshot.json)。不切换/reset/stash原目录，不操作原服务、数据、凭据、索引或数据库。

问题：现有协议限制要求2025-11-25，主lock为FastMCP3.4.4/SDK1.29.0。处理：按用户最新授权将本轮明确归于截止日后的个人迭代，旧入口规则继续保留；评估独立FastMCP4.1.0/SDK2.3.0环境和服务，不在主环境升级。依据、改动范围与验收顺序见[第一批计划](phase-1-plan.md)。两名GPT-6 Luna/high子Agent已实际启动，开发/测试文件分开。

检索过程：agent-reach Exa后端报告本机证书错误，gh只读API因未登录拒绝；切换可用web工具直接读取官方规范/SDK/框架文档，未修改证书信任或登录配置。agent-reach check-update显示1.5.0为当前版本。

状态：第一批官方核验与方案已记录；依赖/API实际probe和独立客户端registry证据仍在进行。第二至第四批尚未验收；真实模型、真实语料、冻结集、WAN与生产验收未执行。

## 第一批依赖与客户端实测检查点

开发owner实际运行resolver：FastMCP3.4.4 + mcp2.3.0不可解（fastmcp-slim3.4.4要求mcp<2）；独立FastMCP4.1.0 + mcp2.3.0可解并安装。Context、get_http_request/get_http_headers仍可导入；HTTP装配API由streamable_http_app转为http_app。证据见[依赖probe](dependency-evidence/fastmcp-sdk-compatibility.md)。原主pyproject/uv.lock和原环境未升级。框架工具decorator/Context运行适配probe通过后方可进入最小实现。

独立测试owner从npm registry核验并实际安装官方TypeScript Client2.3.1，固定modern模式脚本语法及公开Client API核对通过，尚未连接服务。见[独立测试计划](phase1-test-plan.md)和[测试记录](test-development-log.md)。基础Python Client固定SDK2.3.0，新旧协议/SDK/框架版本分别记录。

审核修正：modern-only HTTP对旧Mcp-Session-Id/Last-Event-ID按规范忽略，不用于身份或会话绑定、不生成/回显session；GET/DELETE返回405。静态工具schema可统一公开，资料目录与工具调用/下载仍逐请求授权，不能从工具schema缓存推断资料权限。
