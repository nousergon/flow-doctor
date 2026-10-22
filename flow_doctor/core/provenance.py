"""Which flow-doctor is this process actually running, and where did it come from?

A box builds its venv fresh on every run from a requirements file that pins
flow-doctor only transitively (``nousergon-lib`` -> ``krepis[flow_doctor]`` ->
``flow-doctor>=X``), and that install step is quiet. So the only place a box
log can prove which flow-doctor ran is flow-doctor's own startup line
(alpha-engine-config-I7930). ``runtime_provenance()`` supplies what that line
prints: the running module's version, the installed distribution's version,
how it was installed, and the directory the code was imported from.

Everything here is read-only and never raises: a provenance probe that could
break ``FlowDoctor.__init__`` would be worse than no probe.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

DIST_NAME = "flow-doctor"


def _install_source(dist: Any) -> str:
    """Describe how the distribution was installed, from PEP 610 ``direct_url.json``.

    pip writes ``direct_url.json`` only for direct-URL installs (a VCS ref, a
    local directory, an archive URL). Its absence means the wheel came from a
    package index, which for this fleet is PyPI.
    """
    try:
        raw = dist.read_text("direct_url.json")
    except Exception:
        raw = None
    if not raw:
        return "index"
    try:
        info = json.loads(raw)
    except ValueError:
        return "direct-url(unparseable)"
    url = str(info.get("url", "?"))
    vcs = info.get("vcs_info")
    if isinstance(vcs, dict):
        commit = str(vcs.get("commit_id", "?"))[:12]
        return f"{vcs.get('vcs', 'vcs')}+{url}@{commit}"
    dir_info = info.get("dir_info")
    if isinstance(dir_info, dict):
        return f"editable:{url}" if dir_info.get("editable") else f"dir:{url}"
    return f"archive:{url}"


def runtime_provenance() -> Dict[str, Optional[str]]:
    """Return ``version``, ``dist_version``, ``source`` and ``path`` for this process.

    - ``version``: ``flow_doctor.__version__``, the code that is running.
    - ``dist_version``: the installed ``flow-doctor`` distribution's metadata
      version, or ``None`` when no distribution is installed (a bare source
      checkout on ``sys.path``).
    - ``source``: ``index``, a VCS/dir/archive URL, or ``not-installed``.
    - ``path``: the directory ``flow_doctor`` was imported from.

    ``version`` and ``dist_version`` disagree only when the imported code is
    not the installed distribution (a source tree shadowing a venv install),
    which is exactly the case a version line exists to expose.
    """
    import flow_doctor

    result: Dict[str, Optional[str]] = {
        "version": getattr(flow_doctor, "__version__", None),
        "dist_version": None,
        "source": "not-installed",
        "path": None,
    }
    try:
        result["path"] = os.path.dirname(os.path.abspath(flow_doctor.__file__))
    except Exception:
        pass
    try:
        from importlib import metadata

        dist = metadata.distribution(DIST_NAME)
    except Exception:
        return result
    try:
        result["dist_version"] = dist.version
    except Exception:
        pass
    result["source"] = _install_source(dist)
    return result


def format_provenance(prov: Dict[str, Optional[str]]) -> str:
    """Render ``runtime_provenance()`` as the startup line's components."""
    version = prov.get("version") or "unknown"
    dist_version = prov.get("dist_version")
    if dist_version and dist_version != version:
        version = f"{version}(installed-dist={dist_version},MISMATCH)"
    return f"version={version}, source={prov.get('source')}, path={prov.get('path')}"
