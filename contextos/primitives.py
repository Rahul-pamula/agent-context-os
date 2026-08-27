"""Shared deterministic hashing and no-follow filesystem snapshot primitives."""

from __future__ import annotations

import hashlib
import json
import os
import stat
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
