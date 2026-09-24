"""复审（第一、二批）提出的阻断与非阻断项的回归测试。

每条测试对应复审里的一个编号，命名里保留编号以便对照：
- P1-1 身份一致性必须覆盖下载与列表入口
- P1-2 缺少内容Hash不能降级为无校验取件
- P1-3 服务工厂契约不能破坏既有CLI
- P2-4 被丢弃证据的私有元数据不能返回客户端
- P2-5 取件不能阻塞事件循环
"""

import asyncio
import contextlib
import hashlib
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.responses import PlainTextResponse

from wenjin.catalog import corpus_catalog_report, find_corpus
from wenjin.corpus_identity import CorpusIdentityError, resolve_identity
from wenjin.serving import (
    CorpusSources,
    SourceUnverified,
    content_hash_of,
)

CONTENT = b"PRIVATE_CONTENT\n"
HASH = hashlib.sha256(CONTENT).hexdigest()


def write_corpus(root: Path, *, directory: str, declared: str | None, subdir: str,
                 filename: str = "secret.txt", content: bytes = CONTENT,
                 raw_sha256: str | None = HASH, visibility: str = "corpora-private",
                 extra: dict | None = None) -> Path:
    """在指定目录名下写一份语料；declared是manifest里声明的corpus_id。"""
    raw_dir = root / "raw_data" / subdir
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / filename).write_bytes(content)
    manifest_dir = root / visibility / directory
    manifest_dir.mkdir(parents=True, exist_ok=True)
    entry: dict = {"path": filename, "bytes": len(content),
                   "retrieval_eligible": True, "indexable": True}
    if raw_sha256 is not None:
        entry["raw_sha256"] = raw_sha256
    entry.update(extra or {})
    manifest: dict = {"schema_version": 3, "library": "x", "version": "1",
                      "raw_data_subdir": subdir, "license": "MIT", "files": [entry]}
    if declared is not None:
        manifest["corpus_id"] = declared
    path = manifest_dir / "source-manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


# --- P1-1 身份一致性：目录名必须被核对 ---


def test_p1_1_catalog_rejects_directory_that_lies_about_its_id(tmp_path):
    """目录`private-1`声明成`public-1`时，语料列表不能顶着公开身份列出它。

    不复现的话：给它public-1的公开策略，取件就会放行私有正文。
    这里断言的是**隔离**而不是"整份列表一起失败"：坏目录被挡在外面，同时不牵连别人
    （见 tests/test_catalog_fault_isolation.py）。旧行为下任意一个坏目录会让所有语料的
    取件返回503，故障半径过大。
    """
    write_corpus(tmp_path, directory="private-1", declared="public-1", subdir="x/1")
    report = corpus_catalog_report(tmp_path)
    assert report.entries == (), "身份冲突的语料不能出现在正常条目里"
    assert [problem.reason for problem in report.problems] == ["identity_conflict"]
    assert report.problems[0].directory == "private-1"
    # 它自称的 `public-1` 不能被当正常语料服务。查 `private-1` 必须明确失败；
    # 查 `public-1` 不返回条目（它按目录名判定，自称值不构成身份）。
    with pytest.raises(CorpusIdentityError, match="身份"):
        find_corpus(tmp_path, "private-1")
    assert find_corpus(tmp_path, "public-1") is None


def test_p1_1_fetch_rejects_directory_that_lies_about_its_id(tmp_path):
    """取件入口用同一套判定：只比较"请求身份 vs manifest声明"是不够的。"""
    path = write_corpus(tmp_path, directory="private-1", declared="public-1", subdir="x/1")
    with pytest.raises(CorpusIdentityError, match="身份冲突"):
        CorpusSources.from_manifest(
            path, raw_root=tmp_path / "raw_data", expected_corpus_id="public-1"
        )


