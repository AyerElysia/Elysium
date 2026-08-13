"""Load git-ignored ``KEY=VALUE`` env files into the process environment.

Deployment-local secrets such as ``ELYSIUM_APP_API_V1_SIGNING_SECRET`` live
in ``runtime/app_api_v1_env.local`` (git-ignored).  The desktop start script
used to be the only injector, so launching ``main.py`` from an IDE or a plain
terminal silently lost them.  Loading here, at the very front of the startup
chain, makes every launch path safe.
"""

from __future__ import annotations

import os
from pathlib import Path


def load_local_env(path: str | Path) -> tuple[str, ...]:
    """Inject ``KEY=VALUE`` pairs from a local env file into ``os.environ``.

    Rules:
    - Blank lines and lines starting with ``#`` are skipped.
    - A trailing carriage return (CRLF files) is stripped from the value.
    - Existing environment variables are never overwritten, so an explicit
      shell export always wins over the local file.
    - A missing file is a no-op; subsystems that do not need the secrets can
      run without it.

    Returns the names of the variables that were actually injected.  Values
    are never returned or logged.
    """
    target = Path(path)
    if not target.is_file():
        return ()
    injected: list[str] = []
    for raw in target.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = value.rstrip("\r")
        injected.append(key)
    return tuple(injected)


__all__ = ["load_local_env"]
