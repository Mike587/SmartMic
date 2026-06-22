# -*- coding: utf-8 -*-
"""Unit tests for MS_image_analysis.run_analysis.

subprocess.run is mocked so we test the function's logic (command assembly,
PIXI_* env stripping, return-code mapping, output-folder creation) without
actually launching pixi or an analysis script.
"""
import logging

import pytest

import MS_image_analysis as ia


class _Result:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


_LOG = logging.getLogger("test_image_analysis")


def test_run_analysis_success_strips_pixi_env(monkeypatch, tmp_path):
    captured = {}

    def fake_run(cmd, cwd, capture_output, text, env):
        captured["cmd"] = cmd
        captured["cwd"] = cwd
        captured["env"] = env
        return _Result(0, stdout="analysis ok")

    monkeypatch.setattr(ia.subprocess, "run", fake_run)
    monkeypatch.setenv("PIXI_TEST_VAR", "should_be_stripped")
    monkeypatch.setenv("KEEP_ME", "yes")

    out_dir = tmp_path / "out"
    ok = ia.run_analysis(tmp_path / "img.czi", out_dir, "D9_P1", _LOG, tmp_path / "analyze.py")

    assert ok is True
    assert out_dir.exists()                                   # output folder created
    assert captured["cmd"][:3] == ["pixi", "run", "python"]
    assert "--prefix" in captured["cmd"] and "D9_P1" in captured["cmd"]
    assert not any(k.startswith("PIXI_") for k in captured["env"])   # PIXI_* stripped
    assert captured["env"].get("KEEP_ME") == "yes"            # other env preserved


def test_run_analysis_defaults_cwd_to_script_dir(monkeypatch, tmp_path):
    captured = {}

    def fake_run(cmd, cwd, capture_output, text, env):
        captured["cwd"] = cwd
        return _Result(0)

    monkeypatch.setattr(ia.subprocess, "run", fake_run)
    script = tmp_path / "sub" / "analyze.py"
    script.parent.mkdir()
    ia.run_analysis(tmp_path / "img.czi", tmp_path / "out", "T", _LOG, script)
    assert captured["cwd"] == str(script.parent)


def test_run_analysis_failure_returns_false(monkeypatch, tmp_path):
    def fake_run(cmd, cwd, capture_output, text, env):
        return _Result(1, stderr="bad things")

    monkeypatch.setattr(ia.subprocess, "run", fake_run)
    ok = ia.run_analysis(tmp_path / "img.czi", tmp_path / "out", "T", _LOG, tmp_path / "a.py")
    assert ok is False


def test_run_analysis_appends_extra_args(monkeypatch, tmp_path):
    captured = {}

    def fake_run(cmd, cwd, capture_output, text, env):
        captured["cmd"] = cmd
        return _Result(0)

    monkeypatch.setattr(ia.subprocess, "run", fake_run)
    ia.run_analysis(tmp_path / "i.czi", tmp_path / "o", "T", _LOG, tmp_path / "a.py",
                    extra_args=["--flag", 7])
    assert "--flag" in captured["cmd"] and "7" in captured["cmd"]
