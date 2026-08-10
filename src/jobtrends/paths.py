"""Central resolution of runtime filesystem locations.

The Python package lives under ``src/jobtrends/`` but the project's runtime
data (the SQLite database, the ``data/`` output directory, and the tracked
``config/ats_companies.json``) lives at the repository root. Anchoring those
paths to each module's ``__file__`` would break once the modules moved into the
package, so every path is resolved here relative to a single ``PROJECT_ROOT``.

All locations can be overridden with environment variables, which keeps the
defaults convenient for local development while allowing a deployment (e.g.
Render, with a mounted disk) to point them elsewhere.
"""

from __future__ import annotations

import os
from pathlib import Path


def _find_project_root() -> Path:
    """Locate the repository root.

    Prefers an explicit ``JOBTRENDS_ROOT`` override, otherwise walks up from
    this file looking for the ``pyproject.toml`` marker. Falls back to the
    directory two levels above ``src/jobtrends/`` if no marker is found.
    """
    override = os.environ.get("JOBTRENDS_ROOT")
    if override:
        return Path(override).expanduser().resolve()

    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").is_file():
            return parent
    # src/jobtrends/paths.py -> src/jobtrends -> src -> <root>
    return here.parents[2]


PROJECT_ROOT = _find_project_root()

DATA_DIR = Path(
    os.environ.get("JOBTRENDS_DATA_DIR", PROJECT_ROOT / "data")
).expanduser()

CONFIG_DIR = Path(
    os.environ.get("JOBTRENDS_CONFIG_DIR", PROJECT_ROOT / "config")
).expanduser()

DEFAULT_DB_PATH = Path(
    os.environ.get("JOB_TRENDS_DB", PROJECT_ROOT / "job_trends.db")
).expanduser()

DEFAULT_COMPANIES_FILE = Path(
    os.environ.get("ATS_COMPANIES_FILE", CONFIG_DIR / "ats_companies.json")
).expanduser()