def test_p1_1_rejects_duplicate_corpus_id_across_directories(tmp_path):
    """同一个corpus_id出现在两个目录时拒绝，不能取第一个匹配项。

    两份**都**被隔离，并且查这个 id 必须失败——留任何一份在列表里都等于让
    "哪份被服务"取决于遍历顺序。
    """
    write_corpus(tmp_path, directory="same-id", declared="same-id", subdir="x/1",
                 visibility="corpora")
    write_corpus(tmp_path, directory="same-id", declared="same-id", subdir="y/1",
                 visibility="corpora-private")
    report = corpus_catalog_report(tmp_path)
    assert report.entries == ()
    assert sorted(problem.location for problem in report.problems) == [
        "corpora-private/same-id/source-manifest.json",
        "corpora/same-id/source-manifest.json",
    ]
    assert {problem.reason for problem in report.problems} == {"duplicate_corpus_id"}
    with pytest.raises(CorpusIdentityError, match="corpus_id重复|由多个目录声明"):
        find_corpus(tmp_path, "same-id")


def test_p1_1_identity_resolution_reports_source(tmp_path):
    """三方一致时按权威度报告来源：显式字段 > manifest声明 > 目录名。"""
    write_corpus(tmp_path, directory="demo-1.0", declared="demo-1.0", subdir="x/1")
    assert resolve_identity(directory="demo-1.0", declared="demo-1.0").source == "manifest"
    assert resolve_identity(
        directory="demo-1.0", declared="demo-1.0", explicit="demo-1.0"
    ).source == "explicit"
    assert resolve_identity(directory="demo-1.0").source == "directory"


def test_p1_1_identifier_rejects_path_like_values():
    """library/version/corpus_id不能是绝对路径、带分隔符或`..`。

    **每个坏值都必须再以 `directory=` 的形式测一遍。** 只传 `declared=` 的话，坏值会与
    合法的目录名构成"身份冲突"，被那条检查先挡下——**字符集校验本身从没被执行到**，
    这个测试也就没有判别力。

    实测证据：把标识符正则放宽成 `^.{1,128}$` 之后，只传 declared 的写法仍然全绿；
    加上 directory 那一半才会失败。故障不在代码，在这个测试自己的夹具。
    """
    for bad in ("../evil", "a/b", "/abs", "C:/x", "..", ".", "-leading", "a b"):
        with pytest.raises(CorpusIdentityError):
            resolve_identity(directory="ok-1.0", declared=bad)
        # 目录名位置：没有第二个候选可以冲突，只有字符集校验能拦下它。
        with pytest.raises(CorpusIdentityError, match="语料目录名"):
            resolve_identity(directory=bad)


def test_p1_1_blank_declaration_falls_back_to_directory():
    """空串和纯空白按"未声明"处理：这是兼容旧配置的同一条回退路径。

    与内容Hash相反——那里空白必须拒绝，因为完整性没有回退项；而身份有目录名可依据。
    """
    for blank in ("", "   "):
        identity = resolve_identity(directory="ok-1.0", declared=blank)
        assert identity.corpus_id == "ok-1.0"
        assert identity.source == "directory"


# --- P1-2 缺少内容Hash必须拒绝 ---


def test_p1_2_manifest_without_hash_is_rejected(tmp_path):
    """没有Hash就无法证明取回的是被引用的那份原文，整份manifest拒绝加载。"""
    path = write_corpus(tmp_path, directory="demo-1.0", declared="demo-1.0",
                        subdir="demo/1", raw_sha256=None)
    with pytest.raises(SourceUnverified, match="缺少内容Hash"):
        CorpusSources.from_manifest(
            path, raw_root=tmp_path / "raw_data", expected_corpus_id="demo-1.0"
        )


def test_p1_2_empty_and_malformed_hash_are_rejected(tmp_path):
    """空字符串和格式非法的Hash同样拒绝，不能当成"没有就不校验"。"""
    for bad in ("", "   ", "not-a-hash", "ABC", "0" * 63, "g" * 64):
        with pytest.raises(SourceUnverified):
            content_hash_of({"path": "f.txt", "raw_sha256": bad})


