"""Tests for tools/build_power.py: the built package passes check-power and its scripts run."""

from __future__ import annotations

import importlib.util
import zipfile
from pathlib import Path

from bcp.powercheck import check_power
from conftest import run_python

ROOT = Path(__file__).resolve().parents[1]


def _load_builder():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("build_power", ROOT / "tools" / "build_power.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_build_check_and_run(tmp_path: Path) -> None:
    builder = _load_builder()
    package, archive = builder.build(tmp_path)
    report = check_power(package)
    assert report.ok, report.errors
    assert report.skills == ["bre-doc-drift-check", "bre-review", "brownfield-context-setup"]
    for skill in report.skills:
        scripts = package / "skills" / skill / "scripts"
        assert (scripts / "bcp_cli.py").is_file()
        assert (scripts / "bcp" / "templates" / "steering" / "bre-use-brownfield-agent.md.tmpl").is_file()
        assert (scripts / "bcp" / "agents.py").is_file()
        assert not list(scripts.rglob("__pycache__"))
    cli = package / "skills" / "brownfield-context-setup" / "scripts" / "bcp_cli.py"
    proc = run_python([str(cli), "--help"], capture_output=True, text=True, timeout=120, cwd=tmp_path)
    assert proc.returncode == 0 and "check-power" in proc.stdout
    # vendored templates load from the vendored package, not from src/
    code = ("import sys; sys.path.insert(0, sys.argv[1]); import bcp.render as r; "
            "print(r.__file__); print(len(r.load_template('steering/product.md.tmpl').template))")
    proc = run_python(["-c", code, str(cli.parent)], capture_output=True, text=True, timeout=120, cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert str(cli.parent) in proc.stdout.splitlines()[0]
    with zipfile.ZipFile(archive) as zf:
        names = zf.namelist()
    assert "brownfield-context/plugin.json" in names
    assert "brownfield-context/skills/bre-review/scripts/bcp_cli.py" in names
    refs = "brownfield-context/skills/brownfield-context-setup/references/"
    for rel in ("reference.md", "infra/cloudformation/validated-kb.yaml",
                "infra/iam/deployer-policy.json", "infra/iam/reader-policy.json",
                "infra/config/kb-config.example.json"):
        assert refs + rel in names, rel
    shipped = package / "skills" / "brownfield-context-setup" / "references"
    assert (shipped / "infra" / "cloudformation" / "validated-kb.yaml").read_bytes() == \
        (ROOT / "infra" / "cloudformation" / "validated-kb.yaml").read_bytes()
    assert (shipped / "reference.md").read_bytes() == \
        (ROOT / "docs" / "reference.md").read_bytes()
    assert (scripts / "bcp" / "kb_provision.py").is_file()
