# 安全说明

## 不应提交的内容

- `.env`及任何API key；
- `artifacts/tls/`、`*.key`和真实证书私钥；
- 模型目录和Docker离线tar；
- 审计日志及内部Query正文；
- Qdrant数据卷、快照和本地生成的SQLite索引。

## 当前安全能力

- Streamable HTTP Host白名单；
- 可选TLS证书和私钥；
- Bearer API key在MCP Session创建前校验；
- 客户端伪造身份Header被剥离；
- 成功调用记录客户端ID哈希；失败鉴权记录`mcp_auth_denied`；
- 审计默认不记录Query正文、原始Session、密钥或异常消息；
- Docker模型和索引只读挂载，根文件系统只读。

## 已知限制

当前鉴权是一把密钥映射一个客户端ID，不包含用户目录、角色、权限范围、动态吊销或轮换服务。
自签名证书只用于开发。团队部署应接入内网CA或认证反向代理，并结合网络隔离、日志访问控制
和密钥管理系统。

## 报告问题

在远程仓库配置安全联系方式前，不要在公开Issue中粘贴密钥、内部Query、日志或受限文档。