def test_p1_2_legacy_sha256_field_is_accepted(tmp_path):
    """兼容规则明确：先看raw_sha256，再看旧字段sha256。"""
    assert content_hash_of({"path": "f", "sha256": HASH}) == HASH
    assert content_hash_of({"path": "f", "raw_sha256": HASH, "sha256": "0" * 64}) == HASH
    with pytest.raises(SourceUnverified):
        content_hash_of({"path": "f"})


def test_p1_2_read_verified_refuses_when_hash_missing(tmp_path):
    """第二道防线：即使记录绕过了from_manifest，也不能降级成无校验下载。"""
    from wenjin.serving import SourceRecord

    sources = CorpusSources(
        corpus_id="demo-1.0", raw_data_subdir="demo/1",
        raw_root=tmp_path / "raw_data",
        records={"f.txt": SourceRecord("f.txt", None, 9, True, True)},
    )
    (tmp_path / "raw_data" / "demo" / "1").mkdir(parents=True)
    (tmp_path / "raw_data" / "demo" / "1" / "f.txt").write_bytes(CONTENT)
    with pytest.raises(SourceUnverified):
        sources.read_verified("f.txt")


def test_p1_2_tampered_content_still_rejected(tmp_path):
    """有Hash时被改动的文件依然拒绝——这条是原有保证，回归确认没被削弱。"""
    from wenjin.serving import SourceModified

    path = write_corpus(tmp_path, directory="demo-1.0", declared="demo-1.0", subdir="demo/1")
    sources = CorpusSources.from_manifest(
        path, raw_root=tmp_path / "raw_data", expected_corpus_id="demo-1.0"
    )
    (tmp_path / "raw_data" / "demo" / "1" / "secret.txt").write_bytes(b"REPLACED\n")
    with pytest.raises(SourceModified):
        sources.read_verified("secret.txt")


def test_real_corpora_still_load_with_hash_enforced():
    """强制Hash必填不能误伤真实语料。"""
    root = Path(__file__).resolve().parents[1]
    for corpus_id in ("osg-3.6.5", "proj-9.8.1", "ogc-3dtiles-1.1", "ogc-wms-1.3.0"):
        sources = CorpusSources.from_manifest(
            root / "corpora" / corpus_id / "source-manifest.json",
            raw_root=root / "raw_data",
            expected_corpus_id=corpus_id,
        )
        assert sources.records, corpus_id
        assert all(record.has_content_hash for record in sources.records.values())


# --- P1-3 服务工厂契约 ---


CLI_CALLERS = [
    "scripts/search_docs.py",
    "scripts/assess_evidence.py",
    "scripts/demo_index_switch.py",
    "scripts/demo_content_update.py",
    "scripts/demo_modify_delete.py",
    "evaluation/evaluate_evidence_judge.py",
    "evaluation/evaluate_evidence_threshold.py",
]


def test_p1_3_factory_returns_service_not_tuple(monkeypatch):
    """create_default_service()必须返回service本身：既有调用方不拆元组。"""
    from wenjin import mcp_server as module

    fake = SimpleNamespace(search=lambda *a, **k: {}, close=lambda: None)
    identity = module.RuntimeIdentity("demo-1.0", "test", "demo-1.0-x")
    monkeypatch.setattr(module, "create_runtime", lambda **kwargs: (fake, identity))
    service = module.create_default_service()
    assert service is fake
    assert hasattr(service, "search") and hasattr(service, "close")


def test_p1_3_create_runtime_returns_service_and_identity(monkeypatch):
    """需要身份的一方用create_runtime()，两者出自同一次读取。"""
    from wenjin import mcp_server as module

    fake = SimpleNamespace(search=lambda *a, **k: {}, close=lambda: None)
    identity = module.RuntimeIdentity("demo-1.0", "test", "demo-1.0-x")
    monkeypatch.setattr(module, "create_runtime", lambda **kwargs: (fake, identity))
    service, got = module.create_runtime()
    assert service is fake and got is identity


