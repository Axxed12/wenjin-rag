"""Review reproductions; all fixture assets live in TemporaryDirectory."""
import asyncio
import hashlib
import gc
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))
from test_index_audit import build_assets, write_bm25, FakeQdrant
from test_mcp_multicorpus import FakeService, make_index, build_router, policies
from fastmcp import Client
from wenjin.audit import SearchAudit
from wenjin.index_audit import admit_index
from wenjin.ingestion.chunking import block_chunks, fixed_chunks
from wenjin.mcp_server import build_mcp_server, RuntimeIdentity


async def main():
    results = {}
    with tempfile.TemporaryDirectory(prefix="wenjin-review-") as tmp:
        root = Path(tmp)
        assets = build_assets(root / "audit")
        fake = FakeQdrant(assets["payload"])
        baseline = admit_index(assets["target"], root=root / "audit", qdrant_client=fake)
        gc.collect()  # validate_target uses sqlite's transaction context, not closing().
        payload = dict(assets["payload"])
        payload["text"] = "THIS IS NOT THE SOURCE.\nFABRICATED EVIDENCE.\n"
        payload["content_sha256"] = hashlib.sha256(payload["text"].encode()).hexdigest()
        write_bm25(assets["bm25_path"], payload)
        forged = admit_index(assets["target"], root=root / "audit", qdrant_client=FakeQdrant(payload))
        results["forged_chunk_admission"] = {"baseline": baseline["status"], "forged": forged["status"], "source_unchanged": True}

        doc = {"version": "1", "source_path": "doc.txt", "text": "A" * 1700 + "\n"}
        chunks = block_chunks(doc, split_oversized=True)
        results["long_line_contract"] = [{"start": c.start_line, "end": c.end_line, "length": len(c.text), "equals_declared_lines": c.text == doc["text"]} for c in chunks]
        doc["text"] = "first\nlast"
        results["fixed_no_final_newline"] = [{"text": c.text, "start": c.start_line, "end": c.end_line} for c in fixed_chunks(doc)]

        acl = root / "leak/configs/access"
        acl.mkdir(parents=True)
        (acl / "secret-project.json").write_text(json.dumps({"schema_version": 1, "corpus_id": "secret-project-INTERNAL", "visibility": "internal", "allowed_clients": []}), encoding="utf-8")
        audit = SearchAudit(root / "leak-audit.jsonl")
        server = build_mcp_server(FakeService(), project_root=root / "leak", audit=audit, identity=RuntimeIdentity("osg-3.6.5", "test", "osg"))
        async with Client(server) as client:
            response = await client.call_tool("list_corpora", {})
            results["policy_error_leak"] = response.data
        audit.close()

        router = build_router([make_index("alpha", "1.0", "alpha-1.0")])
        lock = threading.Lock()
        active = peak = 0
        def embed(query):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.3)
            with lock:
                active -= 1
            return [0.0] * 8
        router.embedder.get_query_embedding = embed
        audit = SearchAudit(root / "concurrent-audit.jsonl")
        server = build_mcp_server(FakeService(), project_root=root, access=policies("alpha-1.0"), corpus_router=router, audit=audit, identity=RuntimeIdentity("osg-3.6.5", "test", "osg"))
        async with Client(server) as client:
            responses = await asyncio.gather(*[client.call_tool("search_docs", {"query": "alpha", "library": "alpha"}) for _ in range(4)])
        results["routed_model_concurrency"] = {"requests": len(responses), "peak": peak, "all_success": all(not r.is_error for r in responses)}
        audit.close()
        gc.collect()
    out = Path(__file__).with_name("results.json")
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False, indent=2))


asyncio.run(main())
