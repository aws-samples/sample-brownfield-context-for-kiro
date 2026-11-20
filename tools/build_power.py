"""Build the Kiro Power: dist/brownfield-context/ (vendored bcp in each skill) and a zip.

Usage: python tools/build_power.py [--out DIR]
"""

from __future__ import annotations

import argparse
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
POWER_SRC = ROOT / "power" / "brownfield-context"
NAME = "brownfield-context"

BCP_CLI = '''"""Run the vendored bcp package: python bcp_cli.py <command> ..."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bcp.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
'''


def version() -> str:
    """Package version from src/bcp/__init__.py."""
    sys.path.insert(0, str(SRC))
    from bcp import __version__

    return __version__


def build(out: Path) -> tuple[Path, Path]:
    """Copy the Power source, vendor bcp into every skill's scripts/ folder, and zip it."""
    out.mkdir(parents=True, exist_ok=True)
    package = out / NAME
    if package.exists():
        shutil.rmtree(package)
    shutil.copytree(POWER_SRC, package)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    # knowledge-base rebuild kit: the merged reference and infra/ (template, IAM policies, config example)
    # side by side, so the reference's infra/... paths resolve in the shipped copy too
    refs = package / "skills" / "brownfield-context-setup" / "references"
    shutil.copy2(ROOT / "docs" / "reference.md", refs / "reference.md")
    shutil.copytree(ROOT / "infra", refs / "infra", ignore=ignore, dirs_exist_ok=True)
    # fallback installer scripts, so the README's tools/install-skills.* path exists in the shipped
    # package too (for readers who cannot import the Power through the IDE Powers panel)
    tools_dir = package / "tools"
    tools_dir.mkdir(exist_ok=True)
    for script in ("install-skills.sh", "install-skills.ps1"):
        shutil.copy2(ROOT / "tools" / script, tools_dir / script)
    for skill in sorted(p for p in (package / "skills").iterdir() if p.is_dir()):
        scripts = skill / "scripts"
        scripts.mkdir(exist_ok=True)
        shutil.copytree(SRC / "bcp", scripts / "bcp", ignore=ignore)
        (scripts / "bcp_cli.py").write_text(BCP_CLI, encoding="utf-8", newline="\n")
    archive = out / f"{NAME}-{version()}.zip"
    if archive.exists():
        archive.unlink()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(package.rglob("*")):
            if path.is_file():
                zf.write(path, Path(NAME) / path.relative_to(package))
    return package, archive


def main(argv: list[str] | None = None) -> int:
    """Build, then run check-power on the result."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "dist")
    args = parser.parse_args(argv)
    package, archive = build(args.out)
    sys.path.insert(0, str(SRC))
    from bcp.powercheck import check_power

    report = check_power(package)
    for error in report.errors:
        print(f"ERROR {error}")
    status = "PASS" if report.ok else "FAIL"
    print(f"check-power: {status} ({len(report.skills)} skills: {', '.join(report.skills)})")
    print(f"built {package}")
    print(f"built {archive}")
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