def test_p1_3_no_caller_unpacks_the_factory_result():
    """静态守卫：任何调用方都不能把工厂结果拆成两个变量。

    `service, identity = create_default_service()` 会让查询入口报
    'tuple object has no attribute search'，且finally里的close会掩盖前一个异常。
    """
    root = Path(__file__).resolve().parents[1]
    for relative in CLI_CALLERS:
        text = (root / relative).read_text(encoding="utf-8")
        for line in text.splitlines():
            if "create_default_service(" not in line or "=" not in line:
                continue
            left = line.split("=", 1)[0]
            assert "," not in left, f"{relative}: 不能拆包工厂返回值 -> {line.strip()}"


def test_p1_3_factory_callers_release_the_right_object(monkeypatch, tmp_path):
    """冒烟：按CLI的用法调用（不加载模型），确认search和close都作用在service上。"""
    from wenjin import mcp_server as module

    calls = []

    class FakeService:
        def search(self, query, *, config):
            calls.append(("search", query))
            return {"evidence": []}

        def close(self):
            calls.append(("close", None))

    fake = FakeService()
    identity = module.RuntimeIdentity("demo-1.0", "test", "demo-1.0-x")
    monkeypatch.setattr(module, "create_runtime", lambda **kwargs: (fake, identity))
    service = module.create_default_service(use_reranker=False)
    try:
        service.search("q", config=SimpleNamespace())
    finally:
        service.close()
    assert [name for name, _ in calls] == ["search", "close"]


# --- P2-5 取件不能阻塞事件循环 ---


def test_p2_5_download_runs_off_the_event_loop(tmp_path, monkeypatch):
    """同步读取+Hash必须在线程里跑，否则会卡住同进程的其他连接。"""
    from wenjin import mcp_server as module
    from wenjin.access import AccessPolicy, AccessRegistry
    from wenjin.audit import SearchAudit

    write_corpus(tmp_path, directory="demo-1.0", declared="demo-1.0", subdir="demo/1",
                 visibility="corpora")
    real = module.serve_download

    def slow_serve(**kwargs):
        time.sleep(0.2)  # 模拟一次慢读取
        return real(**kwargs)

    monkeypatch.setattr(module, "serve_download", slow_serve)
    server = module.build_mcp_server(
        SimpleNamespace(search=lambda *a, **k: {}),
        audit=SearchAudit(tmp_path / "audit.jsonl"),
        access=AccessRegistry(None, {"demo-1.0": AccessPolicy("demo-1.0", "public", ("*",))}),
        raw_root=tmp_path / "raw_data",
        project_root=tmp_path,
        identity=module.RuntimeIdentity("demo-1.0", "test", "demo-1.0-x"),
    )
    app = server.http_app(path="/mcp")

    async def scenario() -> float:
        import httpx

        delayed = []

        async def ticker() -> None:
            started = time.perf_counter()
            await asyncio.sleep(0.02)
            delayed.append(time.perf_counter() - started)

        task = asyncio.create_task(ticker())
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            response = await client.get("/files/demo-1.0/secret.txt")
        assert response.status_code == 200
        await task
        return delayed[0]

    elapsed = asyncio.run(scenario())
    # 若在事件循环里同步阻塞，ticker的20ms会被推迟到200ms以上。
    assert elapsed < 0.1, f"事件循环被取件阻塞了 {elapsed:.3f}s"


# --- P2-6 验收脚本的门禁不能被绕过 ---


