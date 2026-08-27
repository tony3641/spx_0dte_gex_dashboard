"""
Minimal .env persistence for the repo-root .env file.

Written by the settings endpoints, read back by python-dotenv at config
import. Updated keys are rewritten in place; all other lines (comments
included) are preserved verbatim. Writes are atomic (temp file + os.replace).
"""
import os
from pathlib import Path

DOTENV_PATH = Path(__file__).parent / ".env"


def read_env(path: Path = None) -> dict:
    """Parse KEY=VALUE lines; strip matching quotes; skip comments/blanks."""
    path = path or DOTENV_PATH
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, val = stripped.partition("=")
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        if key:
            out[key] = val
    return out


def update_env(updates: dict, path: Path = None) -> None:
    """Set each key in `updates`, preserving all other lines verbatim.

    Empty-string values are written as `KEY=` (unset downstream).
    """
    path = path or DOTENV_PATH
    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    pending = dict(updates)
    out = []
    for line in existing:
        stripped = line.strip()
        key = None
        if "=" in stripped and not stripped.startswith("#"):
            key = stripped.partition("=")[0].strip()
        if key in pending:
            out.append(f"{key}={pending.pop(key)}")
        else:
            out.append(line)
    for key, val in pending.items():
        out.append(f"{key}={val}")
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(out) + ("\n" if out else ""), encoding="utf-8")
    os.replace(tmp, path)
