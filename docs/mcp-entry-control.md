# MCP入口配置与容器控制 API

此接口为管理页面提供旧/新版连接信息、默认入口配置和两个固定MCP Docker服务的启停控制。它只改变以后展示或复制的连接推荐，不会重配已有Agent，也不会因默认项改变而启停任一服务。管理页面只有启用账户身份库后才允许调用；必须使用管理员Cookie，所有写请求需带同源`Origin`和`X-CSRF-Token`。

旧入口按`2025-11-25`状态化协议保留`initialize`、`notifications/initialized`、能力协商及可选`MCP-Session-Id`。新版无状态入口使用`2026-07-28`协议。连接配置只返回端点和客户端要求，不返回MCP调用token，也不保证特定图形客户端支持该协议。

## 管理 API

所有路径为同源管理服务下的`/api/admin/mcp/...`。错误响应使用稳定英文`error`字段。

### `GET /api/admin/mcp/entries`

返回固定的旧/新版入口、所选默认项和Docker运行状态：

```json
{
  "entries": [
    {
      "entry_id": "legacy",
      "name": "旧版状态化 MCP",
      "protocol": "2025-11-25",
      "endpoint": "http://127.0.0.1:18765/mcp",
      "is_default": true,
      "runtime_state": "unmanaged",
      "runtime_label": "未受Docker管理",
      "generation": null,
      "control_error": "mcp_container_not_found",
      "control_error_detail": "未找到受管MCP容器；原生Python进程不能由此接口控制。",
      "control_configured": true
    }
  ],
  "default_entry": "legacy",
  "revision": 0
}
```

`entries`实际包含两个固定项目。`runtime_state`是Docker容器状态，`running`只表示容器运行中，不代表MCP协议或业务请求可用；另提供中文`runtime_label`。当前旧版若由Python原生进程运行，会呈现`unmanaged`，其`control_error`为`mcp_container_not_found`并给出`control_error_detail`；接口不会把它伪报为停止状态。`control_configured`只表示broker凭据与地址已配置。broker未配置时会把控制标为未接入；broker不可达时也会保留两项连接元数据，并用`control_error`/`control_error_detail`标记状态读取失败。

### `PUT /api/admin/mcp/default-entry`

请求：

```json
{"entry_id":"stateless","expected_revision":0}
```

成功返回新的`revision`、`default_entry`以及`effect:"future_connection_recommendation_only"`。基于旧revision的并发写返回409 `mcp_revision_conflict`。该设置不会切换、启动、停止服务，也不会修改既有Agent。

### `GET /api/admin/mcp/connection-config?entry_id=legacy`

`entry_id`可以省略；省略时按当前default配置返回连接信息。重复传入`entry_id`会返回422。返回`entry_id`、`name`、`protocol`、`transport`、`endpoint`、`auth:{scheme,credential_source}`和`requirements`。旧版要求initialize生命周期及可选会话头；新版要求`server/discover`和逐请求`_meta`。认证字段只说明客户端凭据来源，不含凭据本身。

默认地址可分别通过`WENJIN_MCP_LEGACY_PUBLIC_URL`和`WENJIN_MCP_STATELESS_PUBLIC_URL`覆盖。只接受HTTP或HTTPS URL；拒绝用户名、密码、query和fragment。服务端不会对这些地址发起网络探测。

### 启停操作与轮询

`POST /api/admin/mcp/entries/{entry_id}/actions`请求只接受`{"action":"start"}`或`{"action":"stop","expected_generation":"..."}`。成功立即返回202，包含`operation_id`、`state:"running"`、entry/action以及中文影响说明。传入的generation只用于检测容器是否被替换，不用作容器定位值。management请求体最大8 KiB；非法JSON返回400、超限返回413、错类型或额外字段返回422，响应带安全中文`detail`。broker端另外限制16 KiB并先验证Bearer再读取请求体，禁止代理转发或redirect。

通过`GET /api/admin/mcp/operations/{operation_id}`轮询，操作状态为`running`、`succeeded`、`failed`或`outcome_unknown`；还会返回`runtime_state`、中文`runtime_label`、`generation`和可能的稳定错误码及`error_detail`。operation在broker内有界保留，通常15分钟后过期；broker重启后原operation会返回410 `mcp_operation_expired`，不会报告成功。