def test_p2_6_acceptance_gate_catches_every_category(tmp_path):
    """每一类验收项的失败都必须让脚本判FAIL，否则它的PASS不能作为验收依据。

    变异测试：从一个全通过的报告出发，逐个类别注入失败，断言都被收集到。
    """
    from scripts.verify_batch1_2 import collect_failures

    healthy = {
        "identity": [{"resolved": "demo-1.0", "directory": "demo-1.0"}],
        "chunk_path_cases": [{"error": None, "raw_sha256_matches": True}],
        "negative_cases": [{"case": "x", "blocked": True}],
        "archives": {"demo-1.0": {"archived_files": 2, "downloadable": 2, "failures": []}},
        "encoding": [{"case": "x", "ok": True}],
    }
    assert collect_failures(healthy) == []

    mutations = {
        "chunk_path": {"chunk_path_cases": [{"error": "SourceNotFound"}]},
        "chunk_path_hash": {"chunk_path_cases": [{"error": None, "raw_sha256_matches": False}]},
        "identity": {"identity": [{"resolved": "public-1", "directory": "private-1"}]},
        "negative": {"negative_cases": [{"case": "x", "blocked": False}]},
        "archive": {"archives": {"demo-1.0": {"archived_files": 2, "downloadable": 1,
                                              "failures": [{"path": "a.tar.gz"}]}}},
        "encoding": {"encoding": [{"case": "x", "ok": False}]},
    }
    for name, mutation in mutations.items():
        mutated = {**healthy, **mutation}
        failures = collect_failures(mutated)
        assert failures, f"变异 {name} 没有被门禁捕获"
        assert any(item["gate"].startswith(name.split("_")[0]) or
                   item["gate"] == name for item in failures), name


def test_p2_6_acceptance_script_exits_nonzero_on_failure(tmp_path, monkeypatch):
    """端到端：注入一个归档失败后，脚本必须以非零码退出。"""
    import importlib

    from scripts import verify_batch1_2 as module

    importlib.reload(module)
    monkeypatch.setattr(module, "OUTPUT", tmp_path / "out.json")
    monkeypatch.setattr(module, "check_archives", lambda: {
        "demo-1.0": {"archived_files": 3, "downloadable": 1, "largest_mb": 0.0,
                     "failures": [{"path": "a.tar.gz", "error": "SourceNotFound"}]},
    })
    with pytest.raises(SystemExit) as excinfo:
        module.main()
    assert excinfo.value.code != 0


def test_p2_6_acceptance_script_passes_on_healthy_report(tmp_path, monkeypatch):
    """对照组：全通过时退出码为0，证明上一条测的是门禁而不是脚本本身总失败。"""
    import importlib

    from scripts import verify_batch1_2 as module

    importlib.reload(module)
    monkeypatch.setattr(module, "OUTPUT", tmp_path / "out.json")
    monkeypatch.setattr(module, "check_archives", lambda: {
        "demo-1.0": {"archived_files": 2, "downloadable": 2, "largest_mb": 0.0,
                     "failures": []},
    })
    with pytest.raises(SystemExit) as excinfo:
        module.main()
    assert excinfo.value.code == 0


# --- 第二轮复审：P2 并发槽位必须覆盖正文发送 ---


def build_download_project(tmp_path: Path, *, content: bytes = b"X" * 1000,
                           entry_extra: dict | None = None) -> Path:
    """一个语料 + 一条登记记录，供取件端点测试使用。"""
    raw = tmp_path / "raw_data" / "demo" / "1"
    raw.mkdir(parents=True, exist_ok=True)
    (raw / "f.txt").write_bytes(content)
    manifest_dir = tmp_path / "corpora" / "demo-1.0"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    entry = {
        "path": "f.txt",
        "raw_sha256": hashlib.sha256(content).hexdigest(),
        "bytes": len(content),
        "retrieval_eligible": True,
        "indexable": True,
    }
    entry.update(entry_extra or {})
    if entry_extra and entry_extra.get("drop_hash"):
        entry.pop("raw_sha256", None)
    entry.pop("drop_hash", None)
    (manifest_dir / "source-manifest.json").write_text(json.dumps({
        "corpus_id": "demo-1.0", "library": "demo", "version": "1",
        "raw_data_subdir": "demo/1", "license": "MIT", "files": [entry],
    }), encoding="utf-8")
    return manifest_dir / "source-manifest.json"


def download_app(tmp_path: Path):
    """构造只带取件端点的ASGI应用（注入假检索服务，不加载模型）。"""
    from wenjin import mcp_server as module
    from wenjin.access import AccessPolicy, AccessRegistry
    from wenjin.audit import SearchAudit

    server = module.build_mcp_server(
        SimpleNamespace(search=lambda *a, **k: {}),
        audit=SearchAudit(tmp_path / "audit.jsonl"),
        access=AccessRegistry(None, {"demo-1.0": AccessPolicy("demo-1.0", "public", ("*",))}),
        raw_root=tmp_path / "raw_data",
        project_root=tmp_path,
        identity=module.RuntimeIdentity("demo-1.0", "test", "demo-1.0-x"),
    )
    return server.http_app(path="/mcp")


