"""Tests for uv.lock alignment after the update dependency reinstall.

``hermes update`` reinstalls dependencies with ``uv pip install -e .[all]``,
which resolves from pyproject.toml constraints and never reads ``uv.lock``.
A lockfile-only version bump (the ``fix(sec)`` pattern) therefore leaves an
existing venv on the old release even after a successful update. These tests
cover the pin collection, the alignment plan, and the non-fatal contract of
the install step itself.
"""

from __future__ import annotations

import subprocess

import hermes_cli.main as m

LOCK = """
version = 1

[[package]]
name = "httplib2"
version = "0.32.0"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "PyNaCl"
version = "1.6.2"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "hermes-agent"
version = "0.19.1"
source = { editable = "." }

[[package]]
name = "forked-dep"
version = "1.0.0"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "forked-dep"
version = "2.0.0"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "git-dep"
version = "0.1.0"
source = { git = "https://example.invalid/repo?rev=abc" }
"""


def _write_lock(tmp_path):
    (tmp_path / "uv.lock").write_text(LOCK, encoding="utf-8")


def test_collect_lockfile_pins_filters_and_canonicalizes(tmp_path):
    _write_lock(tmp_path)
    pins = m._collect_lockfile_pins(tmp_path)
    # Registry packages survive with canonical names; the editable project,
    # the git source, and the platform-forked double entry are all skipped.
    assert pins == {"httplib2": "0.32.0", "pynacl": "1.6.2"}


def test_collect_lockfile_pins_missing_lock(tmp_path):
    assert m._collect_lockfile_pins(tmp_path) == {}


def test_collect_lockfile_pins_unparseable_lock(tmp_path):
    (tmp_path / "uv.lock").write_text("not = [valid", encoding="utf-8")
    assert m._collect_lockfile_pins(tmp_path) == {}


def test_plan_only_targets_drifted_installed_packages():
    pins = {"httplib2": "0.32.0", "pynacl": "1.6.2", "pygments": "2.20.0"}
    installed = {"httplib2": "0.31.2", "pynacl": "1.6.2", "requests": "2.32.0"}
    # Drifted installed package is realigned; matching stays untouched;
    # a locked package that is not installed is never added.
    assert m._plan_lockfile_alignment(pins, installed) == ["httplib2==0.32.0"]


def test_plan_is_empty_when_everything_matches():
    pins = {"httplib2": "0.32.0"}
    installed = {"httplib2": "0.32.0"}
    assert m._plan_lockfile_alignment(pins, installed) == []


def test_align_runs_single_batched_install(tmp_path, monkeypatch):
    _write_lock(tmp_path)
    monkeypatch.setattr(m, "PROJECT_ROOT", tmp_path)
    calls = []

    monkeypatch.setattr(
        m,
        "_list_installed_package_versions",
        lambda prefix, *, env=None: {"httplib2": "0.31.2", "pynacl": "1.6.2"},
    )

    def fake_install(cmd, *, env=None, scripts_dir=None):
        calls.append(cmd)

    monkeypatch.setattr(m, "_run_quarantined_install", fake_install)

    m._align_installed_packages_with_lockfile(["uv", "pip"])
    assert calls == [["uv", "pip", "install", "httplib2==0.32.0"]]


def test_align_no_op_without_lockfile(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "PROJECT_ROOT", tmp_path)

    def explode(*args, **kwargs):  # pragma: no cover - guards the no-op path
        raise AssertionError("should not be called without a lockfile")

    monkeypatch.setattr(m, "_list_installed_package_versions", explode)
    monkeypatch.setattr(m, "_run_quarantined_install", explode)
    m._align_installed_packages_with_lockfile(["uv", "pip"])


def test_align_is_non_fatal_on_install_failure(tmp_path, monkeypatch, capsys):
    _write_lock(tmp_path)
    monkeypatch.setattr(m, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        m,
        "_list_installed_package_versions",
        lambda prefix, *, env=None: {"httplib2": "0.31.2"},
    )

    def boom(cmd, *, env=None, scripts_dir=None):
        raise subprocess.CalledProcessError(2, cmd)

    monkeypatch.setattr(m, "_run_quarantined_install", boom)

    m._align_installed_packages_with_lockfile(["uv", "pip"])
    out = capsys.readouterr().out
    assert "Could not align packages with uv.lock" in out
    assert "exit 2" in out
