# Cached model-node runtime source review

Date: 2026-10-12. Scope: read-only dependency and model-config inspection for the isolated concurrency adapter. No model weights were mounted or loaded, no GPU was passed through, and no model API or benchmark traffic was started.

## Image and package identity

- Cached image: `wenjin-model-node:dual-smoke-20261008`, image SHA-256 `c9d4e5172eca901fcec1d3389c8c119183eacd8b8788e4a3fbb8838dc2fdca3e`; this matches the model-node image recorded in model-admission run `20261012T084409Z-6be28c`.
- The older `wenjin-model-node:2026-10-08` tag is absent. The image was queried by its existing `dual-smoke-20261008` tag.
- Metadata read inside the cached image, via `docker run --rm --network none --entrypoint python` with no mounts and no GPU flags:

  | Package | Cached version |
  |---|---|
  | torch | `2.11.0+cu128` |
  | transformers | `4.57.6` |
  | sentence-transformers | `5.7.0` |
  | llama-index-embeddings-huggingface | `0.8.0` |

- These versions match the current `uv.lock`; the model-node Dockerfile uses `uv sync --locked --group ml`. The five runtime source files below were hashed inside the cached image and in the host `.venv`; each pair matched exactly:

  | Source file relative to site-packages | SHA-256 |
  |---|---|
  | `sentence_transformers/sentence_transformer/model.py` | `3a31fa89962294dce3f30c3355f4c3bfacd6a86fd4e14f43e974068dbfa3e445` |
  | `sentence_transformers/base/modules/transformer.py` | `9d7b347f764cfc383ea1f95530eb1b32f2ba0adbe2ad80ca267f99097b0adf2d` |
  | `transformers/models/qwen3/modeling_qwen3.py` | `4b95c371fd26d40c69083dab36ac1eafd8cf82b415a0bb827275097c5ad2305b` |
  | `transformers/modeling_rope_utils.py` | `55cc0c8cb76f592ab178b4662adcbdf4ad5012bb0b4249c0a96603141f18e9ac` |
  | `llama_index/embeddings/huggingface/base.py` | `bc4405c4b90966985a75ac3e81a855d29abbaa5e4c64b49c3f5d1dedea2b3096` |

## Implementation findings

- `HuggingFaceEmbedding` 0.8.0 owns one `SentenceTransformer`. `SentenceTransformer.encode()` calls `self.to(device)` and `self.eval()` on every call (cached `sentence_transformer/model.py`); then it sorts inputs using character/item length and calls `self.preprocess()` before its forward pass. `_input_length()` uses local input length and does not invoke the tokenizer. Therefore the experiment adapter must keep per-call `to`/`eval` from mutating shared modules: replace them only on the isolated warmed instance with strict guards that fail unless requested device is the frozen device and every module is already in eval mode. Serialize `preprocess()` so the shared processor/tokenizer is never concurrently called.
- Sentence Transformers 5.7.0 `Transformer.forward()` builds call-local kwargs and calls the underlying model. Qwen3 4.57.6 forward builds position IDs, masks, rotary embeddings, and intermediate activations per call. It does not mutate weights. The dynamic RoPE decorator mutates `inv_freq`, `attention_scaling`, and cache-length fields only for `dynamic` or `longrope` rope types.
- Both mounted model config files report `rope_scaling=null`, `attention_dropout=0`, `use_cache=true`, and `Qwen3ForCausalLM` architecture. The Qwen3 model base `forward(use_cache=None)` creates `past_key_values` only when `use_cache` is truthy; the embedding call path does not provide that kwarg, so the observed default call does not create a cache. The reranker explicitly calls `self.model(..., use_cache=False)`. An isolated adapter may pass `use_cache=False` explicitly for embedding only if the 1-lane adapted control is first shown equivalent to the frozen baseline; apply identical semantics to all comparison lanes.
- `Qwen3Reranker.score()` currently serializes tokenizer formatting/padding and forward under the outer runtime lock. The experiment may hold a tokenizer-only lock around formatting/tokenization/padding, then run only local input tensors through the frozen eval model on the lane's assigned CUDA stream with `use_cache=False`. No tokenizer lock may be removed.
- A warmed fixed-device/eval wrapper, serial tokenizer preparation, read-only fixed weights, no dynamic RoPE/cache, and independent CUDA streams make a narrow isolated forward-overlap experiment plausible. They do not establish general thread-safety or authorize production lock removal. Keep warmup, close, and drain protected; on shutdown stop admission and wait for every active operation/stream event before model close. Fingerprint parameters and buffers before and after each run; any change invalidates the run.
- CPU `forward_start/forward_end` timing is insufficient for GPU overlap. Record CUDA start/end events on each lane stream relative to a shared origin event; synchronize only after the request batch, then compute per-lane device intervals and overlap. Emit both host dispatch and device execution events. Do not synchronize each event around every request, since that would serialize the intended measurement.

## Acceptance boundary

These sources support proceeding with the **isolated guarded adapter** described above. They do not prove that the adapter is correctly implemented, that CUDA kernels overlap, or that outputs remain equivalent under concurrency. Acceptance still requires review of the exact `srcdiff`, frozen program/config/image/model identities, parameter/buffer fingerprints, one model process, direct queue/lock/forward events, real per-lane CUDA event intervals, fixed-input error analysis, resource thresholds, and cleanup evidence. Any source/version/config mismatch is a stop condition; do not silently substitute a newer cache or drop a guard.

- 追加只读核对 `llama_index/embeddings/huggingface/base.py`（cached image, LlamaIndex HF0.8.0）：外层 `HuggingFaceEmbedding` 的 `PrivateAttr` 包含 `_model`、`_parallel_process`、`_target_devices`；构造参数为 `parallel_process=False`，init会保存到 `_parallel_process`。`_embed_with_retry()` 在 flag 为 True 时才调用 `start_multi_process_pool()`/`encode_multi_process()`，False时调用内层 `SentenceTransformer.encode(inputs, batch_size, prompt_name, normalize_embeddings, show_progress_bar)`。因此准入guard应检查 outer embedder 的 `_parallel_process`，不能在 inner `SentenceTransformer` 上找这个属性。inner ST 5.7.0 `encode()` 真实签名包含 `pool` 与 `device`；实验 adapter 可以额外 fail-closed 拒绝显式pool/多设备并限定device。证据为2026-10-12在缓存模型镜像中 `docker run --rm --network none --entrypoint python <cached-image-id> -c ... inspect ...`，未载入模型/权重且未启动GPU服务。
