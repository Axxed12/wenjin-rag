# 方案与实现审核记录

## 第一批与第二批实施前审核

- 分工：root负责方案/汇总/审核；GPT-6 Luna/high开发owner和独立测试owner分别成功启动。只在独立worktree开发；文件归属见第一批计划。
- 依赖：实际resolver冲突排除FastMCP3+SDK2同环境；独立FastMCP4+SDK2实际安装及三项工具注册通过。不修改主lock/旧入口。
- 协议边界修正：HTTP现代路径携带旧session-id应忽略，不可据此选择身份；GET/DELETE为405。schema工具列表与受权限控制的资料目录分开判断。
- 下载隔离：原业务地址/files在同域代理会有入口归属冲突；新版响应/路由统一专属/mcp-stateless/files前缀，TLS联验须覆盖取件。
- 工具错误：顶层业务error需新适配层转为isError=true；正常结构化结果与协议JSON-RPC错误分别验收。旧payload标签只在新版适配，不全局修改旧协议常量。
- 审计：FastMCP4 Context.session_id在没有wire session时可生成内部UUID（安装态源码context.py约707-789）；新版旧handler不得把内部UUID记作协议session，开发owner以薄Context适配处理，并要求定向实测。
- 客户端证据：TypeScript harness曾从envelope._meta读serverInfo；规范身份属于result._meta，已通知测试owner修正并实际断言。请求bearer只记录存在标记，不输出值。

以上为审核发现与处理要求；最终通过/失败及未完成项由后续实际测试证据补充，不能将静态审核当验收。

## 第二/第三批检查点审核

- 官方TS SDK2.3.1实际loopback三RPC均200，meta/HTTP头/resultType/serverInfo/缓存/协议标签正确，SDK固定modern不降级。private/ttl0是SDK实际结果，不强制public缓存。
- 临时身份+真实管理HTTP桥+内存Qdrant合同通过：两用户相同旧session头不串身份；shared immutable A在切B后仍按A字节/hash取件，managed旧版替换后409、新版200；撤grant/credential及错误版本有实际输出。
- 协议version=123负例揭示SDK先比header而返回-32020；新版局部校验按Malformed meta回-32602。记录SDK边界，复验变更合同；正向业务链不重复运行。
- 新Caddyfile采用命名path matcher和显式route顺序，保留旧/files并处理新专属/files前缀；Caddy validate与Compose merge已通过（配置检查），尚不代表TLS业务可用。
- overlay初稿误挂file_admin_token，审核要求移除，维护唯一管理writer/个人MCP bridge边界；只读身份卷WAL访问需第四批实际验证。
- root逐文件hash检查原目录631个快照文件无变化，旧core/主lock/原部署配置无新diff；当轮新增链接27项无缺失。最终收尾会按新增产物刷新此检查，不重跑业务tests。

## 第四批进行中的审核要求

- 两进程SDK跨时代隔离已实测：旧SDK初始化/列表/调用保持旧协议，现代Client pin旧地址只discover后失败，无自动降级；旧SDK初始化新地址被拒且无session。
- 正式独立镜像构建成功；image CLI help仅证明导入与参数可用，仍需正式launcher、临时依赖与HTTPS客户端请求。临时help容器与应用监听/运行服务分开记录。
- 只读身份库需覆盖无WAL/SHM sidecar首次读取、管理writer更新后下一请求立即生效。保持人为writer连接或使用immutable读取陈旧库不能证明动态撤权可用；发现失败需交开发处理，不修改旧身份核心。
- 新版测试在旧SDK环境须在导入新版模块前显式skip，integration标记不足以避免collection导入失败；跨进程Python路径应可配置。只检查新增测试收集，不扩展历史测试范围。
- 新版factory静态Bearer模式遗漏bearer_enabled传递，会导致旧工具按匿名身份处理；已交开发owner在新版装配补传并定向验证，个人IdentityStore模式不受此遗漏影响。最终镜像需包含最终源码而非旧build快照。

## 最终审核结论

- 正式CLI/最终镜像经真实Caddy与官方TS SDK联验通过；下载内容与fixture精确hash一致，grant撤销的实际目录数组为空且取件403，重授后恢复。审核纠正了初稿按不存在的library_id字段断言可能假通过的问题，最终直接检查空数组并记录数量。
- WAL filesystem只读失败已保留；新版仅逻辑SQL只读，身份volume允许SQLite sidecars。独立容器实测无预制sidecar鉴权成功、query_only拒写、initialize拒绝、每无writer读段DB正文hash不变，以及外部writer热撤权/重授可见。
- 实际管理create_app经HTTPS验证Cookie、Origin、CSRF；旧路径只做合成upstream代理探针，旧SDK完整生命周期由另一TCP合同证明，范围明确。没有GUI、生产或真实模型能力外推。
- 镜像/源码绑定：root机器核对发现两条手工摘要拼写错误，已由owner动态计算纠正；测试owner在无卷临时容器内对五项源码/manifest/lock逐项验证匹配，宿主模板另列。源码/image无变化，无重复业务验收。
- 源码/测试owner已冻结；root提交独立分支。原开发目录及既有服务/真实数据不合并、不部署。清理仅剩自动策略拒绝的三处临时SQLite目录，明确保留，不声称全清理。
