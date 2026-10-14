# 按库授权编辑器独立验收

日期：2026-10-13。验证仅使用Node合成DOM/fetch合同和pytest临时root、合成资料、内存Qdrant；未接触真实账号、grant、资料或运行服务。本轮只读审查前端实现，没有修改产品代码。

## 结论

编辑器以活动资料库为单位展示普通用户checkbox草稿，管理员自身不在编辑列表中。只有用户明确点击“保存修改”后，界面才为真正变化的reader逐项提交 `/api/admin/grants`，携带现有grant revision或新记录revision `0`；保存成功后逐项通知页面。取消只把checkbox恢复到读取时状态，不发授权请求。

停用reader已有允许规则仍可取消勾选撤权；停用账号没有允许规则时checkbox禁用，不能新授。目录中无法匹配的grant以只读说明展示，不进入待提交集合。部分CAS冲突会保留失败提示并重读服务端授权快照；如果重读失败，当前草稿锁定，直到成功重读，不会在 `finally` 中重新开放旧快照。

账号切换会使旧请求代次失效。未完成的旧管理员写入不会继续处理下一项，也不会向新账号发送含旧grant细节的 `wenjin-grants-changed` 事件。若旧POST在账号切换后返回成功，身份模块只发无payload的 `wenjin-access-catalog-invalidated`；当前页面清除证据、推进检索/目录revision并刷新当前账号目录。测试确认新reader的 `wenjin-account-ready` 先于迟到写入失效事件，目录测试确认中性事件丢弃旧搜索revision并重读目录。

## 验证证据

前端合同命令：

```text
node --test tests/admin_identity_auth_behavior.test.mjs tests/admin_frontend_catalog_refresh.test.mjs
```

结果：`34 passed, 0 failed`。授权合同覆盖按库checkbox草稿、只提交changed reader与CAS revision、admin与unknown grant保持不变、停用reader仅可撤销、取消不产生POST、第二项HTTP 409后的部分失败重读和准确状态、刷新失败锁定、账号切换期间停止后续写入及迟到成功触发中性失效事件。目录合同覆盖失效事件清除旧证据、递增 `searchRevision`、重新读取当前账号目录且不伪装成断开服务。

后端相关的隔离HTTP合同命令：

```text
uv run pytest -q tests/test_identity_http.py::test_identity_http_scopes_auth_grants_and_revocation_across_admin_and_mcp_routes
```

结果：`1 passed, 5 warnings`（2.69s）。该测试使用临时SQLite、合成受管资料与进程内Qdrant，核对账号和grant的 HTTP/MCP 隔离、reader grant 生效与撤权，不是对真实服务的操作。

## 范围边界

这组Node测试通过合成DOM/fetch验证前端行为，不构成真实浏览器视觉检查。HTTP合同在Starlette TestClient中运行，不覆盖真实浏览器、多个浏览器tab之间的同步，也不证明数据库/服务部署状态。网络中断恰好发生在服务器已提交grant而客户端收不到HTTP响应的场景无法从客户端确认写入结果；UI保留错误/锁定并要求重新读取，不能将未知结果显示成确定成功或失败。
