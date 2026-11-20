"""Tests for bcp.precheck (mechanical checks, no AI)."""

from __future__ import annotations

import json
from pathlib import Path

from bcp import cli
from bcp.config import BcpConfig, from_dict
from bcp.precheck import absolutes, risk_score, run_precheck, spot_check_count, tokens_of, topics

CFG = BcpConfig()


def test_guard_contradicted_by_multi_line_interface_annotation(tmp_path: Path, bre_mini: Path,
                                                                repo_mini: Path) -> None:
    data = run_precheck(bre_mini, repo_mini, tmp_path / "rev", ["beta"], CFG)
    entry = data["claims"]["beta/BR-6"]
    guard = entry["guard_check"]
    assert guard["status"] == "contradicted"
    hit = guard["hits"][0]
    assert (hit["path"], hit["start"], hit["end"], hit["annotation"]) == (
        "src/main/java/x/GuardedApi.java", 13, 14, "PreAuthorize")
    assert "src/main/java/x/GuardedApiImpl.java#saveGuarded" in guard["checked"]
    assert {"token": "GuardedApiImpl.saveGuarded", "path": "src/main/java/x/GuardedApiImpl.java",  # nosec B105 - citation token, not a credential
            "start": 8, "end": 14} in entry["citations"]
    assert entry["risk_score"] >= 3
    assert data["summary"]["guard_contradictions"] == 1
    # claims that do not say "unguarded" get no guard check
    assert "guard_check" not in data["claims"]["beta/BR-1"]


def test_guard_consistent_when_no_annotation_anywhere(tmp_path: Path, bre_copy: Path, repo_mini: Path) -> None:
    path = bre_copy / "detailed-domain-bre/domain-beta/business-rules.md"
    path.write_text(path.read_text(encoding="utf-8") + "\n## BR-7: Opening is unguarded\n"
                    "- Source: `GuardedApiImpl.openGuarded`.\n- No privilege check guards the open call.\n",
                    encoding="utf-8")
    data = run_precheck(bre_copy, repo_mini, tmp_path / "rev", ["beta"], CFG)
    assert data["claims"]["beta/BR-7"]["guard_check"]["status"] == "consistent"


def test_guard_annotations_config(tmp_path: Path, bre_mini: Path, repo_mini: Path) -> None:
    cfg = from_dict({"guard_annotations": ["@RolesAllowed"]})
    assert cfg.guard_annotations == ["RolesAllowed"]
    data = run_precheck(bre_mini, repo_mini, tmp_path / "rev", ["beta"], cfg)
    assert data["claims"]["beta/BR-6"]["guard_check"]["status"] == "consistent"


def test_guard_not_checked_when_nothing_resolves(tmp_path: Path, bre_copy: Path, repo_mini: Path) -> None:
    path = bre_copy / "detailed-domain-bre/domain-beta/business-rules.md"
    path.write_text(path.read_text(encoding="utf-8") + "\n## BR-7: Missing is unguarded\n"
                    "- Source: `NoSuchApi.run`.\n", encoding="utf-8")
    data = run_precheck(bre_copy, repo_mini, tmp_path / "rev", ["beta"], CFG)
    assert data["claims"]["beta/BR-7"]["guard_check"]["status"] == "not-checked"


EXTRA_RULES = """
## BR-7: Saving is imperative
```
saveGuarded(guarded):
    check(guarded)
```
- Source: `GuardedApiImpl.saveGuarded`.
- `GuardedApi.saveGuarded` has **no** `@PreAuthorize`.

## BR-8: Uneven coverage
| Method | Guard |
|---|---|
| `openGuarded` | `VIEW_ALL` |
| **`saveGuarded`**, `getGuarded` | **none** |
- Source: annotations in `src/main/java/x/GuardedApi.java`; `ADMIN_ONLY` is unused.

## BR-9: The resolvers are unguarded
```
openGuarded(id)       -- NO @PreAuthorize
```
- Source: `GuardedApi` methods.
"""


