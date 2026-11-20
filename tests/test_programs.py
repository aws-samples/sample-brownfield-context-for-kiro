"""Tests for bcp.programs (``bcp variants``: per-program configuration map)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bcp.bre import BreLayout
from bcp.cli import main
from bcp.config import BcpConfig, from_dict
from bcp.errors import BcpInputError
from bcp.impact import build_file_index
from bcp.manifest import load_manifest
from bcp.programs import (
    ABSENT,
    MASKED,
    build_program_config,
    load_snapshot,
    load_snapshots,
    render_program_config_md,
)
from bcp.sourceindex import SourceIndex
from bcp.variants import build_variants

PROGRAMS = Path(__file__).parent / "fixtures" / "programs"
DEFAULTS_JAVA = """package x;

public class Defaults {

    void seed(java.util.List<Object> props) {
        props.add(new GlobalProperty(Constants.GP_ALPHA_MODE, "basic", "the alpha mode"));
        props.add(new GlobalProperty(Constants.GP_BETA_LIMIT, "10", "the beta limit"));
    }

    void react() {
        String mode = Config.get(Constants.GP_ALPHA_MODE);
        if ("strict".equals(mode)) {
            Config.lock();
        }
    }
}
"""
CONSTANTS_JAVA = """package x;

public final class Constants {
    public static final String GP_ALPHA_MODE = "alpha.mode";
    public static final String GP_BETA_LIMIT = "beta.limit";
    public static final String GP_ALPHA_MAPPING = "alpha.mapping";
}
"""


@pytest.fixture
def repo(repo_copy: Path) -> Path:
    (repo_copy / "src/main/java/x/Defaults.java").write_text(DEFAULTS_JAVA, encoding="utf-8")
    (repo_copy / "src/main/java/x/Constants.java").write_text(CONSTANTS_JAVA, encoding="utf-8")
    return repo_copy


@pytest.fixture
def bre(bre_copy: Path) -> Path:
    rules = bre_copy / "detailed-domain-bre/domain-alpha/business-rules.md"
    rules.write_text(rules.read_text(encoding="utf-8")
                     + "\n## BR-9: Strict mode locks the config\n- `GP_ALPHA_MODE` = strict locks it.\n",
                     encoding="utf-8")
    return bre_copy


def _build(bre: Path, repo: Path | None, keys: list[str] | None = None, cfg: BcpConfig | None = None) -> dict:  # type: ignore[type-arg]
    cfg = cfg or BcpConfig()
    man = load_manifest(bre)
    index = SourceIndex(repo, cfg.source_extensions, cfg.exclude_dirs) if repo else None
    fidx = build_file_index(man)
    layout = BreLayout(bre)
    variants = build_variants(layout, cfg, man.slugs, index, fidx)
    programs = load_snapshots([PROGRAMS], cfg.variant_mask_patterns)
    return build_program_config(layout, man, cfg, index, fidx, programs, keys, variants)


def test_csv_format_quoted_comma_absent_and_empty() -> None:
    (a,) = load_snapshot(PROGRAMS / "SYNTHETIC-program-a.csv", [])
    assert a.program_id == "a" and a.synthetic
    assert a.values["alpha.mapping"] == "1:1, 2:2"  # RFC 4180 quoted comma
    (b,) = load_snapshot(PROGRAMS / "SYNTHETIC-program-b.csv", [])
    assert b.values["alpha.mapping"] == ""  # empty value: the row exists
    assert "beta.limit" not in b.values  # absent: no row


def test_bom_alternate_header_and_json_list(tmp_path: Path) -> None:
    path = tmp_path / "export.csv"
    path.write_bytes(b"\xef\xbb\xbfprogram_id,key,value\r\nz,k.one,1\r\ny,k.one,2\r\n")
    progs = {p.program_id: p for p in load_snapshot(path, [])}
    assert progs["z"].values == {"k.one": "1"} and progs["y"].values == {"k.one": "2"}
    assert not progs["z"].synthetic
    rows = tmp_path / "rows.json"
    rows.write_text(json.dumps([{"program_id": "q", "property": "k.two", "property_value": "x"}]), encoding="utf-8")
    (q,) = load_snapshot(rows, [])
    assert q.values == {"k.two": "x"} and not q.synthetic
    (c,) = load_snapshot(PROGRAMS / "program-c.json", [])
    assert c.synthetic and c.values == {"alpha.mode": "basic", "beta.limit": "10"}


def test_masking_and_clipping(tmp_path: Path) -> None:
    (a,) = load_snapshot(PROGRAMS / "SYNTHETIC-program-a.csv", BcpConfig().variant_mask_patterns)
    assert a.values["alpha.api_token"] == MASKED and a.values["alpha.mode"] == "strict"
    long = tmp_path / "long.csv"
    long.write_text("program_id,property,property_value\nl,k.big," + "x" * 300 + "\n", encoding="utf-8")
    (big,) = load_snapshot(long, [])
    assert big.values["k.big"].startswith("x" * 200) and big.values["k.big"].endswith("(truncated)")


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("program_id,name,value\na,k,v\n", "header must be"),
        ("program_id,property,property_value\na,k,1\na,k,2\n", "duplicate key 'k'"),
        ("program_id,property,property_value\na,k\n", "expected 3 fields"),
    ],
)
def test_bad_files_are_input_errors(tmp_path: Path, content: str, message: str) -> None:
    path = tmp_path / "bad.csv"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(BcpInputError, match=message):
        load_snapshot(path, [])


def test_same_program_in_two_files_is_an_error(tmp_path: Path) -> None:
    for name in ("one.csv", "two.csv"):
        (tmp_path / name).write_text("program_id,property,property_value\np,k,1\n", encoding="utf-8")
    with pytest.raises(BcpInputError, match="appears in one.csv and two.csv"):
        load_snapshots([tmp_path], [])


def test_catalogue_defaults_read_sites_rules_and_near_values(bre: Path, repo: Path) -> None:
    data = _build(bre, repo)
    mode = data["catalogue"]["alpha.mode"]
    assert [c["constant"] for c in mode["constants"]] == ["GP_ALPHA_MODE"]
    assert mode["default"]["value"] == "basic" and not mode["default"]["expression"]
    assert mode["default"]["path"] == "src/main/java/x/Defaults.java"
    sites = {f"{s['path']}:{s['line']}" for s in mode["read_sites"]}
    assert "src/main/java/x/AlphaService.java:11" in sites
    assert "src/main/java/x/Constants.java:4" not in sites  # the definition is not a read site
    assert "alpha" in mode["read_by"]
    assert [r["claim"] for r in mode["rules"]] == ["alpha/BR-9"]
    assert [v["value"] for v in mode["values_near_reads"]] == ["strict"]
    assert data["catalogue"]["alpha.mapping"]["default"] is None
    md = render_program_config_md(data)
    assert "### `alpha.mode`" in md and "Values seen in code near read sites (mechanical)" in md
    assert "alpha/BR-9" in md and "Tier 2" in md


def test_comparison_affects_and_synthetic_labels(bre: Path, repo: Path) -> None:
    data = _build(bre, repo)
    assert set(data["differing_keys"]) == {"alpha.mode", "beta.limit", "alpha.mapping", "alpha.api_token"}
    beta_b = data["comparison"]["beta.limit"]["b"]
    assert not beta_b["present"] and beta_b["effective"] == f"{ABSENT} (10)"
    assert data["comparison"]["alpha.mapping"]["c"]["effective"] == f"{ABSENT} (default not found in code)"
    groups = {g["effective"]: g["programs"] for g in data["affects"]["beta.limit"]["groups"]}
    assert groups == {"5": ["a"], f"{ABSENT} (10)": ["b"], "10": ["c"]}
    assert all(p["synthetic"] for p in data["programs"])
    md = render_program_config_md(data)
    assert "> SYNTHETIC DATA: program(s) a, b, c" in md
    assert "a (SYNTHETIC)" in md and "(empty string)" in md and MASKED in md and "s3cr3t" not in md


def test_markup_attributes_are_not_constants(bre: Path, repo: Path) -> None:
    """Regression: ``value="alpha.mode"`` in seed-data XML must not become a constant named ``value``
    (it made every line naming ``value`` a read site and every rule saying "value" a match)."""
    (repo / "src/main/resources/seed.xml").write_text(
        '<row property="alpha.mode" value="alpha.mode"/>\n<col value="x"/>\n', encoding="utf-8")
    data = _build(bre, repo)
    mode = data["catalogue"]["alpha.mode"]
    assert [c["constant"] for c in mode["constants"]] == ["GP_ALPHA_MODE"]
    sites = {f"{s['path']}:{s['line']}" for s in mode["read_sites"]}
    assert "src/main/resources/seed.xml:1" in sites and "src/main/resources/seed.xml:2" not in sites
    assert [r["claim"] for r in mode["rules"]] == ["alpha/BR-9"]


def test_key_option_resolves_constants(bre: Path, repo: Path) -> None:
    data = _build(bre, repo, ["GP_BETA_LIMIT"])
    assert list(data["affects"]) == ["beta.limit"]
    md = render_program_config_md(data)
    assert "## Programs a change affects" in md and "### `beta.limit`" in md


def test_identical_programs_matching_default_do_not_differ(tmp_path: Path, bre: Path, repo: Path) -> None:
    for pid in ("p", "q"):
        (tmp_path / f"{pid}.csv").write_text(f"program_id,property,property_value\n{pid},alpha.mode,basic\n",
                                             encoding="utf-8")
    cfg = BcpConfig()
    man = load_manifest(bre)
    index = SourceIndex(repo, cfg.source_extensions, cfg.exclude_dirs)
    programs = load_snapshots([tmp_path], [])
    data = build_program_config(BreLayout(bre), man, cfg, index, build_file_index(man), programs)
    assert data["differing_keys"] == []
    assert "No key differs" in render_program_config_md(data)


def test_cli_writes_both_files(bre: Path, repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "out"
    code = main(["variants", "--bre", str(bre), "--repo", str(repo), "--snapshots", str(PROGRAMS),
                 "--out", str(out), "--key", "beta.limit"])
    assert code == 0
    line = capsys.readouterr().out
    assert "variants: 4 keys, 3 programs (3 synthetic), 4 differing keys" in line
    data = json.loads((out / "program-config.json").read_text(encoding="utf-8"))
    assert data["schema"] == "bcp.programs/1" and list(data["affects"]) == ["beta.limit"]
    assert (out / "program-config.md").is_file()


def test_config_keys_validated(tmp_path: Path) -> None:
    cfg = from_dict({"program_snapshots": ["programs"]}, base_dir=PROGRAMS.parent)
    assert cfg.program_snapshots == [str(PROGRAMS)]  # resolved against the config folder
    with pytest.raises(BcpInputError, match="program_snapshots"):
        from_dict({"program_snapshots": ["missing"]}, base_dir=tmp_path)
    with pytest.raises(BcpInputError, match="named groups"):
        from_dict({"variant_default_patterns": [r"get\((\w+)\)"]})
    with pytest.raises(BcpInputError, match="at least 2 groups"):
        from_dict({"variant_constant_patterns": [r"(\w+)="]})


def test_generate_writes_program_config_only_when_configured(workspace: Path) -> None:
    from bcp.generate import generate

    bre = workspace / "business-rules-extraction"
    result = generate(bre, workspace, None, BcpConfig())
    assert ".kiro/context/program-config.md" not in result.files
    impact = json.loads((workspace / ".kiro/agents/impact-analysis.json").read_text(encoding="utf-8"))
    assert "file://.kiro/context/program-config.md" not in impact["resources"]
    result = generate(bre, workspace, None, BcpConfig(program_snapshots=[str(PROGRAMS)]))
    text = (workspace / ".kiro/context/program-config.md").read_text(encoding="utf-8")
    assert text.split("\n")[1].startswith("> Trust: Tier 2") and "SYNTHETIC DATA" in text
    assert ".kiro/context/program-config.json" in result.files
    impact = json.loads((workspace / ".kiro/agents/impact-analysis.json").read_text(encoding="utf-8"))
    res = impact["resources"]
    assert res.index("file://.kiro/context/variant-map.md") < res.index("file://.kiro/context/program-config.md")
