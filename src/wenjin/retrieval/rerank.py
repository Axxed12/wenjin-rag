"""Qwen3 Reranker 的本地推理与候选重排。"""

from pathlib import Path

DEFAULT_INSTRUCTION = (
    "Given a software documentation query, retrieve source passages that provide "
    "the evidence needed to answer the query"
)
STRICT_SOURCE_INSTRUCTION = (
    "Given a software source-code query, judge whether the document directly provides evidence "
    "for every requested API, class, behavior, and version. Prioritize exact identifiers and "
    "requested source type. Do not treat a similar class or method as relevant."
)


def tokenize_pairs(tokenizer, pairs: list[str], *, max_length: int) -> dict:
    """先只生成token IDs；前后缀加入后再由pad统一构造attention mask。"""
    return tokenizer(
        pairs,
        padding=False,
        truncation=True,
        return_attention_mask=False,
        max_length=max_length,
        add_special_tokens=False,
    )


def candidate_document(candidate: dict) -> str:
    """把来源元数据和原始正文组成 reranker 文档输入。"""
    return (
        f"Source: {candidate['source_path']}\n"
        f"Lines: {candidate['start_line']}-{candidate['end_line']}\n"
        f"{candidate['text']}"
    )


def rank_candidates(candidates: list[dict], scores: list[float]) -> list[dict]:
    """按模型分数降序返回新列表；同分时保留原候选顺序。"""
    if len(candidates) != len(scores):
        raise ValueError("候选数量与reranker分数数量不一致")
    ranked = []
    for original_rank, (candidate, score) in enumerate(zip(candidates, scores), start=1):
        copied = dict(candidate)
        copied["rerank_score"] = float(score)
        copied["pre_rerank_rank"] = original_rank
        ranked.append(copied)
    return sorted(ranked, key=lambda item: (-item["rerank_score"], item["pre_rerank_rank"]))


class Qwen3Reranker:
    """按官方 yes/no 判别格式加载本地 Qwen3 Reranker。"""

    def __init__(
        self,
        model_path: Path | str,
        *,
        device: str = "cuda",
        max_length: int = 2048,
        batch_size: int = 8,
        instruction: str = DEFAULT_INSTRUCTION,
    ) -> None:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        if max_length < 256:
            raise ValueError("max_length过小，无法容纳指令、query和文档")
        if batch_size < 1:
            raise ValueError("batch_size必须大于0")
        self.torch = torch
        self.device = device
        self.max_length = max_length
        self.batch_size = batch_size
        self.tokenizer = AutoTokenizer.from_pretrained(
            model_path,
            local_files_only=True,
            padding_side="left",
        )
        dtype = torch.float16 if device.startswith("cuda") else torch.float32
        self.model = (
            AutoModelForCausalLM.from_pretrained(
                model_path,
                local_files_only=True,
                dtype=dtype,
                attn_implementation="sdpa",
            )
            .to(device)
            .eval()
        )
        self.no_token_id = self.tokenizer.convert_tokens_to_ids("no")
        self.yes_token_id = self.tokenizer.convert_tokens_to_ids("yes")
        self.prefix = (
            '<|im_start|>system\nJudge whether the Document meets the requirements based on '
            'the Query and the Instruct provided. Note that the answer can only be "yes" or '
            '"no".<|im_end|>\n<|im_start|>user\n'
        )
        self.suffix = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
        self.prefix_tokens = self.tokenizer.encode(self.prefix, add_special_tokens=False)
        self.suffix_tokens = self.tokenizer.encode(self.suffix, add_special_tokens=False)
        if not instruction.strip():
            raise ValueError("reranker instruction不能为空")
        self.instruction = instruction.strip()

    def _format_pair(self, query: str, document: str) -> str:
        return (
            f"<Instruct>: {self.instruction}\n"
            f"<Query>: {query}\n"
            f"<Document>: {document}"
        )

    def score(self, query: str, documents: list[str]) -> list[float]:
        """返回与documents顺序一致的相关性概率；空输入直接返回空列表。"""
        if not documents:
            return []
        scores = []
        content_budget = self.max_length - len(self.prefix_tokens) - len(self.suffix_tokens)
        if content_budget <= 0:
            raise ValueError("max_length不足以容纳reranker固定前后缀")
        for start in range(0, len(documents), self.batch_size):
            pairs = [
                self._format_pair(query, document)
                for document in documents[start : start + self.batch_size]
            ]
            encoded = tokenize_pairs(
                self.tokenizer,
                pairs,
                max_length=content_budget,
            )
            encoded["input_ids"] = [
                self.prefix_tokens + token_ids + self.suffix_tokens
                for token_ids in encoded["input_ids"]
            ]
            inputs = self.tokenizer.pad(encoded, padding=True, return_tensors="pt")
            inputs = {name: value.to(self.device) for name, value in inputs.items()}
            with self.torch.inference_mode():
                logits = self.model(**inputs, use_cache=False).logits[:, -1, :]
                yes_no_logits = self.torch.stack(
                    [logits[:, self.no_token_id], logits[:, self.yes_token_id]],
                    dim=1,
                )
                batch_scores = yes_no_logits.float().softmax(dim=1)[:, 1]
            scores.extend(batch_scores.cpu().tolist())
        return scores

    def close(self) -> None:
        """释放模型引用和CUDA缓存，便于后续顺序加载4B模型。"""
        import gc

        del self.model
        gc.collect()
        if self.torch.cuda.is_available():
            self.torch.cuda.empty_cache()
