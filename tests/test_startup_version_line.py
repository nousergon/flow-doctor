"""The startup line proves which flow-doctor a process runs (alpha-engine-config-I7930).

A box builds its venv quietly from a requirements file that pins flow-doctor
only transitively, so on 2026-10-21 no box log could show whether 0.17.0 (the
unavailable-diagnosis detector) was installed: the one flow-doctor line in the
run log, ``[flow-doctor] initialized: ...``, carried no version. These tests
are the contract that it now does, and that the probe can never break init.
"""

from __future__ import annotations

import json
import logging

import flow_doctor
from flow_doctor import FlowDoctor
from flow_doctor.core import provenance
from flow_doctor.core.provenance import format_provenance, runtime_provenance

FLOW = "startup-version-line"


def _init_line(caplog, tmp_path) -> str:
    caplog.set_level(logging.INFO, logger="flow_doctor")
    FlowDoctor.builder(FLOW).with_store(path=str(tmp_path / "fd.db")).build()
    lines = [r.getMessage() for r in caplog.records if "[flow-doctor] initialized:" in r.getMessage()]
    assert len(lines) == 1, lines
    return lines[0]


def test_init_line_carries_the_running_version_and_import_path(caplog, tmp_path):
    line = _init_line(caplog, tmp_path)
    assert f"version={flow_doctor.__version__}" in line
    assert "source=" in line
    assert runtime_provenance()["path"] in line


def test_existing_leading_components_are_unchanged(caplog, tmp_path):
    # Readers matching "initialized: <flow> (store=" must keep matching.
    line = _init_line(caplog, tmp_path)
    assert line.startswith(f"[flow-doctor] initialized: {FLOW} (store=sqlite, notifiers=0, ")


def test_a_failing_probe_does_not_break_init(caplog, tmp_path, monkeypatch):
    def boom():
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(provenance, "runtime_provenance", boom)
    line = _init_line(caplog, tmp_path)
    assert "version=unknown(probe-error=RuntimeError)" in line


class _FakeDist:
    def __init__(self, version: str, direct_url):
        self.version = version
        self._direct_url = direct_url

    def read_text(self, name):
        assert name == "direct_url.json"
        return None if self._direct_url is None else json.dumps(self._direct_url)


def _with_dist(monkeypatch, dist):
    from importlib import metadata

    def fake_distribution(name):
        assert name == "flow-doctor"
        if dist is None:
            raise metadata.PackageNotFoundError(name)
        return dist

    monkeypatch.setattr(metadata, "distribution", fake_distribution)


def test_index_install_reports_index(monkeypatch):
    _with_dist(monkeypatch, _FakeDist(flow_doctor.__version__, None))
    prov = runtime_provenance()
    assert prov["source"] == "index"
    assert prov["dist_version"] == flow_doctor.__version__
    assert "MISMATCH" not in format_provenance(prov)


def test_vcs_install_reports_url_and_commit(monkeypatch):
    _with_dist(
        monkeypatch,
        _FakeDist(
            flow_doctor.__version__,
            {
                "url": "https://github.com/nousergon/flow-doctor.git",
                "vcs_info": {"vcs": "git", "commit_id": "bea1ee9a0b1c2d3e4f5061728394a5b6c7d8e9f0"},
            },
        ),
    )
    assert runtime_provenance()["source"] == "git+https://github.com/nousergon/flow-doctor.git@bea1ee9a0b1c"


def test_editable_install_reports_editable(monkeypatch):
    _with_dist(
        monkeypatch,
        _FakeDist(flow_doctor.__version__, {"url": "file:///src/flow-doctor", "dir_info": {"editable": True}}),
    )
    assert runtime_provenance()["source"] == "editable:file:///src/flow-doctor"


def test_no_distribution_reports_not_installed(monkeypatch):
    _with_dist(monkeypatch, None)
    prov = runtime_provenance()
    assert prov["source"] == "not-installed"
    assert prov["dist_version"] is None
    assert format_provenance(prov).startswith(f"version={flow_doctor.__version__}, source=not-installed")


def test_code_shadowing_a_different_install_is_flagged(monkeypatch):
    # The imported module is not the installed distribution: the line must say so.
    _with_dist(monkeypatch, _FakeDist("0.0.1", None))
    rendered = format_provenance(runtime_provenance())
    assert f"version={flow_doctor.__version__}(installed-dist=0.0.1,MISMATCH)" in rendered