def test_guard_check_handles_fences_emphasis_tables_and_code_lines(tmp_path: Path, bre_copy: Path,
                                                                   repo_mini: Path) -> None:
    """Real-data fixes: a leading code fence must not shift backtick tokens; **no** emphasis still reads as a
    "no guard" claim; a table row with a "none" guard cell names its methods; pseudo-code lines and bare
    names bind to the cited class or file (never to a CONSTANT)."""
    path = bre_copy / "detailed-domain-bre/domain-beta/business-rules.md"
    path.write_text(path.read_text(encoding="utf-8") + EXTRA_RULES, encoding="utf-8")
    claims = run_precheck(bre_copy, repo_mini, tmp_path / "rev", ["beta"], CFG)["claims"]
    br7 = claims["beta/BR-7"]["guard_check"]
    assert br7["status"] == "contradicted" and br7["hits"][0]["method"] == "saveGuarded"
    br8 = claims["beta/BR-8"]["guard_check"]
    assert br8["status"] == "contradicted"
    assert {h["method"] for h in br8["hits"]} == {"saveGuarded", "getGuarded"}  # openGuarded's row is not "none"
    assert "src/main/java/x/GuardedApi.java#openGuarded" not in br8["checked"]
    br9 = claims["beta/BR-9"]["guard_check"]
    assert br9["status"] == "consistent" and br9["checked"][0].endswith("GuardedApi.java#openGuarded")


def test_absolute_flags() -> None:
    text = "## BR-1: T\nOnly admins may save. Every save is audited.\n```\nIF none THEN x\n```\nno other path."
    hits = absolutes(text, 10)
    words = [(h["word"], h["line"], h["in_code_block"]) for h in hits]
    assert ("only", 11, False) in words and ("every", 11, False) in words
    assert ("none", 13, True) in words
    assert ("no other", 15, False) in words
    assert not [h for h in hits if h["line"] == 10]  # the heading is not scanned


def test_topics_and_risk() -> None:
    assert topics("The save rolls back the transaction and requires a privilege") == ["authorization", "transaction"]
    assert "count" in topics("It runs exactly 3 checks.") and "concurrency" in topics("A lock is held")
    entry = {"absolutes": [{"in_code_block": False}] * 6, "topics": ["transaction"],
             "guard_check": {"status": "contradicted"}, "cluster": [{}]}
    assert risk_score(entry) == 4 + 1 + 3 + 1
    assert risk_score({"absolutes": [{"in_code_block": True}], "topics": [], "cluster": []}) == 0.5


def test_cluster_finds_repeat_outside_own_file(tmp_path: Path, bre_mini: Path, repo_mini: Path) -> None:
    data = run_precheck(bre_mini, repo_mini, tmp_path / "rev", ["beta"], CFG)
    cluster = data["claims"]["beta/BR-6"]["cluster"]
    paths = {h["path"] for h in cluster}
    assert "detailed-domain-bre/domain-beta/workflows.md" in paths
    assert "detailed-domain-bre/domain-beta/business-rules.md" not in paths
    hit = next(h for h in cluster if h["path"].endswith("workflows.md"))
    assert hit["line"] == 4 and hit["jaccard"] == 1.0
    assert data["claims"]["beta/BR-1"]["cluster"] == []


def test_tokens_keep_identifiers_whole() -> None:
    toks = tokens_of("The `GuardedApi.saveGuarded` method has no guard")
    assert "guardedapi.saveguarded" in toks and "the" not in toks and "no" not in toks


def test_spot_check_count() -> None:
    assert [spot_check_count(n) for n in (0, 1, 3, 4, 5, 9)] == [0, 1, 1, 1, 2, 3]


def test_cli_writes_both_files(tmp_path: Path, bre_mini: Path, repo_mini: Path,
                               capsys) -> None:  # type: ignore[no-untyped-def]
    out = tmp_path / "rev"
    assert cli.main(["precheck", "--bre", str(bre_mini), "--repo", str(repo_mini), "--out", str(out),
                     "--domains", "alpha,beta"]) == 0
    line = capsys.readouterr().out
    assert line.startswith("precheck: 14 claims, ") and "1 guard contradictions" in line
    data = json.loads((out / "precheck/precheck.json").read_text(encoding="utf-8"))
    assert data["schema"] == "bcp.precheck/1" and data["generated_at"].endswith("Z") and "repo_head" in data
    md = (out / "precheck/precheck.md").read_text(encoding="utf-8")
    assert md.index("## Guard contradictions") < md.index("## Domain `alpha`")
    assert "beta/BR-6" in md.split("## Domain")[0]
    assert cli.main(["precheck", "--bre", str(bre_mini), "--repo", str(repo_mini), "--out", str(out),
                     "--domains", "nope"]) == 2
