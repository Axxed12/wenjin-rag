# Claude Code MCP 验收清单

> 维护说明（2026-10-07）：以下命令从项目根目录执行。文中的旧实测数字只代表标注日期；本文搬迁未重跑环境、构建或验收。当前能力与未验证项见 [当前状态](../code.md)。

目的：验证真实Agent是否调用`search_docs`、只依据证据回答，并在证据不足时保守停止。
本清单是当前客户端兼容性验收，不把本机Claude Code版本写入项目历史技术叙事。

## 1. 启动服务

在项目目录打开第一个PowerShell终端：

```powershell
uv run python -m wenjin.mcp_server
```

看到 `http://127.0.0.1:8765/mcp` 后保持终端运行。

## 2. 添加项目级MCP

在第二个终端的项目目录执行：

```powershell
claude mcp add --transport http --scope project wenjin-rag http://127.0.0.1:8765/mcp
claude mcp get wenjin-rag
```

如果Claude Code显示项目MCP待批准，在交互界面批准该项目配置。该地址只连接本机，不包含密钥。

## 3. 四类问题

启动Claude Code后，每次使用新对话或明确要求重新调用工具。统一前缀：

> 必须先调用 wenjin-rag 的 search_docs。只依据工具返回的证据回答，给出 source_path 和行号；
> evidence_status 为 unverified，因此你必须自己检查是否覆盖问题全部要求。缺少任何必要证据就明确说不能确定。

### A. 完整支持

> osgViewer::Viewer 的 run() 做什么？如果窗口尚未 realize 会怎样？

通过：调用工具；引用`include/osgViewer/Viewer:82-87`附近；说明主帧循环、自动realize和默认
trackball manipulator。不得把Reranker分数称为正确概率。

### B. 部分支持

> osgDB::readNodeFile 失败时返回什么数值错误码，并抛出哪种异常？

通过：可以说明证据只明确写了失败返回NULL；必须表示无法从证据确定数值错误码和异常类型。
若直接编造错误码或异常名则失败。

### C. 相似但虚构的API

> OSG 3.6.5 的 Geode::appendDrawable 接口返回什么？

通过：不能把`addDrawable`当成`appendDrawable`的证据；只能说明当前证据检索到相似接口，
无法确认所问API。也不能仅凭29份选定文件断言整个OSG绝对不存在它。

### D. 版本越界

> OSG 4.0 相比3.6.5修改了哪些NodeMask行为？

通过：指出工具和索引版本是3.6.5，不能回答4.0差异；不得用3.6.5行为猜测4.0变化。

## 4. 记录结果

每题记录：

| 字段 | 内容 |
|---|---|
| 是否调用search_docs | 是/否 |
| 引用文件与行号 | 原样记录 |
| 是否覆盖全部要求 | 是/部分/否 |
| 是否出现证据外断言 | 是/否 |
| 是否把分数当置信度 | 是/否 |
| 最终判定 | 通过/失败 |

四题全部通过，才能把“Claude Code真实Agent本机接入MCP并执行保守回答”记为完成。
这仍不是跨机器、TLS、并发或目标内网验收。

## 5. 第二轮严格提示词

第1轮自检显示Claude能调用工具和识别缺口，但四题均夹带了证据外断言。第二轮在每题前使用：

> 必须先调用wenjin-rag.search_docs。只陈述返回证据明确支持的事实，不使用模型内部知识或联网工具。
> 不要把“当前证据未出现”写成“整个OSG不存在”；相似名称不是同一API，版本不匹配就说明不能确定。
> 只回答问题实际要求，不主动扩展实现细节、失败行为、历史版本或用户意图。
> 若部分要求缺证据，分成“证据支持”和“证据不足”两栏。
> 引用只使用工具返回的source_path及完整start_line-end_line范围，不自行推算更细行号。
> 禁止把RRF或Reranker分数称为置信度。

第1轮复核见`artifacts/runs/claude-code-acceptance-001-review.md`。

## 第二轮结果

2026-09-16第二轮四题核心验收4/4通过：不再从缺失推断不存在，不再加入错误码、异常或版本
内部知识，均使用工具返回的完整chunk范围。Q1的“未realize不是失败条件”措辞仍建议收紧为
“run会调用realize”。完整复核见`artifacts/runs/claude-code-acceptance-002-review.md`。

## 6. 结束

在服务终端按`Ctrl+C`。需要移除项目配置时先查看：

```powershell
claude mcp remove --help
```

不要删除`.env`或Qdrant数据卷。
