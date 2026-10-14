# 前端交付记录：资料库分类与检索可见性说明

日期：2026-10-12

## 本轮变更

- 文件分组头新增管理员可用的“资料库分类”控件，可显示并设置“公开资料 / 私有资料”；普通账号不显示管理控件。
- 分类从资料库分类接口读取并随目录刷新。保存使用 `expected_revision` 做并发版本校验；遇到 409 时重新读取当前分类并提示管理员核对后重试。
- 分类更新只刷新分类元数据和目录，不调用逐人授权接口，也不更改共享/私域来源的检索能力。界面说明公开/私有仅作管理分类，普通账号查看与检索仍按授权执行。
- 调整文件页、检索页、资料库授权、上传和评测相关说明：管理员可读取所有资料库；目录中有资料或版本不代表存在当前可检索来源；普通账号按逐人授权；资料库分类与来源公开/私域属性分开表达。
- “创建可管理副本”说明为复制原文并建立独立受管索引，不修改原库，也不会授予读取权限；强调仅在需要独立替换或删除时使用，保留原有确认流程。

## 接口合同

- `GET /api/admin/libraries/{library_id}/classification` 返回 `{classification:{library_id,classification,revision,defaulted,source_visibility}}`。
- `PUT` 同路径，提交 `{classification:"private"|"public",expected_revision}`；目录列表同步携带分类对象。
- 分类是管理元数据标签，不是访问策略；不会授予/撤销用户权限。读取授权由现有逐人授权流程决定。

## 验证

- `node --check src/wenjin/file_management/admin/app.js`：通过。
- `node --check src/wenjin/file_management/admin/identity.js`：通过。
- `node --test tests/admin_frontend_catalog_refresh.test.mjs tests/admin_identity_auth_behavior.test.mjs`：30 项通过，0 失败。新增行为检查覆盖分类读取、revision CAS 保存、目录同步及不触发授权 API。
- `git diff --check`（本轮相关前端和测试文件）：通过。
- 未执行浏览器验收、真实账号操作或 grant 修改；本报告只记录 Node 行为与语法验证。

## 相关文件

- `src/wenjin/file_management/admin/app.js`
- `src/wenjin/file_management/admin/index.html`
- `src/wenjin/file_management/admin/styles.css`
- `tests/admin_frontend_catalog_refresh.test.mjs`

## 2026-10-13 前端复核补充

- 分类编辑卡从资料库 `<details>` 外移入折叠内容区，避免它与资料库列表形成桌面双列布局；默认 summary 显示紧凑的公开/私有分类徽标。未就绪受管文件归组也采用相同的徽标与折叠编辑布局。
- 来源显示识别共享源的 `source_visibility: private|internal`，并保留 `private_config` / `private_candidate` scope 判定。来源标签和检索资格不读取资料库 `classification`；release_active 私域源按目录 `readable/searchable` 能力处理，未发布候选仍禁用。
- ≤560px 下编辑控件变为窄屏布局，分类选择器独占一行，操作按钮并排，分类卡留在展开后的资料库内容内。
- 验证：`node --check src/wenjin/file_management/admin/app.js` 通过；`node --test tests/admin_frontend_catalog_refresh.test.mjs tests/admin_identity_auth_behavior.test.mjs` 31/31 通过，覆盖摘要徽标和编辑器的 DOM 嵌套，以及 `source_visibility=private` 的 release_active 显示/检索判定；相关文件 `git diff --check` 通过。未执行浏览器或服务重启。