class GatedBodySend:
    """把正文发送挂起，模拟慢客户端；每个请求一个可由测试控制的闸门。"""

    def __init__(self, app) -> None:
        self.app = app
        self.gates: list[asyncio.Event] = []
        self.prepared = 0

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/files"):
            await self.app(scope, receive, send)
            return
        gate = asyncio.Event()
        self.gates.append(gate)

        async def gated(message):
            if message["type"] == "http.response.body" and message.get("body"):
                self.prepared += 1
                await gate.wait()
            await send(message)

        await self.app(scope, receive, gated)


def test_p2_1_slot_is_held_until_body_is_sent(tmp_path):
    """并发槽位必须覆盖正文发送：否则慢客户端占着内存而新请求还能继续读文件。

    槽位只包住"读取+构造响应"时，三个请求会全部读完文件（prepared=3）。
    """
    import httpx

    from wenjin import mcp_server as module

    build_download_project(tmp_path)
    app = GatedBodySend(download_app(tmp_path))

    async def scenario() -> tuple[int, int]:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://t", timeout=None) as client:
            tasks = [asyncio.create_task(client.get("/files/demo-1.0/f.txt"))
                     for _ in range(module.MAX_CONCURRENT_DOWNLOADS + 1)]
            await asyncio.sleep(0.5)
            while_blocked = app.prepared
            app.gates[0].set()  # 放行一个，第三个才应该能开始读
            await asyncio.sleep(0.5)
            after_release = app.prepared
            for gate in app.gates:
                gate.set()
            await asyncio.gather(*tasks)
            return while_blocked, after_release

    while_blocked, after_release = asyncio.run(scenario())
    assert while_blocked == module.MAX_CONCURRENT_DOWNLOADS, (
        f"前{module.MAX_CONCURRENT_DOWNLOADS}个响应未结束时，不应有更多请求读完文件"
    )
    assert after_release == module.MAX_CONCURRENT_DOWNLOADS + 1, "释放一个槽位后，第三个应能继续"


class BlockingReader:
    """可控制的阻塞读取：统计同时在读的线程数，并可随时放行。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._release = threading.Event()
        self.live = 0
        self.peak = 0
        # 累计启动次数：用"还在读的线程数"判断第三个请求是否启动并不可靠，
        # 尚未结束的旧线程会被计入，可能把"没启动"误判成"启动了"。
        self.started = 0

    def serve(self, **_kwargs):
        with self._lock:
            self.live += 1
            self.started += 1
            self.peak = max(self.peak, self.live)
        self._release.wait(10)
        with self._lock:
            self.live -= 1
        return PlainTextResponse("ok")

    def unblock(self) -> None:
        self._release.set()

    def peak_after_settle(self) -> int:
        return self.peak


def wait_until(predicate, *, timeout: float = 5.0) -> bool:
    """轮询等待条件成立，避免用固定sleep造成偶发失败。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_p2_1_slot_is_released_when_client_disconnects(tmp_path, monkeypatch):
    """取消路径的槽位不能被提前释放：占满槽位再取消，第三个在读取结束前不得开始。

    这个测试必须**先占满**所有槽位。只发一个请求再取消的话，即使它泄漏，
    第二个请求仍然能用剩下的槽位跑起来，测试会误判为通过。
    """
    import httpx

    from wenjin import mcp_server as module

    build_download_project(tmp_path)
    reader = BlockingReader()
    monkeypatch.setattr(module, "serve_download", reader.serve)
    app = download_app(tmp_path)
    slots = module.MAX_CONCURRENT_DOWNLOADS

    async def scenario() -> tuple[int, int, int]:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://t", timeout=None) as client:
            tasks = [asyncio.create_task(client.get("/files/demo-1.0/f.txt"))
                     for _ in range(slots)]
            assert await asyncio.to_thread(wait_until, lambda: reader.live == slots),                 "两个槽位应都已进入读取"

            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            # 线程不随协程取消而停止：它们应该还在读，槽位不能因此释放。
            still_reading = reader.live

            third = asyncio.create_task(client.get("/files/demo-1.0/f.txt"))
            # 给第三个请求一点时间：它不该能开始读取。
            await asyncio.to_thread(
                wait_until, lambda: reader.live > slots, timeout=0.5
            )
            after_third = reader.live

            reader.unblock()
            third.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await third
            return still_reading, after_third, reader.peak_after_settle()

    still_reading, after_third, peak = asyncio.run(scenario())
    assert still_reading == slots, "取消时后台线程应仍在读取"
    assert not after_third > slots, "读取未结束前，第三个请求不得开始新的读取"
    assert peak == slots, f"读取并发峰值{peak}超过了槽位上限{slots}"


