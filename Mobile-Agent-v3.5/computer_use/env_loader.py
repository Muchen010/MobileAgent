"""
Lightweight .env file loader (no external dependency).

Searches for a .env file starting from this script's directory and walking
upward through parent directories, then loads KEY=VALUE pairs into os.environ
without overriding values that are already set (so real shell env vars and CLI
args still take precedence).
"""

import os


def load_env_file(env_file: str = ".env", max_levels: int = 5):
    """
    Walk upward from this file's directory looking for *env_file*.
    When found, parse it and populate os.environ.

    Returns:
        The absolute path of the loaded file, or None if nothing was found.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    for _ in range(max_levels):
        candidate = os.path.join(here, env_file)
        if os.path.isfile(candidate):
            _populate_env(candidate)
            return candidate
        parent = os.path.dirname(here)
        if parent == here:
            break
        here = parent
    return None


def _populate_env(path: str) -> None:
    """Parse a .env file and set keys in os.environ (without overwriting)."""
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            # Strip surrounding quotes if present
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            if key and key not in os.environ:
                os.environ[key] = value
