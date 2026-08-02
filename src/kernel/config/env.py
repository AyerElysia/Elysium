"""Environment-variable expansion shared by all configuration loaders."""

from __future__ import annotations

import os
import re
from typing import Any


_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def interpolate_env(value: Any) -> Any:
    """Recursively expand ``${VAR}``, preserving placeholders that are unset."""
    if isinstance(value, str):
        return _ENV_PATTERN.sub(
            lambda match: os.environ.get(match.group(1), match.group(0)),
            value,
        )
    if isinstance(value, dict):
        return {key: interpolate_env(item) for key, item in value.items()}
    if isinstance(value, list):
        return [interpolate_env(item) for item in value]
    return value


__all__ = ["interpolate_env"]
