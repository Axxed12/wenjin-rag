from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from scripts.search_routed import ROOT as CLI_ROOT
from scripts.search_routed import build_parser
from wenjin.mcp_server import create_router
from wenjin.retrieval.router import DEFAULT_MULTI_CORPUS_ROUTE, MultiCorpusRouter

ROOT = Path(__file__).resolve().parents[1]
OLD_ROUTE = ROOT / "configs/indexes/multicorpus-candidates-002.json"
NEW_ROUTE = ROOT / DEFAULT_MULTI_CORPUS_ROUTE
THREE_D_TARGET = ROOT / "configs/indexes/ogc-3dtiles-1.1-blocks-split-candidate-003.json"


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def test_default_route_changes_only_the_3dtiles_candidate() -> None:
    old = _read(OLD_ROUTE)
    new = _read(NEW_ROUTE)
    target = _read(THREE_D_TARGET)

    old_by_library = {item["library"]: item for item in old["candidates"]}
    new_by_library = {item["library"]: item for item in new["candidates"]}
    assert set(old_by_library) == set(new_by_library)
    assert new["route_id"] == "multicorpus-candidates-003"
    assert new["active_unchanged"] == old["active_unchanged"]
    assert new_by_library["ogc-3dtiles"] == target
    for library in set(old_by_library) - {"ogc-3dtiles"}:
        assert new_by_library[library] == old_by_library[library]


def test_mcp_and_cli_share_the_new_default_route(monkeypatch, tmp_path) -> None:
    cli_default = build_parser().get_default("route")
    assert cli_default == CLI_ROOT / DEFAULT_MULTI_CORPUS_ROUTE

    expected_path = tmp_path / DEFAULT_MULTI_CORPUS_ROUTE
    expected_path.parent.mkdir(parents=True)
    expected_path.write_text("{}", encoding="utf-8")
    captured: dict[str, object] = {}
    marker = object()

    def fake_from_route_file(cls, route_path, **kwargs):
        captured["route_path"] = route_path
        captured.update(kwargs)
        return marker

    monkeypatch.setattr(MultiCorpusRouter, "from_route_file", classmethod(fake_from_route_file))
    result = create_router(
        root=tmp_path,
        qdrant_client=SimpleNamespace(),
        embedder=SimpleNamespace(),
        reranker=None,
        search_lock=None,
    )

    assert result is marker
    assert captured["route_path"] == expected_path
    assert captured["root"] == tmp_path
