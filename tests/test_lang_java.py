"""Tests for the language plugins (bcp.lang)."""

from __future__ import annotations

from pathlib import Path

from bcp.lang import for_path
from bcp.lang.generic import GENERIC
from bcp.lang.java import JAVA, mask

JAVA_DIR = Path(__file__).parent / "fixtures" / "repo-mini" / "src" / "main" / "java" / "x"


def lines_of(name: str) -> list[str]:
    return (JAVA_DIR / name).read_text(encoding="utf-8").split("\n")


def test_for_path() -> None:
    assert for_path("a/B.java") is JAVA and for_path("a/b.py") is GENERIC


def test_mask_blanks_strings_and_comments() -> None:
    src = ['String s = "a { b"; // } c', "/* {", " } */ int x = '{';", 'String t = "\\"}";']
    out = mask(src)
    assert "{" not in out[0] and "}" not in out[0] and out[0].startswith('String s = "')
    assert out[1].strip() == "" and "{" not in out[2] and "}" not in out[2] and "int x" in out[2]
    assert "}" not in out[3]


def test_method_range_ignores_braces_in_strings_and_comments() -> None:
    lines = lines_of("GuardedApiImpl.java")
    decl = JAVA.method_decls(lines, "saveGuarded")
    assert decl == [8]
    assert JAVA.method_range(lines, 8) == (8, 14)
    get = JAVA.method_decls(lines, "getGuarded")[0]
    assert JAVA.method_range(lines, get) == (get, get + 2)


def test_abstract_method_range_ends_at_semicolon() -> None:
    lines = lines_of("GuardedApi.java")
    assert JAVA.method_decls(lines, "saveGuarded") == [15]
    assert JAVA.method_range(lines, 15) == (15, 15)
    assert JAVA.method_decls(lines, "getGuarded") == [21]  # generic return type


def test_multi_line_annotations_across_comments() -> None:
    lines = lines_of("GuardedApi.java")
    save = JAVA.annotations_before(lines, 15)
    assert [(a.name, a.start, a.end) for a in save] == [("PreAuthorize", 13, 14)]
    assert "hasAuthority('EDIT_GUARDED')" in save[0].text
    get = JAVA.annotations_before(lines, 21)
    assert [(a.name, a.start, a.end) for a in get] == [("Authorized", 18, 19)]
    assert JAVA.annotations_before(lines, 23) == []


def test_inline_and_override_annotations() -> None:
    lines = ["class A {", "  @Override @Authorized({\"X\"}) public void run() {", "  }", "}"]
    names = [a.name for a in JAVA.annotations_before(lines, 2)]
    assert names == ["Override", "Authorized"]
    impl = lines_of("GuardedApiImpl.java")
    assert [a.name for a in JAVA.annotations_before(impl, 8)] == ["Override"]


def test_class_annotations_and_implemented_types() -> None:
    lines = ["package x;", "", "@Transactional", "@Authorized(", "  \"ADMIN\")",
             "public class Svc extends Base<Long>", "    implements Api, other.Second<A, B> {", "}"]
    assert [a.name for a in JAVA.class_annotations(lines)] == ["Transactional", "Authorized"]
    assert JAVA.implemented_types(lines) == ["Base", "Api", "Second"]
    assert JAVA.implemented_types(lines_of("GuardedApiImpl.java")) == ["BaseService", "GuardedApi"]
    assert JAVA.implemented_types(["public interface I extends A, B {", "}"]) == ["A", "B"]


def test_imports() -> None:
    lines = ["import java.util.List;", "import static org.x.Y.z;", "importx foo;"]
    assert JAVA.imports(lines) == ["java.util.List", "org.x.Y.z"]


def test_generic_plugin() -> None:
    lines = ["def run(x):", "    return x"]
    assert GENERIC.method_decls(lines, "run") == [1]
    assert GENERIC.method_range(lines, 1) == (1, 1)
    assert GENERIC.annotations_before(lines, 1) == [] and GENERIC.implemented_types(lines) == []
