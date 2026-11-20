"""Tests for bcp.textio."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import pytest

from bcp import textio
from bcp.errors import BcpInputError
from bcp.textio import read_text, replace_with_retry, safe_join, sha256_file, sha256_text, write_text


def test_read_text_strips_bom_and_replaces_bad_bytes(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    good = tmp_path / "bom.md"
    good.write_bytes(b"\xef\xbb\xbfhello")
    assert read_text(good) == "hello"
    bad = tmp_path / "bad.md"
    bad.write_bytes(b"ok \xff end")
    with caplog.at_level(logging.WARNING):
        assert "ok" in read_text(bad)
    assert "bad.md" in caplog.text


def test_write_text_uses_lf(tmp_path: Path) -> None:
    path = tmp_path / "a" / "b.md"
    write_text(path, "x\r\ny\rz\n")
    assert path.read_bytes() == b"x\ny\nz\n"


def test_hashes(tmp_path: Path) -> None:
    path = tmp_path / "f.txt"
    path.write_bytes(b"abc")
    assert sha256_file(path) == sha256_text("abc")


@pytest.mark.skipif(sys.platform != "win32", reason="open files block os.replace only on Windows")
def test_replace_with_retry_waits_for_a_reader_to_close(tmp_path: Path) -> None:
    """Real-data fix: generate failed with WinError 5 while Kiro had a steering file open."""
    dest = tmp_path / "dest.md"
    dest.write_text("old", encoding="utf-8")
    src = tmp_path / "src.tmp"
    src.write_text("new", encoding="utf-8")
    handle = open(dest, "rb")  # noqa: SIM115 - held open on purpose, like a Kiro reader
    try:
        with pytest.raises(PermissionError):
            os.replace(src, dest)  # the failure the fix handles
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(textio.time, "sleep", lambda _s: handle.close())  # the reader closes while we wait
            replace_with_retry(src, dest)
    finally:
        handle.close()
    assert dest.read_text(encoding="utf-8") == "new"


def test_replace_with_retry_gives_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def busy(src: object, dest: object) -> None:
        calls.append(1)
        raise PermissionError("busy")

    monkeypatch.setattr(textio.os, "replace", busy)
    monkeypatch.setattr(textio, "REPLACE_DELAY_S", 0)
    with pytest.raises(PermissionError):
        replace_with_retry(tmp_path / "a", tmp_path / "b")
    assert len(calls) == textio.REPLACE_RETRIES


@pytest.mark.parametrize("rel", ["/etc/passwd", "C:/x", "..", "a/../../b", "a\\..\\..\\b", "", "\\x"])
def test_safe_join_rejects(tmp_path: Path, rel: str) -> None:
    with pytest.raises(BcpInputError):
        safe_join(tmp_path, rel)


def test_safe_join_accepts(tmp_path: Path) -> None:
    assert safe_join(tmp_path, "a/b.txt") == (tmp_path / "a" / "b.txt").resolve()