def test_p2_1_slot_released_after_reads_finish_when_cancelled(tmp_path, monkeypatch):
    """取消之后，槽位最终要释放：读取一结束，被挡住的请求就能继续。"""
    import httpx

    from wenjin import mcp_server as module

    build_download_project(tmp_path)
    reader = BlockingReader()
    monkeypatch.setattr(module, "serve_download", reader.serve)
    app = download_app(tmp_path)
    slots = module.MAX_CONCURRENT_DOWNLOADS

    async def scenario() -> tuple[int, int]:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://t", timeout=None) as client:
            tasks = [asyncio.create_task(client.get("/files/demo-1.0/f.txt"))
                     for _ in range(slots)]
            assert await asyncio.to_thread(wait_until, lambda: reader.live == slots)
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task

            third = asyncio.create_task(client.get("/files/demo-1.0/f.txt"))
            reader.unblock()  # 放行两次读取，线程结束 -> 槽位释放
            # 用累计启动数判断第三个读取是否开始，并等第三个HTTP请求真的返回200。
            await asyncio.to_thread(wait_until, lambda: reader.started >= slots + 1, timeout=5.0)
            response = await asyncio.wait_for(third, timeout=5.0)
            return reader.started, response.status_code

    started_total, status = asyncio.run(scenario())
    assert started_total == slots + 1, "读取结束后槽位没有释放，第三个读取从未开始"
    assert status == 200, "被挡住的请求应在槽位释放后正常完成"


# --- 第二轮复审：P2 缺Hash必须在HTTP层有结构化拒绝与审计 ---


@pytest.mark.parametrize("extra,label", [
    ({"drop_hash": True}, "缺失"),
    ({"raw_sha256": ""}, "空串"),
    ({"raw_sha256": "not-a-hash"}, "非法格式"),
    ({"raw_sha256": "0" * 63}, "长度不足"),
])
def test_p2_2_missing_or_bad_hash_returns_structured_error_with_audit(
    tmp_path, extra, label
):
    """真实HTTP路径：缺Hash不能变成500，也不能不留审计。

    这个异常在**加载manifest时**就抛出（早于read_verified），只在后一处捕获是不够的。
    """
    import httpx

    build_download_project(tmp_path, entry_extra=extra)
    app = download_app(tmp_path)

    async def scenario() -> tuple[int, bytes]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://t",
        ) as client:
            response = await client.get("/files/demo-1.0/f.txt")
            return response.status_code, response.content

    status, body = asyncio.run(scenario())
    assert status == 409, f"{label}：应返回结构化拒绝而不是500"
    assert b"X" * 100 not in body, f"{label}：不得返回正文"

    rows = [json.loads(line) for line in
            (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    downloads = [row for row in rows if row.get("event") == "mcp_download_audit"]
    assert len(downloads) == 1, f"{label}：必须留下一条下载审计"
    assert downloads[0]["outcome"] == "unverified"
    # 具体缺哪条记录只进审计，不返回客户端。
    assert downloads[0]["detail"]
    assert b"f.txt" not in body