broker启动时必须从`WENJIN_MCP_CONTROL_PROJECT`取得一个严格校验的Compose项目名，并在该进程内固定；这支持例如`wenjin`、`wenjin-cloud`等实际项目。entry到服务的映射仍硬编码为`legacy->mcp`、`stateless->mcp-stateless`。每次操作会重新验证Compose project/service标签和唯一容器；缺失、重复、旧generation分别返回`mcp_container_not_found`、`mcp_container_ambiguous`和`mcp_generation_conflict`。请求不能指定或覆盖Compose项目、服务、容器名或Docker ID。broker只执行已创建容器的start/stop，不创建、构建、删除、重启容器，也不控制管理、模型或Qdrant服务。

每个入口同一时间只允许一个动作。Docker调用预算为30秒，stop grace固定20秒，management动作读取预算为60秒。start只接受`created`/`exited`/`dead`状态，stop只接受`running`；start的`running`和stop的`created`/`exited`/`dead`为幂等成功。paused、restarting、removing、unknown等状态返回`mcp_container_state_conflict`。Docker接受动作后会再次inspect同一generation；若状态未达到目标，则标记`outcome_unknown`并阻止该入口新动作。只有刷新`GET /api/admin/mcp/entries`并确认同一generation达到目标状态后才解除锁定；轮询operation只读操作记录，不会触发状态复核。停止入口会中断该入口的客户端连接。容器状态读取不是MCP健康探测。

## 部署隔离

通过可选[Compose overlay](../deploy/compose.mcp-control.yaml)启动时，只有`mcp-control-broker`服务挂载Docker Engine Unix socket。管理容器只获得服务间HTTP地址和Bearer secret；broker不发布宿主端口，只加入内部`data-plane`网络。`WENJIN_MCP_CONTROL_PROJECT`必须填写与两个目标容器相同的准确Compose项目名；变量缺失或不合法时broker拒绝启动，project/service不能由API请求覆盖。broker访问会禁用系统代理和HTTP redirect。部署前需用当前源码重建backend镜像；broker healthcheck从挂载的secret文件读取凭据，管理服务只等待broker进程启动，不因可选控制broker暂不可用而阻止基础管理服务启动。secret路径、项目名和可公开连接URL示例见[环境变量模板](../deploy/.env.mcp-control.example)。

不配置broker URL或token时，管理页启停控制fail closed。无账户身份库的旧式管理模式不能调用这些端点，避免匿名控制容器。旧版Python原生进程不会通过Docker broker启停；只有Docker受管容器可被控制。

上述后端初轮的未部署状态见[历史开发记录](../artifacts/iterations/2026-10-14-mcp-entry-control/development-log.md)。后续页面与本机部署记录见[本轮记录](../artifacts/iterations/2026-10-14-mcp-entry-control-ui-deploy/development-log.md)，各轮验收范围分别保留。

## 管理页面与本机原生部署

管理员登录后从“MCP入口”进入两版入口卡片。“进入连接配置”展示并复制不含凭据的客户端配置；个人MCP凭据仍从身份与授权页面签发。“设为默认”只改变后续推荐地址，已有Agent需要自行更改客户端配置。启动/停止独立操作，停止前提示连接将中断；操作过期或结果不确定时刷新核对容器状态。

本机管理服务仍在 `http://127.0.0.1:18777/admin/`，旧版原生MCP仍在 `http://127.0.0.1:18765/mcp`，不能通过Docker按钮启停。只启动控制服务使用 [本机Compose配置](../deploy/compose.mcp-control.native.yaml)：

```powershell
docker compose -p wenjin-manual-mcp -f deploy/compose.mcp-control.native.yaml up -d --no-deps mcp-control-broker
```

broker绑定 `127.0.0.1:18769`；管理进程设置 `WENJIN_MCP_CONTROL_URL=http://127.0.0.1:18769` 和同一secret的 `WENJIN_MCP_CONTROL_TOKEN` 后重载。secret保存在被Git忽略的 `deploy/secrets/manual-agent/mcp_control_token`，不要写入页面、URL或日志。新旧公开地址分别用前述PUBLIC_URL变量指定。

这份本机配置与容器同网overlay不同：Docker Desktop实测internal-only网络没有路由宿主发布端口，故使用专用bridge网络并仅发布loopback端口。Docker socket仍仅挂载于broker；准确项目名固定为 `wenjin-manual-mcp`。它不能启停原生进程、创建新容器或控制模型/Qdrant。
