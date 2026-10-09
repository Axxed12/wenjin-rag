"""固定开发题集的服务端绑定与共享检索运行时装配。"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import platform
from pathlib import Path
from typing import Any

from wenjin.evaluation_tasks.service import EvaluationContext, EvaluationJob, EvaluationSpec
from wenjin.identity.policy import Forbidden

PROFILES_FOUR_ARM = ("baseline", "parent_child", "multi_query", "mmr")
_DATASET = "developer-validation-2026-10-04"


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _tree_fingerprint(path: Path) -> dict[str, Any]:
    """完整hash固定模型目录；不读取或返回凭据，也不加载模型进内存。"""
    from wenjin.retrieval.lazy_reranker import model_tree_fingerprint

    return model_tree_fingerprint(path)


def _configured_model_path(settings: Any, field: str, root: Path) -> Path:
    resolver = getattr(settings, "resolved_model_path", None)
    if callable(resolver):
        return Path(resolver(field, root=root)).resolve()
    path = Path(getattr(settings, field))
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def _load_rows(root: Path, experiment_id: str) -> tuple[list[dict[str, Any]], str, str]:
    dataset_path = root / "evaluation" / "datasets" / f"{_DATASET}.jsonl"
    sidecar = dataset_path.with_suffix(".sha256")
    try:
        raw = dataset_path.read_bytes()
        expected = sidecar.read_text(encoding="ascii").split()[0]
    except Exception as error:
        raise RuntimeError("固定开发题集或SHA256 sidecar不可用") from error
    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected:
        raise ValueError("固定开发题集SHA256与sidecar不匹配")
    rows = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    if experiment_id == "gdal_dstalpha_diagnostic_v1":
        rows = [row for row in rows if row.get("case_id") == "dv-gdal-003"]
    if not rows:
        raise ValueError("白名单题集没有匹配的开发题")
    result: list[dict[str, Any]] = []
    for row in rows:
        # 检索runner仅接收query与稳定路由字段；不把reference答案传入策略实现。
        result.append({
            "case_id": row["case_id"], "query": row["query"],
            "library": row["library"], "version": row["version"],
            **({"target_id": row["target_id"]} if row.get("target_id") else {}),
            **({"source_manifest_sha256": row["source_manifest_sha256"]}
               if row.get("source_manifest_sha256") else {}),
        })
    return result, actual, dataset_path.relative_to(root).as_posix()


def _config(experiment_id: str) -> dict[str, int]:
    # 两个模板都使用固定候选深度和上下文预算，调用方不能提交覆盖值。
    return {
        "candidate_k": 20, "top_k": 5, "rrf_k": 60,
        "max_libraries": 5, "rerank_pool": 30, "context_char_budget": 8000,
    }


class EvaluationRuntimeFactory:
    """由管理app构造；只借用manager资源，context仅关闭自有rewriter/reranker。"""

    def __init__(self, *, root: Path, app_state: Any) -> None:
        self.root = Path(root).resolve()
        self.app_state = app_state

    @staticmethod
    def specs() -> dict[str, EvaluationSpec]:
        return {
            "gdal_dstalpha_diagnostic_v1": EvaluationSpec(
                experiment_id="gdal_dstalpha_diagnostic_v1",
                title="单题诊断：GDAL 透明带选项",
                dataset_id=_DATASET,
                profiles=("baseline",),
                max_cases=1,
                required_libraries=("gdal",),
            ),
            "developer_four_arm_v1": EvaluationSpec(
                experiment_id="developer_four_arm_v1",
                title="固定开发题集：四种检索策略对比",
                dataset_id=_DATASET,
                profiles=PROFILES_FOUR_ARM,
                max_cases=50,
                required_libraries=("gdal", "ogc-3dtiles", "ogc-wms", "osgearth", "proj"),
            ),
        }

    def _runtime_parts(self):
        manager = self.app_state.manager
        qdrant = getattr(self.app_state, "qdrant_client", None) or getattr(manager, "qdrant_client", None)
        embedder = getattr(manager, "embedder", None)
        model_id = getattr(manager, "embedding_model", None)
        lock = getattr(manager, "_model_lock", None)
        if qdrant is None or embedder is None or not model_id or lock is None:
            raise RuntimeError("共享检索运行时未就绪，评测任务不可执行")
        return manager, qdrant, embedder, str(model_id), lock

    def _runtime_environment(self) -> dict[str, Any]:
        settings = getattr(self.app_state, "settings", None)
        remote = getattr(settings, "model_provider", "local") == "remote"
        common = {
            "provider": "remote" if remote else "local",
            "python_version": platform.python_version(),
            "platform": platform.platform(),
        }
        if remote:
            identity = getattr(self.app_state, "model_provider_identity", None)
            if not isinstance(identity, dict):
                raise RuntimeError("远程模型provider身份不可用")
            return {
                **common,
                "device": "remote",
                "device_name": "remote_model_api",
                "reranker_precision_policy": "reported_by_model_service_identity_boundary",
                "embedding_dtype_policy": "reported_by_model_service_identity_boundary",
                "model_service_identity_sha256": _sha(_canonical(identity)),
                "dependencies": {
                    package: importlib.metadata.version(package)
                    for package in ("llama-index-core", "qdrant-client", "httpx", "openai")
                },
            }
        import torch

        use_cuda = bool(torch.cuda.is_available())
        return {
            **common,
            "device": "cuda" if use_cuda else "cpu",
            "device_name": torch.cuda.get_device_name(0) if use_cuda else "cpu",
            "reranker_precision_policy": "float16" if use_cuda else "float32",
            "embedding_dtype_policy": "backend_default_not_explicitly_pinned",
            "torch_version": importlib.metadata.version("torch"),
            "cuda_runtime": getattr(torch.version, "cuda", None),
            "dependencies": {
                package: importlib.metadata.version(package)
                for package in ("llama-index-core", "qdrant-client", "transformers", "openai")
            },
        }

    def _code_fingerprint(self) -> list[dict[str, str]]:
        # 按已安装包真实执行位置取代码身份，数据root无需复制src目录。
        package_root = Path(__file__).resolve().parents[1]
        package_files = (
            "settings.py", "serving.py", "index_audit.py", "index_registry.py",
            "manifest.py", "ingestion/multiformat.py",
            "indexing/bm25.py", "indexing/dense.py",
            "releases/resolver.py", "retrieval/router.py", "retrieval/source_context.py",
            "retrieval/fusion.py", "retrieval/rerank.py",
            "retrieval/search_policy.py", "retrieval/lazy_reranker.py",
            "retrieval/strategy_experiment.py",
            "retrieval/query_rewrite.py", "evaluation_tasks/runtime.py",
            "evaluation_tasks/service.py", "models/providers.py",
        )
        result = [{
            "path": f"wenjin/{name}",
            "sha256": hashlib.sha256((package_root / name).read_bytes()).hexdigest(),
        } for name in package_files]
        from wenjin.retrieval.router import DEFAULT_MULTI_CORPUS_ROUTE

        route_path = self.root / DEFAULT_MULTI_CORPUS_ROUTE
        result.append({
            "path": str(DEFAULT_MULTI_CORPUS_ROUTE).replace("\\", "/"),
            "sha256": hashlib.sha256(route_path.read_bytes()).hexdigest(),
        })
        return result

    def _rewrite_configuration(self) -> dict[str, Any]:
        settings = getattr(self.app_state, "settings", None)
        base_url = getattr(settings, "query_rewrite_base_url", None)
        api_key = getattr(settings, "query_rewrite_api_key", None)
        model = getattr(settings, "query_rewrite_model", None)
        timeout = getattr(settings, "query_rewrite_timeout_seconds", 60.0)
        if not base_url or not api_key or not model:
            # 与既有QueryRewriteSettings使用同一配置源；密钥只留在进程内传给OpenAI客户端。
            # run identity/audit/API仅保存model ID和provider URL摘要。
            try:
                from wenjin.retrieval.query_rewrite import QueryRewriteSettings

                rewrite_settings = QueryRewriteSettings(_env_file=self.root / ".env")
            except Exception as error:
                raise RuntimeError("四臂评测缺少query rewrite provider配置") from error
            base_url = rewrite_settings.query_rewrite_base_url
            api_key = rewrite_settings.query_rewrite_api_key
            model = rewrite_settings.query_rewrite_model
            timeout = rewrite_settings.query_rewrite_timeout_seconds
        if not isinstance(base_url, str) or not base_url.strip() or not api_key or not model:
            raise RuntimeError("四臂评测需要主服务配置query rewrite provider与模型")
        secret = api_key.get_secret_value() if hasattr(api_key, "get_secret_value") else str(api_key)
        if not secret:
            raise RuntimeError("四臂评测query rewrite凭据未配置")
        return {
            "base_url": base_url.strip(), "api_key": secret,
            "model": str(model).strip(),
            "timeout": float(timeout),
        }

    def _model_bindings(self, spec: EvaluationSpec) -> dict[str, Any]:
        settings = getattr(self.app_state, "settings", None)
        if settings is None:
            raise RuntimeError("评测需要管理服务的模型配置")
        remote = getattr(settings, "model_provider", "local") == "remote"
        provider_identity = getattr(self.app_state, "model_provider_identity", None)
        if remote:
            if not isinstance(provider_identity, dict):
                raise RuntimeError("远程模型provider身份不可用")
            embedding_assets = {"sha256": provider_identity["embedding"]["asset_sha256"]}
            reranker_assets = {"sha256": provider_identity["reranker"]["asset_sha256"]}
        else:
            embedding_path = _configured_model_path(settings, "embedding_model_path", self.root)
            reranker_path = _configured_model_path(settings, "reranker_model_path", self.root)
            try:
                embedding_assets = _tree_fingerprint(embedding_path)
                reranker_assets = _tree_fingerprint(reranker_path)
            except Exception as error:
                raise RuntimeError("评测模型资产不可用或完整性指纹失败") from error
        from wenjin.retrieval.search_policy import SearchPolicy

        policy = getattr(self.app_state, "search_policy", None)
        if policy is None:
            policy = SearchPolicy.from_settings(settings)
        binding: dict[str, Any] = {
            "embedding_model_id": self._runtime_parts()[3],
            "embedding_model_assets": embedding_assets,
            "reranker_enabled": bool(policy.reranker_enabled),
            "provider_identity": provider_identity if remote else None,
        }
        runtime_environment = self._runtime_environment()
        if policy.reranker_enabled:
            binding.update({
                "reranker_model_id": policy.reranker_model_id,
                "reranker_model_assets": reranker_assets,
                "reranker_controls": {
                    "max_length": policy.reranker_max_length,
                    "batch_size": policy.reranker_batch_size,
                    "device": "remote" if remote else runtime_environment["device"],
                },
            })
        binding["runtime_environment"] = runtime_environment
        if "multi_query" in spec.profiles:
            rewrite = self._rewrite_configuration()
            binding["rewriter_model_id"] = rewrite["model"]
            binding["rewriter_provider_sha256"] = _sha(rewrite["base_url"].encode("utf-8"))
        return binding

    def _bind(self, spec: EvaluationSpec, rows: list[dict[str, Any]], dataset_sha: str,
              dataset_path: str) -> tuple[dict[str, Any], dict[str, Any], Any]:
        from wenjin.releases.resolver import (
            build_pinned_evaluation_router,
            create_evaluation_release_resolver,
        )
        from wenjin.retrieval.strategy_experiment import run_identity_sha256

        _manager, qdrant, embedder, model_id, lock = self._runtime_parts()
        resolver = create_evaluation_release_resolver(
            root=self.root, qdrant_client=qdrant, embedder=embedder, reranker=None,
            search_lock=lock, expected_embedding_model=model_id,
        )
        request_snapshot = resolver.begin_request()
        router, run_identity = build_pinned_evaluation_router(
            resolver=resolver, request_snapshot=request_snapshot, rows=rows,
            qdrant_client=qdrant, embedder=embedder, reranker=None, search_lock=lock,
            expected_embedding_model=model_id, profiles=spec.profiles,
        )
        config = _config(spec.experiment_id)
        try:
            code_entries = self._code_fingerprint()
        except Exception as error:
            raise RuntimeError("评测执行代码或固定route指纹不可读取") from error
        model_bindings = self._model_bindings(spec)
        run_identity.update({
            "experiment_id": spec.experiment_id,
            "dataset_id": _DATASET,
            "dataset_sha256": dataset_sha,
            "config": config,
            "config_sha256": _sha(_canonical(config)),
            "profile_sha256": _sha(_canonical({"profiles": list(spec.profiles), "config": config})),
            "code_assets": code_entries,
            "code_sha256": _sha(_canonical(code_entries)),
            "runtime_environment": model_bindings["runtime_environment"],
            "model_bindings": model_bindings,
            "embedding_model_id": model_bindings["embedding_model_id"],
            "embedding_model_sha256": model_bindings["embedding_model_assets"]["sha256"],
            "reranker_model_id": model_bindings["reranker_model_id"],
            "reranker_model_sha256": model_bindings["reranker_model_assets"]["sha256"],
            **({"rewriter_model_id": model_bindings["rewriter_model_id"]}
               if "rewriter_model_id" in model_bindings else {}),
        })
        run_identity["run_identity_sha256"] = run_identity_sha256(run_identity)
        resource_ids = sorted({str(row["library"]).lower().replace("_", "-") for row in rows})
        input_snapshot = {
            "dataset_id": _DATASET,
            "dataset_path": dataset_path,
            "dataset_sha256": dataset_sha,
            "dataset_case_count": len(rows),
            "case_ids": [row["case_id"] for row in rows],
            "resource_ids": resource_ids,
            "profiles": list(spec.profiles),
            "config": config,
            "route_revision": request_snapshot.revision,
            "run_identity_sha256": run_identity["run_identity_sha256"],
        }
        return input_snapshot, run_identity, (resolver, request_snapshot, router, resource_ids)

    def binding_factory(self, spec: EvaluationSpec, _job_id: str, actor: Any):
        rows, dataset_sha, dataset_path = _load_rows(self.root, spec.experiment_id)
        identity_store = self.app_state.identity_store
        resource_ids = sorted({str(row["library"]).lower().replace("_", "-") for row in rows})
        for resource_id in resource_ids:
            if not identity_store.can_read(actor, "library", resource_id):
                raise Forbidden(f"管理员缺少资料库读取授权：{resource_id}")
        snapshot, run_identity, _bound = self._bind(spec, rows, dataset_sha, dataset_path)
        return snapshot, run_identity

    def context_factory(self, job: EvaluationJob, _job_dir: Path) -> EvaluationContext:
        from wenjin.evaluation_tasks.service import EvaluationIntegrityError
        from wenjin.retrieval.router import RouteConfig
        spec = self.specs()[job.experiment_id]
        rows, dataset_sha, dataset_path = _load_rows(self.root, spec.experiment_id)
        snapshot, run_identity, (_resolver, _request_snapshot, router, _resources) = self._bind(
            spec, rows, dataset_sha, dataset_path
        )
        if job.input_snapshot != snapshot or job.run_identity != run_identity:
            raise EvaluationIntegrityError("固定题集、资料发布或检索资产已变化；拒绝漂移运行/恢复")
        config = RouteConfig(**snapshot["config"])
        settings = getattr(self.app_state, "settings", None)
        owned_closers: list[Any] = []
        rewriter = None
        try:
            if "reranker_model_id" in run_identity.get("model_bindings", {}):
                if settings is None:
                    raise RuntimeError("评测reranker配置不可用")
                shared = getattr(self.app_state, "shared_reranker", None)
                binding = run_identity["model_bindings"]
                controls = binding.get("reranker_controls", {})
                remote = getattr(settings, "model_provider", "local") == "remote"
                if remote:
                    provider_identity = getattr(self.app_state, "model_provider_identity", None)
                    if (shared is None or provider_identity != binding.get("provider_identity")
                            or shared.model_id != binding["reranker_model_id"]
                            or shared.max_length != controls.get("max_length")
                            or shared.batch_size != controls.get("batch_size")
                            or shared.device != "remote"):
                        raise EvaluationIntegrityError(
                            "运行远程reranker与固定服务身份/控制参数不一致"
                        )
                    current_assets = {
                        "sha256": provider_identity["reranker"]["asset_sha256"]
                    }
                else:
                    expected_path = _configured_model_path(
                        settings, "reranker_model_path", self.root
                    )
                    if (
                        shared is None
                        or Path(shared.model_path).resolve() != expected_path
                        or shared.model_id != binding["reranker_model_id"]
                        or shared.max_length != controls.get("max_length")
                        or shared.batch_size != controls.get("batch_size")
                        or shared.device != controls.get("device")
                    ):
                        raise EvaluationIntegrityError(
                            "运行共享reranker与固定模型身份/控制参数不一致"
                        )
                    current_assets = _tree_fingerprint(shared.model_path)
                if current_assets != binding.get("reranker_model_assets"):
                    raise EvaluationIntegrityError("共享reranker模型资产已变化")
                if not remote:
                    try:
                        shared.bind_asset_fingerprint(
                            str(binding["reranker_model_assets"]["sha256"])
                        )
                    except RuntimeError as error:
                        raise EvaluationIntegrityError(str(error)) from error
                # 评测任务借用管理器唯一实例，实例由app lifespan关闭。
                router.reranker = shared
            if "multi_query" in spec.profiles:
                from openai import OpenAI

                from wenjin.retrieval.query_rewrite import OpenAIQueryRewriter

                rewrite = self._rewrite_configuration()
                client = OpenAI(
                    base_url=rewrite["base_url"], api_key=rewrite["api_key"],
                    timeout=rewrite["timeout"],
                )
                rewriter = OpenAIQueryRewriter(client=client, model=rewrite["model"])
                owned_closers.append(client.close)
        except BaseException:
            for close_resource in reversed(owned_closers):
                close_resource()
            raise
        def close_owned() -> None:
            for close_resource in reversed(owned_closers):
                close_resource()

        return EvaluationContext(
            rows=rows, router=router, config=config, rewriter=rewriter,
            input_snapshot=snapshot, run_identity=run_identity,
            close_owned=close_owned if owned_closers else None,
        )

    def create_service(self, identity_store: Any, store: Any):
        from wenjin.evaluation_tasks.service import EvaluationTaskService
        from wenjin.retrieval.strategy_experiment import run_retrieval_experiment

        specs = self.specs()
        self._specs = specs
        return EvaluationTaskService(
            root=self.root, store=store, identity_store=identity_store,
            specs=specs, context_factory=self.context_factory,
            runner=run_retrieval_experiment, binding_factory=self.binding_factory,
        )
