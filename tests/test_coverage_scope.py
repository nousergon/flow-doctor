"""The coverage gate's *scope* is asserted here, not only its number.

repository-baseline-policy.md §4.2 C5: the way a coverage gate stops being
honest is by narrowing what it measures rather than by lowering the number —
which reads as an improvement in every report. Measured on symposion, removing
one flag moved the reported figure from 34.76% to 92.36% with no new test code.

So these tests assert three things a passing suite cannot otherwise notice:

* the measured source is the WHOLE ``flow_doctor`` package (C1), never a
  subset of it;
* the floor is enforced by a non-zero exit (C2) and is a ratchet that may be
  raised and never lowered (C3);
* nothing is quietly excluded from measurement by ``[tool.coverage.run]
  omit``, which would shrink the denominator without touching either number.

Parsed with plain regex/line scanning rather than a TOML library — CI runs on
3.12 (stdlib ``tomllib``) but the local dev venv floors at 3.9, and this repo
declares no TOML-parsing dependency for either.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT_TEXT = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
PACKAGE_ROOT = REPO_ROOT / "flow_doctor"

#: The floor may be RAISED here as coverage improves. Lowering it is a policy
#: amendment (repository-baseline-policy.md §4.2 C3), not a code change.
MINIMUM_FLOOR = 84


def _section(name: str) -> str:
    """Return the raw text of a ``[name]`` TOML table, up to the next ``[``."""
    match = re.search(
        rf"^\[{re.escape(name)}\]\n(.*?)(?=^\[|\Z)",
        PYPROJECT_TEXT,
        re.MULTILINE | re.DOTALL,
    )
    assert match, f"pyproject.toml has no [{name}] table"
    return match.group(1)


def test_coverage_source_is_the_whole_package() -> None:
    """C1 — ``[tool.coverage.run] source`` names the package, so unimported
    modules still count, and CI's ``coverage run -m pytest`` (not pytest-cov)
    measures against the same config."""
    section = _section("tool.coverage.run")
    match = re.search(r'source\s*=\s*\[\s*"([^"]+)"\s*\]', section)
    assert match and match.group(1) == "flow_doctor", (
        f"coverage source must be exactly the flow_doctor package, got "
        f"{match.group(1) if match else None!r}. Narrowing it to a submodule "
        "or a path measures the tested subset and reports it as the repository."
    )


def test_coverage_floor_is_enforced_and_never_lowered() -> None:
    """C2 + C3 — ``coverage report`` (invoked with no ``--fail-under`` flag in
    ci.yml) reads ``fail_under`` from this config automatically and exits
    non-zero below it. The floor only ratchets up."""
    section = _section("tool.coverage.report")
    match = re.search(r"fail_under\s*=\s*(\d+)", section)
    assert match, "[tool.coverage.report] must set fail_under"
    fail_under = int(match.group(1))
    assert fail_under >= MINIMUM_FLOOR, (
        f"coverage floor {fail_under} is below the ratchet {MINIMUM_FLOOR}. "
        "A floor is raised as coverage improves and never lowered to make a "
        "change pass (repository-baseline-policy.md §4.2 C3)."
    )


def test_omit_does_not_shrink_the_measured_package() -> None:
    """A shrunk denominator is a narrowing that neither flag would reveal.

    ``[tool.coverage.run] omit`` here only excludes ``tests/*`` and
    ``*/__pycache__/*`` — paths that were never inside ``source =
    ["flow_doctor"]`` (tests) or never real source (bytecode cache) to begin
    with. This test fails the moment an omit pattern would match anything
    that actually lives under the measured ``flow_doctor`` package.
    """
    section = _section("tool.coverage.run")
    omit_match = re.search(r"omit\s*=\s*\[(.*?)\]", section, re.DOTALL)
    omit = re.findall(r'"([^"]+)"', omit_match.group(1)) if omit_match else []
    package_files = {
        p.relative_to(REPO_ROOT).as_posix() for p in PACKAGE_ROOT.rglob("*.py")
    }
    for pattern in omit:
        matched = {f for f in package_files if fnmatch.fnmatch(f, pattern)}
        assert not matched, (
            f"omit pattern {pattern!r} excludes real package source "
            f"({sorted(matched)}) from the coverage denominator — this "
            "narrows the gate's scope without lowering fail_under, which "
            "reads as an improvement in every report."
        )


def test_every_source_module_is_inside_the_measured_package() -> None:
    """No shipped source file lives outside what ``source = ["flow_doctor"]``
    measures. ``[tool.setuptools.packages.find] include`` governs what ships;
    it must match the coverage source."""
    section = _section("tool.setuptools.packages.find")
    match = re.search(r'include\s*=\s*\[\s*"([^"]+)"\s*\]', section)
    assert match and match.group(1) == "flow_doctor*", (
        f"packaged include {match.group(1) if match else None!r} diverges "
        "from the coverage-measured package (flow_doctor) — a module could "
        "ship without being measured."
    )
