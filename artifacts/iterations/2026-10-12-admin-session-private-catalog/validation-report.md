# 管理员签发的reader凭据与私域发布目录独立验收

日期：2026-10-12

## 范围和隔离

本轮只验证账号身份、资料库读取授权、私域target目录准入及检索快照一致性。新增HTTP合同使用 pytest `tmp_path`、独立SQLite身份库、Qdrant `:memory:` 和合成TXT；没有读取或写入正式资料、真实授权或运行服务，也没有重启服务。Node/UI和浏览器体验不属于本报告范围。

## 结果

新增 [test_admin_issued_reader_mcp_contract.py](../../../tests/test_admin_issued_reader_mcp_contract.py) 覆盖管理员通过 `POST /api/admin/users/{user_id}/credentials` 为reader签发凭据的完整HTTP边界：首次密码未修改时签发被拒；reader改密后签发成功；resolve返回目标reader的user_id与`role=reader`；MCP Bearer请求管理员API为401。没有资料库授权时MCP目录不泄露文件且检索为403；管理员授权后新的目录请求可见文件且检索返回合成证据；撤权后同一凭据的后续目录请求隐藏文件、检索为403。每项均为新HTTP请求，验证没有跨请求授权缓存。

只读检查 `src/wenjin/identity/store.py` 的签发/解析路径与管理HTTP路由：目标user_id不同于签发者时必须通过管理员检查；目标用户必须启用且完成首次改密；token记录绑定目标user_id；解析时从凭据关联用户行读取当前角色和状态。MCP Bearer仅由MCP入口解析，管理后台操作仍要求web session，因此签发者的admin角色没有传给reader凭据。

只读检查 `src/wenjin/retrieval/legacy_catalog.py`：私域裸target只是候选；只有`schema_version=1`且带非空`candidates`的route能参与选择，并且候选manifest必须位于`corpora-private/`。同一库多个不同target形成歧义时不发布，重复声明同一target会按target ID去重。目录快速可用性检查要求manifest、BM25、manifest SHA和Qdrant集合状态/点数/维度一致；搜索仍进行完整准入，并在返回前重新校验target与version。release catalog中已激活或曾激活的库不回退到private route/legacy配置；当前目录构造对同一次读取复用private-route快照。

## 验证命令

最终身份、授权、私域route、release迁移和检索竞态定向集合：

```text
uv run pytest tests/test_admin_issued_reader_mcp_contract.py tests/test_private_catalog_identity.py tests/test_admin_snapshot_consistency.py tests/test_identity_http.py tests/test_mcp_identity_http.py -q
```

结果：`10 passed, 17 warnings in 6.31s`。警告为Starlette测试客户端弃用提示及索引构建中Qdrant `recreate_collection` 弃用提示，没有测试失败。

这10项覆盖新签发HTTP合同1项、私域选择/歧义/损坏资源/迁移拒绝回退及授权HTTP合同3项、检索期间route/registry切换和原文变更3项、既有身份HTTP合同1项、状态化MCP身份/授权合同2项。

静态检查：

```text
uv run ruff check tests/test_admin_issued_reader_mcp_contract.py tests/test_identity_http.py
```

结果：`All checks passed!`

```text
uv run ruff format --check tests/test_admin_issued_reader_mcp_contract.py
```

结果：新增测试已格式化。对两个文件一起运行format check时，`tests/test_identity_http.py`另有既存的格式差异，涉及本次两处改动以外的多段代码；本轮没有为了格式化而改写整份既有测试文件。

## 验收调整与边界

既有 `tests/test_identity_http.py` 的mode断言已更新为分别核对`mode`、`bootstrap_open`和`session_policy`（空闲1800秒、绝对时限43200秒）。它还有一条MCP搜索拒绝断言此前携带文件替换前的旧version_id，服务按合同先返回409陈旧版本；测试现携带当前active version_id，以准确检验该断言意图中的授权拒绝403。

前期运行中观察到 `tests/test_private_catalog_identity.py` 的合成构建fixture曾将BM25 Path放在项目root外；后端修正夹具后，本轮最终私域组通过。另有 `tests/test_identity_store.py::test_web_and_mcp_tokens_are_separate_and_both_expire` 仍期待错用途token在resolve时抛出ValueError，而当前身份边界映射为AuthenticationFailed；该项不在本轮最终定向集合内，也未修改。

通过的是隔离SQLite、内存Qdrant、合成数据和Starlette TestClient合同。它证明这组代码路径的合同行为，不代表浏览器验收、生产身份系统验收、真实私域资产发布或服务部署验收。
