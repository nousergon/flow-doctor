"""Lockstep invariant: pyproject.toml::version == flow_doctor.__version__.

``auto-tag.yml`` reads the version from ``pyproject.toml`` to cut the
``vX.Y.Z`` git tag on push-to-main, while consumers see
``flow_doctor.__version__`` at runtime. If the two drift, the tag and the
installed package disagree about what version shipped. This test is the
chokepoint that keeps them in lockstep — bump both or neither.
"""
from __future__ import annotations

import re
from pathlib import Path

import flow_doctor

_PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def _pyproject_version() -> str:
    """Parse [project].version from pyproject.toml.

    Prefers a real TOML parser (tomllib on 3.11+, tomli if installed) and
    falls back to a focused regex on the ``[project]`` table so the test
    runs dependency-free on Python 3.9/3.10.
    """
    raw = _PYPROJECT.read_text(encoding="utf-8")
    try:
        try:
            import tomllib  # type: ignore[import-not-found]
        except ModuleNotFoundError:  # Python < 3.11
            import tomli as tomllib  # type: ignore[no-redef]
        return tomllib.loads(raw)["project"]["version"]
    except ModuleNotFoundError:
        # Regex fallback: first ``version = "..."`` after the [project] header.
        section = raw.split("[project]", 1)[1]
        match = re.search(r'^\s*version\s*=\s*"([^"]+)"', section, re.MULTILINE)
        assert match, "could not locate [project].version in pyproject.toml"
        return match.group(1)


def test_version_pin_lockstep() -> None:
    assert flow_doctor.__version__ == _pyproject_version(), (
        f"Version drift: flow_doctor.__version__={flow_doctor.__version__!r} "
        f"but pyproject.toml::version={_pyproject_version()!r}. Bump both "
        f"(flow_doctor/__init__.py and pyproject.toml) to the same value."
    )
