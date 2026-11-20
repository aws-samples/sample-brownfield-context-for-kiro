"""Tests for bcp.sourceindex (class lookup, three resolution states, path order)."""

from __future__ import annotations

from pathlib import Path

import pytest

from bcp import claims
from bcp.claims import classify, is_non_code
from bcp.config import DEFAULT_EXCLUDE_DIRS, DEFAULT_EXTENSIONS
from bcp.sourceindex import (
    BRE_DOC,
    METHOD_MISSING,
    RESOLVED,
    UNRESOLVED,
    SourceIndex,
    declaration_name,
    evidence_anchor,
    resolution_ratio,
    resolve_claim,
    type_name,
)

EXT = list(DEFAULT_EXTENSIONS)
ALPHA = ["src/main/java/x/AlphaService.java", "src/main/java/x/Alpha.java"]


@pytest.fixture
def index(repo_mini: Path) -> SourceIndex:
    return SourceIndex(repo_mini, EXT, DEFAULT_EXCLUDE_DIRS)


def res(index: SourceIndex, raw: str, bre: Path | None = None):  # type: ignore[no-untyped-def]
    return index.resolve(classify(raw, EXT, None), ALPHA, ALPHA, bre)


def test_index_and_has_source(index: SourceIndex, tmp_path: Path) -> None:
    assert index.has_source()
    assert "lib/util.py" in index.paths
    assert not SourceIndex(tmp_path, EXT, DEFAULT_EXCLUDE_DIRS).has_source()


def test_main_preferred_over_test(index: SourceIndex) -> None:
    r = res(index, "AlphaService")
    assert (r.status, r.path, r.how, r.line) == (RESOLVED, "src/main/java/x/AlphaService.java", "class-file", 1)


def test_class_method_line_not_call_site(index: SourceIndex) -> None:
    r = res(index, "BetaService.saveBeta")
    assert (r.status, r.how, r.line) == (RESOLVED, "method-line", 5)


def test_fqn_package_path_preferred(index: SourceIndex) -> None:
    r = index.resolve(classify("y.GammaHelper", EXT, None))
    assert (r.status, r.path, r.how) == (RESOLVED, "src/main/java/y/GammaHelper.java", "fqn-path")
    r = res(index, "x.AlphaService.purgeAlpha")
    assert (r.status, r.path, r.how) == (RESOLVED, "src/main/java/x/AlphaService.java", "method-line")


def test_field_token_is_method_missing(index: SourceIndex) -> None:
    r = res(index, "Alpha.status")
    assert (r.status, r.path, r.line, r.how) == (METHOD_MISSING, "src/main/java/x/Alpha.java", 1, "class-file")


def test_missing_class_unresolved(index: SourceIndex) -> None:
    assert res(index, "ZetaEngine").status == UNRESOLVED


def test_path_resolution_order(index: SourceIndex, bre_mini: Path) -> None:
    r = res(index, "app/services/orders.py")
    assert (r.status, r.how) == (RESOLVED, "path")
    r = res(index, "alpha.hbm.xml")
    assert (r.status, r.how, r.path) == (RESOLVED, "filename-search", "src/main/resources/alpha.hbm.xml")
    r = res(index, "AlphaService.java")
    assert (r.how, r.alternatives, r.path) == ("filename-search", 2, "src/main/java/x/AlphaService.java")
    r = res(index, "reference-documentation/coupling-hotspots.md", bre_mini)
    assert (r.status, r.path) == (BRE_DOC, "reference-documentation/coupling-hotspots.md")
    assert res(index, "no/such/file.java", bre_mini).status == UNRESOLVED


def test_glob_scope_crosses_slash(index: SourceIndex) -> None:
    assert index.glob_scope("app/*handler*.py") == ["app/services/sub/x_handler.py"]


@pytest.mark.parametrize(("line", "name"), [
    ("    public Alpha saveAlpha(Alpha alpha) {", "saveAlpha"),
    ("\tprivate static <T> List<T> load(Class<T> c)", "load"),
    ("    Alpha getAlpha(Integer id) throws APIException;", "getAlpha"),  # interface member, no modifier
    ("    void purge(Alpha a);", "purge"),
    ("def handle_order(self, order):", "handle_order"),
    ("    async def fetch(x):", "fetch"),
    ("func (s *Server) Start(ctx context.Context) error {", "Start"),
    ("export async function loadUser(id) {", "loadUser"),
    ("sub process_record {", "process_record"),
    ("        return saveAlpha(alpha);", None),
    ("        if (alpha.inUse()) {", None),
    ("            throw new APIException(\"x\");", None),
    ("        String mode = Config.get(Constants.GP_ALPHA_MODE);", None),
    ("        betaService.saveBeta(alpha);", None),
    ("    // public void commented(int x) {", None),
    ("    @PreAuthorize(\"hasPermission('x')\")", None),
])
def test_declaration_name(line: str, name: str | None) -> None:
    assert declaration_name(line) == name


def test_type_name() -> None:
    assert type_name("public final class AlphaService extends Base {") == "AlphaService"
    assert type_name("class Handler(Base):") == "Handler"
    assert type_name("type Server struct {") == "Server"
    assert type_name("    String classification = x;") is None
    assert type_name(" * the class Foo is documented here") is None


def _range(start: int, end: int, path: str = "src/main/java/x/AlphaService.java") -> dict:  # type: ignore[type-arg]
    lines = (REPO_MINI_JAVA.read_text(encoding="utf-8")).split("\n")
    return {"kind": "range", "path": path, "start": start, "end": end, "quote": "\n".join(lines[start - 1:end])}


