"""Tests for bcp.deps (cross-component dependency and contracts map)."""

from __future__ import annotations

import json
from pathlib import Path

from bcp.bre import BreLayout
from bcp.cli import main
from bcp.config import BcpConfig
from bcp.deps import BRE_ONLY, CODE_ONLY, CONFIRMED, build_deps, render_deps_md
from bcp.impact import build_file_index, build_impact
from bcp.manifest import load_manifest
from bcp.sourceindex import SourceIndex


def _deps(bre: Path, repo: Path) -> dict:  # type: ignore[type-arg]
    cfg = BcpConfig()
    man = load_manifest(bre)
    index = SourceIndex(repo, cfg.source_extensions, cfg.exclude_dirs)
    return build_deps(man, cfg, index, build_file_index(man), build_impact(man, BreLayout(bre)))


def test_code_edges_classified_against_bre(bre_mini: Path, repo_mini: Path) -> None:
    data = _deps(bre_mini, repo_mini)
    edges = {(e["from"], e["to"]): e for e in data["edges"]}
    assert edges[("alpha", "beta")]["status"] == CONFIRMED  # AlphaService names BetaService (same package)
    assert "src/main/java/x/AlphaService.java → src/main/java/x/BetaService.java" in edges[("alpha", "beta")]["samples"]
    assert edges[("beta", "gamma")]["status"] == BRE_ONLY  # claimed by the manifest, no code reference
    assert edges[("beta", "alpha")]["status"] == CODE_ONLY  # BetaService uses Alpha and Constants
    assert edges[("beta", "alpha")]["code_references"] == 2
    assert data["edge_counts"] == {CONFIRMED: 1, BRE_ONLY: 1, CODE_ONLY: 1}
    md = render_deps_md(data)
    assert "| `beta` | `gamma` | bre-only (verify) | 0 | - |" in md
    assert "Verify every `bre-only` edge" in md


def test_imports_resolve_across_packages(bre_copy: Path, repo_copy: Path) -> None:
    helper = repo_copy / "src/main/java/x/BetaService.java"
    helper.write_text(helper.read_text(encoding="utf-8").replace("package x;\n", "package x;\n\nimport y.GammaHelper;\n"),
                      encoding="utf-8")
    edges = {(e["from"], e["to"]): e for e in _deps(bre_copy, repo_copy)["edges"]}
    assert edges[("beta", "gamma")]["status"] == CONFIRMED


def test_interfaces_and_integration_points(bre_mini: Path, repo_copy: Path) -> None:
    (repo_copy / "src/main/java/x/Alpha.java").write_text(
        "package x;\n\n/** Alpha client. */\npublic interface AlphaClient {\n    void call();\n}\n", encoding="utf-8")
    svc = repo_copy / "src/main/java/x/AlphaService.java"
    svc.write_text(svc.read_text(encoding="utf-8").replace(
        "        return alpha;\n", "        // uses JmsTemplate later\n        new RestTemplate();\n        return alpha;\n", 1),
        encoding="utf-8")
    data = _deps(bre_mini, repo_copy)
    assert data["interfaces"]["alpha"] == [{"name": "AlphaClient", "path": "src/main/java/x/Alpha.java"}]
    points = data["integration_points"]["alpha"]
    assert points["http"] == {"count": 1, "hits": ["src/main/java/x/AlphaService.java:L14"]}
    assert "messaging" not in points  # the mention is in a comment
    md = render_deps_md(data)
    assert "`AlphaClient` (Alpha.java)" in md and "| `alpha` | http | 1 | AlphaService.java:L14 |" in md
    assert "| `alpha` | alpha, beta |" in md  # data flow through the shared table


def test_focus_scopes_the_markdown(bre_mini: Path, repo_mini: Path) -> None:
    data = _deps(bre_mini, repo_mini)
    md = render_deps_md(data, ["gamma"])
    assert "| `beta` | `gamma` |" in md and "| `alpha` | `beta` |" not in md
    assert "Scoped to the agent domains (gamma): 1 edges touch them" in md
    assert "| `alpha` | `beta` | confirmed-by-code | 1 | AlphaService.java → BetaService.java |" in render_deps_md(data)


def test_cli_and_generate_write_the_map(workspace: Path, tmp_path: Path) -> None:
    from bcp.generate import generate

    bre = workspace / "business-rules-extraction"
    out = tmp_path / "deps"
    assert main(["deps", "--bre", str(bre), "--repo", str(workspace), "--out", str(out)]) == 0
    data = json.loads((out / "dependency-map.json").read_text(encoding="utf-8"))
    assert data["schema"] == "bcp.deps/1" and data["edge_counts"][CONFIRMED] == 1
    generate(bre, workspace, None, BcpConfig())
    text = (workspace / ".kiro/context/dependency-map.md").read_text(encoding="utf-8")
    assert text.split("\n")[1].startswith("> Trust: Tier 1: mechanical; code-derived edges")
    impact = json.loads((workspace / ".kiro/agents/impact-analysis.json").read_text(encoding="utf-8"))
    assert "file://.kiro/context/dependency-map.md" in impact["resources"]
    assert "`.kiro/context/dependency-map.md` (Tier 1)" in impact["prompt"]
