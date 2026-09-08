"""Directory browsing and git-repository discovery for the project picker.

Replaces typing an absolute path into a text field.

Two things here are shaped by measurement rather than convenience:

  * Repo discovery is cached and shallow. A depth-3 scan of four directories on
    this machine finds 51 repositories and takes ~2.1 seconds; a live scan on
    every request would make the picker feel broken.
  * `.git` is not always a directory. In a git worktree it is a *file* holding
    `gitdir: /path/to/real/.git/worktrees/<name>`, and three of those 51 repos
    are exactly that. Reading `.git/HEAD` blindly fails on them, and this
    project has a /worktree command in its own roadmap.

Git status is deliberately NOT part of discovery: branch, dirty count and last
commit each cost a subprocess, so the picker lists repositories immediately and
enriches only the rows a client actually asks about.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any

from . import config

_SKIP_DIRS = frozenset({
    "node_modules", ".venv", "venv", "__pycache__", ".next", "dist", "build",
    "DerivedData", ".build", "target", "Pods", ".tox", ".mypy_cache",
})

_REPO_CACHE: dict[str, Any] = {"at": 0.0, "repos": []}
REPO_CACHE_TTL = 60.0
REPO_SCAN_MAX_DEPTH = 3


def _is_denied(path: Path) -> bool:
    for denied in config.DENIED_ABSOLUTE_PATHS:
        try:
            path.relative_to(denied)
            return True
        except ValueError:
            pass
    return any(part in config.DENIED_SUBPATH_NAMES for part in path.parts)


def _in_allowed_roots(path: Path) -> bool:
    for root in config.ALLOWED_ROOTS:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            pass
    return False


def is_repo(path: Path) -> bool:
    # Exists-not-is_dir: a worktree's .git is a file.
    return (path / ".git").exists()


def list_dir(raw_path: str) -> dict[str, Any]:
    """One level of the filesystem, for the browser tab.

    Denied entries are listed and flagged rather than hidden, so the UI can grey
    them out -- a directory silently missing is much harder to reason about than
    one shown as off-limits.
    """
    path = Path(raw_path).expanduser()
    try:
        path = path.resolve(strict=True)
    except OSError as e:
        raise FileNotFoundError(f"no such directory: {raw_path}") from e
    if not path.is_dir():
        raise NotADirectoryError(f"not a directory: {raw_path}")
    if not _in_allowed_roots(path) or _is_denied(path):
        raise PermissionError(f"outside the allowed roots: {raw_path}")

    entries: list[dict[str, Any]] = []
    with_errors = False
    try:
        for entry in path.iterdir():
            try:
                # follow_symlinks=False: a symlinked directory should not be
                # descended into as though it were part of this tree.
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            entries.append({
                "name": entry.name,
                "path": str(entry),
                "isRepo": is_repo(entry),
                "isDenied": _is_denied(entry),
                "isHidden": entry.name.startswith("."),
            })
    except PermissionError:
        with_errors = True

    entries.sort(key=lambda e: (e["isHidden"], e["name"].lower()))
    parent = str(path.parent) if _in_allowed_roots(path.parent) and path.parent != path else None
    return {"path": str(path), "parent": parent, "entries": entries,
            "partial": with_errors}


def find_repos(refresh: bool = False) -> list[dict[str, Any]]:
    """Shallow, cached scan of the allowed roots for git repositories."""
    now = time.time()
    if not refresh and now - _REPO_CACHE["at"] < REPO_CACHE_TTL and _REPO_CACHE["repos"]:
        return _REPO_CACHE["repos"]

    found: list[dict[str, Any]] = []
    seen: set[str] = set()

    def walk(directory: Path, depth: int) -> None:
        if depth > REPO_SCAN_MAX_DEPTH or _is_denied(directory):
            return
        try:
            children = list(directory.iterdir())
        except OSError:
            return
        if is_repo(directory) and str(directory) not in seen:
            seen.add(str(directory))
            found.append({
                "path": str(directory),
                "name": directory.name,
                "isWorktree": (directory / ".git").is_file(),
            })
            # Do not descend into a repository: its subdirectories are its
            # contents, not more projects.
            return
        for child in children:
            try:
                if child.is_dir(follow_symlinks=False) and child.name not in _SKIP_DIRS:
                    walk(child, depth + 1)
            except OSError:
                continue

    for root in config.ALLOWED_ROOTS:
        walk(Path(root).expanduser(), 0)

    found.sort(key=lambda r: r["name"].lower())
    _REPO_CACHE.update({"at": now, "repos": found})
    return found


def _git_dir(repo: Path) -> Path | None:
    """Resolve a repo's real git directory, handling the worktree file form."""
    marker = repo / ".git"
    if marker.is_dir():
        return marker
    if marker.is_file():
        try:
            text = marker.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return None
        if text.startswith("gitdir:"):
            target = Path(text.split(":", 1)[1].strip()).expanduser()
            if not target.is_absolute():
                target = (repo / target).resolve()
            return target if target.exists() else None
    return None


def head_branch(repo: Path) -> str | None:
    """Current branch, read from HEAD without invoking git.

    Cheap enough to do during discovery if ever needed, and correct for
    worktrees, where HEAD lives in the linked git dir rather than in `.git/`.
    """
    git_dir = _git_dir(repo)
    if git_dir is None:
        return None
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if head.startswith("ref: refs/heads/"):
        return head.removeprefix("ref: refs/heads/")
    return head[:12] or None          # detached HEAD: show the short sha


def repo_status(paths: list[str], timeout: float = 3.0) -> dict[str, Any]:
    """Branch, dirty count and last-commit time for specific repositories.

    Kept out of find_repos on purpose: each of these is a subprocess, so they
    run only for the rows a client is actually showing.
    """
    out: dict[str, Any] = {}
    for raw in paths:
        repo = Path(raw).expanduser()
        if not _in_allowed_roots(repo) or _is_denied(repo) or not is_repo(repo):
            continue
        status: dict[str, Any] = {
            "branch": head_branch(repo),
            "isWorktree": (repo / ".git").is_file(),
        }
        try:
            dirty = subprocess.run(
                ["git", "-C", str(repo), "status", "--porcelain"],
                capture_output=True, text=True, timeout=timeout,
            )
            if dirty.returncode == 0:
                status["dirtyCount"] = len([l for l in dirty.stdout.splitlines() if l.strip()])
            last = subprocess.run(
                ["git", "-C", str(repo), "log", "-1", "--format=%ct"],
                capture_output=True, text=True, timeout=timeout,
            )
            if last.returncode == 0 and last.stdout.strip():
                status["lastCommitAt"] = int(last.stdout.strip())
        except subprocess.TimeoutExpired:
            # Not hypothetical: `git status` in a worktree on this machine hangs
            # indefinitely. Say so rather than returning a row that merely looks
            # like a clean repo with no commits.
            status["statusUnavailable"] = "timeout"
        except (OSError, ValueError):
            status["statusUnavailable"] = "error"
        out[str(repo)] = status
    return out