REPO_MINI_JAVA = Path(__file__).parent / "fixtures/repo-mini/src/main/java/x/AlphaService.java"


def test_anchor_uses_declaration_in_range(index: SourceIndex) -> None:
    anchor = evidence_anchor(_range(6, 14), index)
    assert anchor is not None
    assert (anchor.line, anchor.member, anchor.quote, anchor.moved) == (
        7, "AlphaService.saveAlpha", "public Alpha saveAlpha(Alpha alpha) {", False)


def test_anchor_inside_body_finds_enclosing_member(index: SourceIndex) -> None:
    anchor = evidence_anchor(_range(17, 19), index)
    assert anchor is not None
    assert (anchor.line, anchor.member) == (17, "AlphaService.purgeAlpha")
    assert anchor.quote == "if (alpha.inUse()) {"
    # without the current source the member falls back to the file's type name
    bare = evidence_anchor(_range(17, 19), None)
    assert bare is not None and bare.member == "AlphaService"


def test_anchor_field_has_type_only_and_skips_comments(index: SourceIndex) -> None:
    ev = {"kind": "range", "path": "src/main/java/x/AlphaService.java", "start": 4, "end": 5,
          "quote": "\n    private final BetaService betaService = new BetaService();"}
    anchor = evidence_anchor(ev, index)
    assert anchor is not None and (anchor.line, anchor.member) == (5, "AlphaService")


def test_anchor_moved_line_and_non_range(index: SourceIndex) -> None:
    ev = _range(16, 20)
    ev["start"] = 30  # recorded before lines were inserted above; the declaration is now at line 16
    anchor = evidence_anchor(ev, index)
    assert anchor is not None and (anchor.line, anchor.moved) == (16, True)
    assert evidence_anchor({"kind": "search", "pattern": "x", "scope": "*"}, index) is None
    assert evidence_anchor({"kind": "range", "path": "a.java", "start": 1, "end": 1, "quote": "\n}"}, None) is None


def test_anchor_skips_sibling_nested_type(tmp_path: Path) -> None:
    src = tmp_path / "src/P.java"
    src.parent.mkdir(parents=True)
    src.write_text("public class P {\n    enum Kind { A, B }\n\n    public void run() {\n        go();\n    }\n}\n",
                   encoding="utf-8")
    idx = SourceIndex(tmp_path, EXT, DEFAULT_EXCLUDE_DIRS)
    ev = {"kind": "range", "path": "src/P.java", "start": 4, "end": 6,
          "quote": "    public void run() {\n        go();\n    }"}
    anchor = evidence_anchor(ev, idx)
    assert anchor is not None and anchor.member == "P.run"


def _claim(text: str):  # type: ignore[no-untyped-def]
    return claims.parse(text, extensions=EXT)[0]


@pytest.mark.parametrize("suffix", ["#7-14", "#L7-L14", "#7", "#L7", "#7-9,12-14"])
def test_path_with_line_range_anchor_resolves(index: SourceIndex, suffix: str) -> None:
    """A citation's ``<path>#<start>-<end>`` anchor (BRE rule 5) resolves to the file and start line."""
    tok = classify("src/main/java/x/AlphaService.java" + suffix, EXT, None)
    assert tok.kind == "path" and not is_non_code(tok)
    r = index.resolve(tok, ALPHA, ALPHA)
    assert (r.status, r.path, r.line, r.how) == (RESOLVED, "src/main/java/x/AlphaService.java", 7, "path")


def test_evidence_path_citation_is_verified_not_text(index: SourceIndex) -> None:
    """The AWS Transform ``Evidence: `<path>#<start>-<end>` `` form counts as a resolved citation."""
    claim = _claim("## BR-1: A\n* Evidence: `src/main/java/x/AlphaService.java#6-14`\n")
    out = resolve_claim(index, claim, ALPHA, ALPHA)
    assert [c["token"] for c in out.candidates] == ["src/main/java/x/AlphaService.java#6-14"]
    assert out.candidates[0]["status"] == RESOLVED and out.candidates[0]["line"] == 6
    assert out.text_tokens == [] and out.unresolved == []


def test_bound_miss_reclassified_as_text(index: SourceIndex) -> None:
    claim = _claim("## BR-1: A\n- Source: `AlphaService.saveAlpha` / `patient` / `unvoidAlpha`.\n")
    out = resolve_claim(index, claim, ALPHA, ALPHA)
    assert [c["token"] for c in out.candidates] == ["AlphaService.saveAlpha", "unvoidAlpha"]
    assert out.text_tokens == ["patient"]
    assert out.unresolved == []


def test_explicit_method_miss_stays_candidate_and_counts(index: SourceIndex, bre_mini: Path) -> None:
    claim = _claim(
        "## BR-1: A\n- Source: `Alpha.status`, `AlphaService.saveAlpha`, `ZetaEngine`, "
        "`reference-documentation/coupling-hotspots.md`, `IF a / b`.\n"
    )
    out = resolve_claim(index, claim, ALPHA, ALPHA, bre_mini)
    states = {c["token"]: c["status"] for c in out.candidates}
    assert states == {"Alpha.status": METHOD_MISSING, "AlphaService.saveAlpha": RESOLVED}
    assert out.unresolved == ["ZetaEngine"]
    assert out.bre_refs == ["reference-documentation/coupling-hotspots.md"]
    assert out.text_tokens == ["IF a / b"]
    assert [t.raw for t, _ in out.missing] == ["Alpha.status", "ZetaEngine", "IF a / b"]
    assert resolution_ratio(out.candidates, out.unresolved) == pytest.approx(1 / 3)
    assert resolution_ratio([], []) == 1.0
