"""Shared deterministic hashing and no-follow filesystem snapshot primitives."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path
from typing import Any


class SnapshotError(OSError):
    """Raised when a path cannot provide one stable regular-file snapshot."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def is_link_like(path: Path) -> bool:
    """Recognize symlinks and Windows reparse points without following them."""
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise SnapshotError(f"cannot inspect path without following links {path}: {exc}") from exc
    link_tags = {
        getattr(stat, "IO_REPARSE_TAG_SYMLINK", -1),
        getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", -2),
    }
    return stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_reparse_tag", 0) in link_tags


def read_regular_file_snapshot(path: Path, *, subject: str) -> tuple[bytes, os.stat_result]:
    """Read one no-follow snapshot and verify the pathname still names it."""
    try:
        if is_link_like(path):
            raise SnapshotError(f"{subject} must not be link-like: {path}")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
    except OSError as exc:
        if isinstance(exc, SnapshotError):
            raise
        raise SnapshotError(f"cannot open {subject} snapshot {path}: {exc}") from exc
    try:
        metadata_before = os.fstat(descriptor)
        if not stat.S_ISREG(metadata_before.st_mode):
            raise SnapshotError(f"{subject} must be a regular file: {path}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        current = os.stat(path, follow_symlinks=False)
        metadata_after = os.fstat(descriptor)
        fingerprint_before = (
            metadata_before.st_mode,
            metadata_before.st_size,
            metadata_before.st_mtime_ns,
            metadata_before.st_ctime_ns,
        )
        fingerprint_after = (
            metadata_after.st_mode,
            metadata_after.st_size,
            metadata_after.st_mtime_ns,
            metadata_after.st_ctime_ns,
        )
        if (
            not stat.S_ISREG(current.st_mode)
            or not os.path.samestat(metadata_before, current)
            or not os.path.samestat(metadata_before, metadata_after)
            or fingerprint_before != fingerprint_after
        ):
            raise SnapshotError(f"{subject} changed during snapshot: {path}")
        return b"".join(chunks), metadata_after
    except OSError as exc:
        if isinstance(exc, SnapshotError):
            raise
        raise SnapshotError(f"{subject} changed during snapshot: {path}") from exc
    finally:
        os.close(descriptor)


def git_environment() -> dict[str, str]:
    """Return an offline, read-only Git plumbing environment without ambient routing."""
    environment = {
        key: value for key, value in os.environ.items()
        if not key.upper().startswith("GIT_")
    }
    environment.update({
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
    })
    return environment


def _git_top_candidate(root: Path) -> Path:
    """Find the nearest lexical ancestor carrying a non-link-like .git marker."""
    for candidate in (root, *root.parents):
        marker = candidate / ".git"
        try:
            metadata = marker.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise SnapshotError(f"cannot inspect local Git marker {marker}: {exc}") from exc
        if is_link_like(marker):
            raise SnapshotError(f"local Git marker must not be link-like: {marker}")
        if stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode):
            return candidate
        raise SnapshotError(f"local Git marker must be a file or directory: {marker}")
    raise SnapshotError("cannot find a containing local Git repository")


def git_command(root: Path, *, safe_root: Path | None = None) -> list[str]:
    safe_root = root if safe_root is None else safe_root
    return ["git", "-c", f"safe.directory={safe_root}", "-C", str(root)]


def git_repository_identity(
    root: Path, *, require_clean_index: bool, require_toplevel: bool = True,
) -> str:
    """Resolve a containing repository and optionally require root to be its top level."""
    environment = git_environment()
    git_top = _git_top_candidate(root)
    command = git_command(root, safe_root=git_top)
    try:
        top = subprocess.run(
            [*command, "rev-parse", "--show-toplevel"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=environment,
        ).stdout.decode("utf-8").strip()
        commit = subprocess.run(
            [*command, "rev-parse", "--verify", "HEAD^{commit}"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=environment,
        ).stdout.decode("ascii").strip()
        staged = None
        if require_clean_index:
            staged = subprocess.run(
                [*command, "diff-index", "--cached", "--quiet", commit, "--"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment,
            )
    except (OSError, subprocess.CalledProcessError, UnicodeError) as exc:
        detail = (
            exc.stderr.decode("utf-8", errors="replace").strip()
            if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        )
        raise SnapshotError(f"cannot resolve the local Git repository: {detail}") from exc
    try:
        same_candidate = os.path.samefile(git_top, Path(top))
        same_root = os.path.samefile(root, Path(top))
    except OSError as exc:
        raise SnapshotError(f"cannot compare Git top-level identity: {exc}") from exc
    if not same_candidate:
        raise SnapshotError("Git top level does not match the nearest local .git marker")
    if require_toplevel and not same_root:
        raise SnapshotError("source root must be the local Git top-level directory")
    if staged is not None and staged.returncode == 1:
        raise SnapshotError("Git index differs from HEAD; commit or unstage it first")
    if staged is not None and staged.returncode != 0:
        raise SnapshotError(
            "cannot compare Git index with HEAD: "
            + staged.stderr.decode("utf-8", errors="replace").strip()
        )
    if not all(character in "0123456789abcdef" for character in commit) or len(commit) not in {40, 64}:
        raise SnapshotError("Git returned a non-canonical commit identifier")
    return commit
