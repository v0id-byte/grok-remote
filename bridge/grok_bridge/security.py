"""Path validation.

`validate_cwd` gates which directory a session may be created in: it must
resolve under one of ALLOWED_ROOTS and must not fall under a denied sensitive
subpath. See config.py for why that is a denylist of specific directories
rather than a blanket dotfile ban.

`validate_path_in_cwd` gates the fs/read_text_file and fs/write_text_file
requests the ACP agent makes back at us, confining them to one session's cwd.
Both resolve symlinks before deciding, because lexical checks are not enough:
a symlink at `project/foo -> ~/.ssh` makes `project/foo/config` look like it is
inside the workspace while pointing straight out of it.
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


def validate_path_in_cwd(raw_path: str, cwd: Path) -> Path:
    """Confine an agent-supplied path to one session's working directory.

    Called for every fs/* request the agent sends back to us. Symlinks are the
    interesting case: `project/foo -> ~/.ssh` makes `project/foo/config` pass any
    lexical containment check while resolving outside the workspace entirely, so
    containment is decided on the fully resolved path.

    Writes may legitimately target a file that does not exist yet, so when the
    path itself cannot be resolved the nearest existing ancestor is checked
    instead -- that ancestor is what a symlink would have to subvert.
    """
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = cwd / candidate

    try:
        resolved = candidate.resolve(strict=True)
        anchor = resolved
    except OSError:
        # Not created yet: resolve as far as it exists and check that.
        resolved = candidate.resolve(strict=False)
        anchor = resolved
        while not anchor.exists() and anchor != anchor.parent:
            anchor = anchor.parent
        anchor = anchor.resolve(strict=False)

    cwd_resolved = cwd.resolve(strict=False)
    if not (_is_relative_to(resolved, cwd_resolved) and _is_relative_to(anchor, cwd_resolved)):
        raise InvalidCwd(f"path escapes the session directory: {raw_path}")

    for part in resolved.parts:
        if part in config.DENIED_SUBPATH_NAMES:
            raise InvalidCwd(f"denied path component '{part}': {raw_path}")

    return resolved
