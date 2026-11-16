"""Unit tests for scripts/run_tests.py (patch 01a). Nothing here spawns pytest."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "run_tests_wrapper", Path(__file__).resolve().parents[1] / "scripts" / "run_tests.py")
wrapper = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wrapper)


def test_run_ids_are_unique():
    assert len({wrapper.new_run_id() for _ in range(50)}) == 50


def test_explicit_target_detection():
    assert wrapper.has_explicit_target(["tests/test_a.py", "-q"])
    assert wrapper.has_explicit_target(["tests/test_a.py::test_x"])
    assert not wrapper.has_explicit_target([])
    assert not wrapper.has_explicit_target(["-q"])
    assert not wrapper.has_explicit_target(["-k", "foo", "-q"])  # option values are not targets
    assert not wrapper.has_explicit_target(["--deselect", "tests/a.py::t"])


def test_command_overrides_fixed_paths_per_run(tmp_path):
    cmd = wrapper.build_command(["tests/x.py"], tmp_path / "r1")
    assert cmd[:3] == [sys.executable, "-m", "pytest"]
    assert f"--basetemp={tmp_path / 'r1' / 'basetemp'}" in cmd
    assert f"cache_dir={tmp_path / 'r1' / 'cache'}" in cmd
    other = wrapper.build_command(["tests/x.py"], tmp_path / "r2")
    paths = lambda c: {x for x in c[-4:] if x != "-o"}
    assert not paths(cmd) & paths(other)  # no shared path between runs


def test_whole_suite_run_is_refused_without_opt_in(tmp_path, capsys):
    assert wrapper.main(["-q", "--runs-dir", str(tmp_path)]) == 2
    assert not list(tmp_path.iterdir())  # refused before creating anything
    assert "refused" in capsys.readouterr().err
