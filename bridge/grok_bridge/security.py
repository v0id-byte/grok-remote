"""cwd validation for new sessions.

Two layers: cwd must resolve under one of ALLOWED_ROOTS, and must not fall
under any denied sensitive subpath. See config.py for why this is a denylist
of specific sensitive directories rather than a blanket dotfile ban.
"""

from __future__ import annotations

from pathlib import Path

from . import config


class InvalidCwd(ValueError):
    pass


def validate_cwd(raw_path: str) -> Path:
    try:
        resolved = Path(raw_path).expanduser().resolve(strict=True)
    except OSError as e:
        raise InvalidCwd(f"path does not exist: {raw_path}") from e

    if not resolved.is_dir():
        raise InvalidCwd(f"not a directory: {raw_path}")

    if not any(_is_relative_to(resolved, root) for root in config.ALLOWED_ROOTS):
        raise InvalidCwd(f"outside allowed roots: {raw_path}")

    for denied in config.DENIED_ABSOLUTE_PATHS:
        if _is_relative_to(resolved, denied):
            raise InvalidCwd(f"denied path: {raw_path}")

    for part in resolved.parts:
        if part in config.DENIED_SUBPATH_NAMES:
            raise InvalidCwd(f"denied path component '{part}': {raw_path}")

    return resolved


def _is_relative_to(path: Path, other: Path) -> bool:
    try:
        path.relative_to(other)
        return True
    except ValueError:
        return False
