# MCP 1/2-lane独立结果对照

日期：2026-10-12。以下数据只从各run冻结目录的原始MCP请求、服务端model events、outcome、资源采样和cleanup复算。MCP 2-lane启动失败没有有效请求样本，故不计算两档差值或吞吐提升。首轮 `20261012T125406Z-4a9f0c` 是另一条失败partial，不覆盖成功MCP1。

## 阶段结果

| Run | Lanes | MCP tool calls | 合同通过 | 性能查询 | Ready (s) | 结果/使用范围 |
|---|---:|---:|---:|---:|---:|---|
| `20261012T125703Z-2d27c2` | 1 | 35/35 HTTP 200 | 35/35 | 24/24，shared 12、managed 12 | 56.434 | MCP1有效全链路结果 |
| `20261012T125923Z-b93726` | 2 | 未发出 | 不适用 | 0 | 55.274 | `model_identity_invalid` startup failure；不是MCP行为样本 |

MCP1的35次调用分解：2次无权身份拒绝、2次warmup、24次性能查询（3轮，每轮8请求）、1次过期version拒绝、4次query-ingest并发前置查询、2次上传后managed readback。24个性能调用全成功。每轮client窗口耗时2.872/2.650/2.531秒。共享/受管fixture均18 chunks；两次新增文件各18 chunks，readback均按新file/version/marker通过。

## MCP每调用端到端时延

算法：从`mcp-requests.jsonl`各row `elapsed_seconds`直接换算毫秒。Median使用`statistics.median`，nearest-rank P50与P95另算；样本数与max并列。

| Family | n | Median ms | Nearest-rank P50 ms | P95 ms | Max ms |
|---|---:|---:|---:|---:|---:|
| Shared performance | 12 | 1060.71 | 1034.10 | 1587.86 | 1587.86 |
| Managed performance | 12 | 1074.73 | 1028.74 | 1396.52 | 1396.52 |

这包括MCP会话/授权、检索、embedding及重排路径，不可解释为单纯模型耗时。

## 每次查询的真实召回/重排行为

| Scenario | Calls | Dense returned | BM25 returned | Fused candidates | `reranker_used` | Evidence chunks |
|---|---:|---:|---:|---:|---|---:|
| Shared perf | 12 | 10 each | 10 each | 12 each | true, 12/12 | 5 each |
| Managed perf | 12 | 10 each | 10 each | 12 each | false, 12/12 | 5 each |
| Managed post-ingest readback | 2 | 10 each | 10 each | 13 each | false, 2/2 | 5 each |

两次competition各含shared与managed并发前置查询，四次查询的metadata分别与各自上述分支相同；两次readback绑定刚上传的active file/version并命中marker。

原始metadata与冻结源码一致：shared走`_legacy_service`，它把`shared_reranker`传给shared检索service；managed走`mcp_search`→management `_search`→`FileManagementService.search`→新建`RetrievalService`，该调用链没有传reranker。因此只能称shared MCP做了模型rerank；managed MCP路径本轮是dense/BM25召回+RRF，不是rerank。REST模型reranker独立性能测试不改变managed MCP实际路由结论。

## 模型API子调用事件

MCP1 `model-events.jsonl`共1106 events，62个model API HTTP request/complete对，55个admission/operation，130个forward_dispatch/end。CPU服务事件直接区间统计如下：

| Interval | n | Median ms | Nearest-rank P50 ms | P95 ms | Max ms |
|---|---:|---:|---:|---:|---:|
| Admission enqueue → worker dequeue | 55 | 1.06 | 1.06 | 200.64 | 297.55 |
| Operation enter → operation exit | 55 | 57.81 | 57.81 | 325.96 | 412.73 |
| Model API HTTP receive → completion | 62 | 107.41 | 103.18 | 388.46 | 619.38 |
| CUDA embedding forward event interval | 40 | 42.47 | 42.47 | 298.99 | 306.31 |
| CUDA reranker forward event interval | 90 | 34.50 | 34.50 | 70.08 | 98.93 |

该服务为1 lane，CUDA时间线峰值同时活跃lane数=1，没有模型多lane overlap。Operation按类型计55个：`embedding:query=32`、`embedding:document=8`、`rerank=15`；forward_end为embedding query 32、embedding document 8、reranker 90（一次rerank operation会含多candidate forward事件）。模型API event request IDs与MCP client tool-call IDs不是同一命名空间，因此不作虚假的逐MCP-call ID join；每层只报告可直接关联的原始边界。

## 资源和清理

MCP1 GPU/host资源原始采样174行：GPU free最小4945 MiB，host free最小3736 MiB（距3072 MiB停止线664 MiB），GPU utilization最高100%；采样间隔中位/max为563/594 ms。Docker采样24行，model-node 23条有效行，RSS最大约2763 MiB、CPU最高123.97%，Docker采样间隔中位/max约4407/4766 ms，名义2秒。阈值未触发，formal/frozen/model state unchanged；cleanup containers/volumes/networks均0且临时目录删除。

Ready只报告`container process→ready`，本轮56.434秒含自动warmup；OS cache已热。Cold HTTP inference及磁盘冷启动均未测。

## 失败run隔离

- `20261012T125406Z-4a9f0c`：MCP前置步骤记录31条调用后，因新文件grant前未同步资源目录而404。后续加入`GET /api/admin/resources`同步；该失败partial不计通过/性能样本。
- `20261012T125923Z-b93726`：在MCP流量前以`ModelProviderError/model_identity_invalid`退出，outcome fail；无MCP request rows。formal/frozen/model state unchanged、cleanup为0。不能称它证明MCP2失败或通过。

因此当前仅有有效MCP1端到端结果。需要新的同源有效MCP2 run，才能完成MCP 1/2对照；模型API 2-lane的独立通过不替代这一结论。
