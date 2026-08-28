"""Immutable offline bundle locks and read-only structural composition plans."""

from __future__ import annotations

import json
import os
import re
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Sequence

from .component_schema import (
    COMPONENT_MANIFEST_SCHEMA_VERSION,
    ComponentManifestError,
    PATH_POLICIES,
    component_closure,
    portable_path_identity,
    resolved_component_paths,
    unclassified_tracked_paths,
    untracked_owned_paths,
    validate_component_manifest,
)
from .runtime_schema import RUNTIME_DESCRIPTOR_SCHEMA_VERSION
from .runtime_schema import RuntimeManifestError, validate_runtime_manifest
from .primitives import (
    SnapshotError,
    canonical_json,
    git_command,
    git_environment,
    git_repository_identity,
    is_link_like,
    read_regular_file_snapshot,
    sha256_bytes,
)
from .workspace_schema import (
    WORKSPACE_SCHEMA_VERSION,
    WorkspaceConfigError,
    render_workspace_config,
    strict_json_loads,
    validate_workspace_config,
    validate_workspace_path,
)


BUNDLE_LOCK_SCHEMA_VERSION = 1
PLANNER_PROTOCOL_VERSION = 1
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{40,64}$")
BUNDLE_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")
LOCK_KEYS = {"schema_version", "bundle", "bundle_sha256"}
BUNDLE_KEYS = {
    "name", "version", "source_git_commit", "compatibility",
    "component_manifest_path", "files",
}
COMPATIBILITY_KEYS = {
    "component_manifest_schema", "runtime_descriptor_schema",
    "workspace_schema", "planner_protocol",
}
FILE_KEYS = {"path", "sha256_raw", "sha256_text_lf", "size", "executable"}


class BundleError(ValueError):
    """Raised when a lock, offline source, or structural plan is unsafe."""


@dataclass(frozen=True)
class VerifiedBundle:
    root: Path
    lock_path: Path
    source_mode: str
    role: str
    mode_verified: bool
    lock: dict[str, Any]
    manifest: dict[str, Any]
    runtimes: dict[str, dict[str, Any]]
    records: dict[str, dict[str, Any]]

    @property
    def digest(self) -> str:
        return self.lock["bundle_sha256"]

    @property
    def name(self) -> str:
        return self.lock["bundle"]["name"]

    @property
    def version(self) -> str:
        return self.lock["bundle"]["version"]


def _fail(field: str, message: str) -> None:
    raise BundleError(f"{field}: {message}")


def _exact_keys(value: Any, expected: set[str], field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(field, "must be an object")
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unknown:
            details.append("unknown " + ", ".join(unknown))
        _fail(field, "; ".join(details))
    return value


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        _fail(field, "must be a non-empty string without surrounding whitespace")
    if any(
        unicodedata.category(character) in {"Cc", "Cf", "Zl", "Zp"}
        for character in value
    ):
        _fail(field, "must not contain control or format characters")
    return value


def _sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        _fail(field, "must be a lowercase SHA-256 digest")
    return value


def _validate_manifest(value: Any, *, root: Path) -> dict[str, Any]:
    try:
        return validate_component_manifest(value, root=root, check_paths=False)
    except ComponentManifestError as exc:
        raise BundleError(str(exc)) from exc


def _component_closure(manifest: Any, component_ids: Sequence[str]) -> list[str]:
    try:
        return component_closure(manifest, component_ids)
    except ComponentManifestError as exc:
        raise BundleError(str(exc)) from exc


def _is_link_like(path: Path) -> bool:
    try:
        return is_link_like(path)
    except SnapshotError as exc:
        raise BundleError(str(exc)) from exc


def _source_root(root: Path, field: str) -> Path:
    if not isinstance(root, Path):
        _fail(field, "must be an explicit local Path")
    absolute = root.absolute()
    if _is_link_like(absolute):
        _fail(field, "must not be link-like")
    try:
        metadata = absolute.lstat()
    except OSError as exc:
        _fail(field, f"cannot inspect local directory: {exc}")
    if not stat.S_ISDIR(metadata.st_mode):
        _fail(field, "must be an existing local directory")
    try:
        return absolute.resolve()
    except OSError as exc:
        _fail(field, f"cannot resolve local directory identity: {exc}")


def _safe_path(root: Path, relative: str, field: str, *, missing_ok: bool) -> Path:
    try:
        relative = validate_workspace_path(relative, field)
    except WorkspaceConfigError as exc:
        raise BundleError(str(exc)) from exc
    current = root
    for index, part in enumerate(PurePosixPath(relative).parts):
        if current.exists():
            if _is_link_like(current):
                _fail(field, f"must not traverse link-like path {current}")
            try:
                matches = [
                    entry.name for entry in current.iterdir()
                    if portable_path_identity(entry.name) == portable_path_identity(part)
                ]
            except OSError as exc:
                _fail(field, f"cannot enumerate {current}: {exc}")
            if len(matches) > 1:
                _fail(field, f"portable path collision below {current}: {', '.join(sorted(matches))}")
            if matches and matches[0] != part:
                _fail(field, f"portable alias {matches[0]!r} collides with {part!r}")
        current /= part
        if _is_link_like(current):
            _fail(field, f"must not be or traverse a link-like path: {relative}")
        if not current.exists() and index < len(PurePosixPath(relative).parts) - 1:
            if missing_ok:
                continue
            _fail(field, f"missing ancestor for {relative}")
    if current.exists():
        metadata = current.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            _fail(field, f"must name a regular file: {relative}")
        if getattr(metadata, "st_nlink", 1) > 1:
            _fail(field, f"must not name a multiply linked file: {relative}")
    elif not missing_ok:
        _fail(field, f"path does not exist: {relative}")
    return current


def _read_snapshot(path: Path, field: str) -> tuple[bytes, os.stat_result]:
    try:
        return read_regular_file_snapshot(path, subject=field)
    except SnapshotError as exc:
        raise BundleError(str(exc)) from exc


def _record(path: str, data: bytes, executable: bool) -> dict[str, Any]:
    return {
        "path": path,
        "sha256_raw": sha256_bytes(data),
        "sha256_text_lf": _text_lf_digest(data),
        "size": len(data),
        "executable": executable,
    }


def _text_lf_digest(data: bytes) -> str | None:
    if b"\0" in data:
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
    return sha256_bytes(normalized)


def _bundle_name(value: Any, field: str) -> str:
    name = _text(value, field)
    if not BUNDLE_NAME_RE.fullmatch(name) or name in {"latest", "main", "master"}:
        _fail(field, "must be a stable lowercase bundle identifier")
    return name


def _bundle_version(value: Any, field: str) -> str:
    version = _text(value, field)
    if version.casefold() in {"latest", "main", "master", "head"} or "://" in version:
        _fail(field, "must be an exact offline version, not a channel or URL")
    return version


def _git_index(root: Path) -> dict[str, tuple[str, bool]]:
    """Return stage-zero blob IDs and executable bits from a local Git index."""
    import subprocess

    try:
        result = subprocess.run(
            [
                *git_command(root), "ls-files", "--stage", "-z", "--",
            ],
            env=git_environment(),
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = (
            exc.stderr.decode("utf-8", errors="replace").strip()
            if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        )
        _fail("source_mode", f"cannot read local Git index: {detail}")
    entries: dict[str, tuple[str, bool]] = {}
    for entry in result.stdout.split(b"\0"):
        if not entry:
            continue
        try:
            metadata, raw_path = entry.split(b"\t", 1)
            mode, oid, stage = metadata.split(b" ")
            path = raw_path.decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            _fail("source_mode", "Git index returned an invalid record")
        if stage != b"0":
            _fail("source_mode", f"Git index has unresolved stages for {path}")
        if mode not in {b"100644", b"100755"}:
            _fail(
                "source_mode",
                f"Git index path {path!r} has unsupported non-regular mode {mode.decode('ascii', errors='replace')}",
            )
        if path in entries:
            _fail("source_mode", f"Git index returned duplicate path {path!r}")
        entries[path] = (oid.decode("ascii"), mode == b"100755")
    return entries


def _git_blob(root: Path, oid: str, field: str) -> bytes:
    import subprocess

    try:
        result = subprocess.run(
            [*git_command(root), "cat-file", "blob", oid], check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=git_environment(),
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = (
            exc.stderr.decode("utf-8", errors="replace").strip()
            if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        )
        _fail(field, f"cannot read local Git blob: {detail}")
    return result.stdout


def _git_repository_identity(root: Path) -> str:
    try:
        return git_repository_identity(
            root, require_clean_index=True
        )
    except SnapshotError as exc:
        raise BundleError(f"source_mode: {exc}") from exc


def _source_entries(
    root: Path, paths: Iterable[str], *, source_mode: str,
) -> dict[str, tuple[bytes, bool]]:
    if source_mode not in {"directory", "git-index"}:
        _fail("source_mode", "must equal 'directory' or 'git-index'")
    result: dict[str, tuple[bytes, bool]] = {}
    if source_mode == "git-index":
        index = _git_index(root)
        for relative in paths:
            if relative not in index:
                _fail(f"source.{relative}", "is absent from the local Git index")
            oid, executable = index[relative]
            result[relative] = (_git_blob(root, oid, f"source.{relative}"), executable)
        return result
    for relative in paths:
        path = _safe_path(root, relative, f"source.{relative}", missing_ok=False)
        data, metadata = _read_snapshot(path, f"source.{relative}")
        result[relative] = (
            data, bool(stat.S_IMODE(metadata.st_mode) & 0o111),
        )
    return result


def _validated_runtimes(
    source_bytes: dict[str, bytes], manifest: dict[str, Any], *, root: Path,
) -> dict[str, dict[str, Any]]:
    runtimes: dict[str, dict[str, Any]] = {}
    paths = sorted(
        (
            path for path in source_bytes
            if len(PurePosixPath(path).parts) == 2
            and PurePosixPath(path).parts[0] == "runtimes"
            and path.endswith(".json")
            and path != "runtimes/schema.json"
        ),
        key=portable_path_identity,
    )
    for path in paths:
        runtime_id = PurePosixPath(path).stem
        try:
            value = strict_json_loads(
                source_bytes[path].decode("utf-8"), source=path
            )
            descriptor = validate_runtime_manifest(
                value, runtime_id=runtime_id, root=root, check_paths=False
            )
        except (UnicodeError, WorkspaceConfigError, RuntimeManifestError) as exc:
            raise BundleError(str(exc)) from exc
        _component_closure(manifest, descriptor["components"])
        runtimes[runtime_id] = descriptor
    return runtimes


def create_bundle_lock(
    root: Path, *, name: str, version: str, source_mode: str = "git-index",
) -> dict[str, Any]:
    """Build a deterministic detached lock from one strict maintainer source tree."""
    root = _source_root(root, "source_root")
    name = _bundle_name(name, "bundle.name")
    version = _bundle_version(version, "bundle.version")
    source_git_commit = (
        _git_repository_identity(root) if source_mode == "git-index" else None
    )
    manifest_relative = "components/manifest.json"
    source = _source_entries(root, [manifest_relative], source_mode=source_mode)
    try:
        manifest_value = strict_json_loads(
            source[manifest_relative][0].decode("utf-8"), source=manifest_relative
        )
    except (UnicodeError, WorkspaceConfigError) as exc:
        raise BundleError(str(exc)) from exc
    manifest = _validate_manifest(manifest_value, root=root)
    if source_mode == "git-index":
        tracked = list(_git_index(root))
        unclassified = unclassified_tracked_paths(manifest, tracked, root=root)
        if unclassified:
            _fail("source_root", "unclassified Git index paths: " + ", ".join(unclassified[:20]))
        absent = untracked_owned_paths(manifest, tracked, root=root)
        if absent:
            _fail("source_root", "owned paths absent from Git index: " + ", ".join(absent[:20]))
    all_components = [item["id"] for item in manifest["components"]]
    paths = resolved_component_paths(manifest, all_components)
    source = _source_entries(
        root, (item["path"] for item in paths), source_mode=source_mode
    )
    _validated_runtimes(
        {path: data for path, (data, _) in source.items()}, manifest, root=root
    )
    files = []
    for item in paths:
        data, executable = source[item["path"]]
        files.append(_record(item["path"], data, executable))
    if source_mode == "git-index":
        final_commit = _git_repository_identity(root)
        if final_commit != source_git_commit:
            _fail("source_mode", "Git HEAD changed while the bundle lock was generated")
    bundle = {
        "name": name,
        "version": version,
        "source_git_commit": source_git_commit,
        "compatibility": {
            "component_manifest_schema": COMPONENT_MANIFEST_SCHEMA_VERSION,
            "runtime_descriptor_schema": RUNTIME_DESCRIPTOR_SCHEMA_VERSION,
            "workspace_schema": WORKSPACE_SCHEMA_VERSION,
            "planner_protocol": PLANNER_PROTOCOL_VERSION,
        },
        "component_manifest_path": "components/manifest.json",
        "files": sorted(files, key=lambda item: portable_path_identity(item["path"])),
    }
    return {
        "schema_version": BUNDLE_LOCK_SCHEMA_VERSION,
        "bundle": bundle,
        "bundle_sha256": sha256_bytes(canonical_json(bundle).encode("utf-8")),
    }


def validate_bundle_lock(value: Any) -> dict[str, Any]:
    document = _exact_keys(value, LOCK_KEYS, "lock")
    if type(document.get("schema_version")) is not int or document["schema_version"] != 1:
        _fail("schema_version", f"must equal integer {BUNDLE_LOCK_SCHEMA_VERSION}")
    bundle = _exact_keys(document.get("bundle"), BUNDLE_KEYS, "bundle")
    _bundle_name(bundle.get("name"), "bundle.name")
    _bundle_version(bundle.get("version"), "bundle.version")
    source_git_commit = bundle.get("source_git_commit")
    if source_git_commit is not None and (
        not isinstance(source_git_commit, str)
        or not GIT_COMMIT_RE.fullmatch(source_git_commit)
    ):
        _fail("bundle.source_git_commit", "must be null or a lowercase Git commit id")
    compatibility = _exact_keys(
        bundle.get("compatibility"), COMPATIBILITY_KEYS, "bundle.compatibility"
    )
    for key in COMPATIBILITY_KEYS:
        if type(compatibility.get(key)) is not int or compatibility[key] < 1:
            _fail(f"bundle.compatibility.{key}", "must be a positive integer")
    try:
        manifest_path = validate_workspace_path(
            bundle.get("component_manifest_path"), "bundle.component_manifest_path"
        )
    except WorkspaceConfigError as exc:
        raise BundleError(str(exc)) from exc
    if manifest_path != "components/manifest.json":
        _fail("bundle.component_manifest_path", "must equal 'components/manifest.json'")
    raw_files = bundle.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        _fail("bundle.files", "must be a non-empty array")
    identities: dict[str, str] = {}
    files: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_files):
        field = f"bundle.files[{index}]"
        item = _exact_keys(raw, FILE_KEYS, field)
        try:
            path = validate_workspace_path(item.get("path"), f"{field}.path")
        except WorkspaceConfigError as exc:
            raise BundleError(str(exc)) from exc
        identity = portable_path_identity(path)
        if identity in identities:
            _fail("bundle.files", f"portable path collision: {identities[identity]!r} and {path!r}")
        identities[identity] = path
        _sha256(item.get("sha256_raw"), f"{field}.sha256_raw")
        text_digest = item.get("sha256_text_lf")
        if text_digest is not None:
            _sha256(text_digest, f"{field}.sha256_text_lf")
        if type(item.get("size")) is not int or item["size"] < 0:
            _fail(f"{field}.size", "must be a non-negative integer")
        if type(item.get("executable")) is not bool:
            _fail(f"{field}.executable", "must be a boolean")
        files.append(item)
    ordered = sorted(files, key=lambda item: portable_path_identity(item["path"]))
    if files != ordered:
        _fail("bundle.files", "must use portable path order")
    identities_by_segment = sorted(
        portable_path_identity(item["path"]).split("/") for item in files
    )
    for left, right in zip(identities_by_segment, identities_by_segment[1:]):
        if len(left) < len(right) and right[:len(left)] == left:
            _fail("bundle.files", "must not contain a file/descendant path conflict")
    if manifest_path not in {item["path"] for item in files}:
        _fail("bundle.files", "must include the component manifest")
    expected_digest = sha256_bytes(canonical_json(bundle).encode("utf-8"))
    actual_digest = _sha256(document.get("bundle_sha256"), "bundle_sha256")
    if actual_digest != expected_digest:
        _fail("bundle_sha256", f"does not match bundle payload; expected {expected_digest}")
    return document


def load_bundle_lock(path: Path) -> dict[str, Any]:
    if _is_link_like(path):
        _fail("lock", "must not be link-like")
    try:
        raw = path.read_text(encoding="utf-8")
        value = strict_json_loads(raw, source=str(path))
    except (OSError, UnicodeError, WorkspaceConfigError) as exc:
        raise BundleError(str(exc)) from exc
    return validate_bundle_lock(value)


def _validate_compatibility(lock: dict[str, Any], role: str) -> None:
    if role not in {"candidate", "current"}:
        _fail("role", "must equal 'candidate' or 'current'")
    compatibility = lock["bundle"]["compatibility"]
    expected = {
        "component_manifest_schema": COMPONENT_MANIFEST_SCHEMA_VERSION,
        "runtime_descriptor_schema": RUNTIME_DESCRIPTOR_SCHEMA_VERSION,
        "workspace_schema": WORKSPACE_SCHEMA_VERSION,
        "planner_protocol": PLANNER_PROTOCOL_VERSION,
    }
    keys = expected if role == "candidate" else {
        "component_manifest_schema": COMPONENT_MANIFEST_SCHEMA_VERSION
    }
    for key, value in keys.items():
        if compatibility[key] != value:
            _fail(
                f"bundle.compatibility.{key}",
                f"unsupported for {role} bundle; expected {value}, got {compatibility[key]}",
            )


def verify_bundle(
    lock_path: Path, source_root: Path, *, expected_sha256: str,
    source_mode: str = "directory", role: str = "candidate",
) -> VerifiedBundle:
    """Verify one caller-pinned lock and all of its local bytes without fetching."""
    expected_sha256 = _sha256(expected_sha256, "expected_sha256")
    lock_path = lock_path.absolute()
    lock = load_bundle_lock(lock_path)
    _validate_compatibility(lock, role)
    if lock["bundle_sha256"] != expected_sha256:
        _fail("expected_sha256", "does not match the supplied bundle lock")
    root = _source_root(source_root, "source_root")
    if source_mode == "git-index":
        locked_commit = lock["bundle"]["source_git_commit"]
        if locked_commit is None:
            _fail("bundle.source_git_commit", "is required for Git-index verification")
        actual_commit = _git_repository_identity(root)
        if actual_commit != locked_commit:
            _fail("bundle.source_git_commit", "does not match the local Git HEAD commit")
    records = {item["path"]: item for item in lock["bundle"]["files"]}
    source = _source_entries(root, records, source_mode=source_mode)
    verified_bytes: dict[str, bytes] = {}
    for relative, record in records.items():
        data, executable = source[relative]
        if len(data) != record["size"] or sha256_bytes(data) != record["sha256_raw"]:
            _fail(f"source.{relative}", "raw bytes do not match the bundle lock")
        if _text_lf_digest(data) != record["sha256_text_lf"]:
            _fail(f"source.{relative}", "text normalization does not match the bundle lock")
        if (source_mode == "git-index" or os.name != "nt") and executable != record["executable"]:
            _fail(f"source.{relative}", "executable mode does not match the bundle lock")
        verified_bytes[relative] = data
    manifest_relative = lock["bundle"]["component_manifest_path"]
    try:
        manifest_value = strict_json_loads(
            verified_bytes[manifest_relative].decode("utf-8"), source=manifest_relative
        )
        manifest = _validate_manifest(manifest_value, root=root)
    except (UnicodeError, WorkspaceConfigError, BundleError) as exc:
        raise BundleError(str(exc)) from exc
    all_components = [item["id"] for item in manifest["components"]]
    expected_paths = {
        item["path"] for item in resolved_component_paths(manifest, all_components)
    }
    if set(records) != expected_paths:
        missing = sorted(expected_paths - set(records), key=portable_path_identity)
        extra = sorted(set(records) - expected_paths, key=portable_path_identity)
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if extra:
            details.append("extra " + ", ".join(extra))
        _fail("bundle.files", "does not equal the non-development component inventory: " + "; ".join(details))
    runtimes = (
        _validated_runtimes(verified_bytes, manifest, root=root)
        if role == "candidate" else {}
    )
    return VerifiedBundle(
        root=root, lock_path=lock_path, source_mode=source_mode, role=role,
        mode_verified=(source_mode == "git-index" or os.name != "nt"), lock=lock,
        manifest=manifest, runtimes=runtimes, records=records,
    )


def _snapshot(path: Path, field: str, *, executable_hint: bool) -> dict[str, Any] | None:
    if _is_link_like(path):
        _fail(field, "must not be link-like")
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        _fail(field, f"cannot inspect target: {exc}")
    data, metadata = _read_snapshot(path, field)
    executable = (
        executable_hint if os.name == "nt"
        else bool(stat.S_IMODE(metadata.st_mode) & 0o111)
    )
    return {
        "sha256_raw": sha256_bytes(data),
        "sha256_text_lf": _text_lf_digest(data),
        "size": len(data),
        "executable": executable,
    }


def _snapshot_value(record: dict[str, Any] | None) -> dict[str, Any] | None:
    if record is None:
        return None
    return {
        "sha256_raw": record["sha256_raw"],
        "sha256_text_lf": record["sha256_text_lf"],
        "size": record["size"],
        "executable": record["executable"],
    }


def _matches_base(
    observed: dict[str, Any] | None, base: dict[str, Any] | None,
) -> bool:
    if observed is None or base is None:
        return observed is base
    if observed["executable"] != base["executable"]:
        return False
    if (
        observed["sha256_raw"] == base["sha256_raw"]
        and observed["size"] == base["size"]
    ):
        return True
    return (
        base["sha256_text_lf"] is not None
        and observed["sha256_text_lf"] == base["sha256_text_lf"]
    )


def _owned_records(bundle: VerifiedBundle, component_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
    selected = set(_component_closure(bundle.manifest, component_ids))
    owners = {
        item["path"]: {"owner": item["owner"], "policy": item["policy"]}
        for item in resolved_component_paths(bundle.manifest, sorted(selected))
    }
    return {
        path: {**metadata, **bundle.records[path]}
        for path, metadata in owners.items()
    }


def _runtime_ids(bundle: VerifiedBundle) -> list[str]:
    return sorted(bundle.runtimes)


def _configured_components(bundle: VerifiedBundle, agents: Sequence[str]) -> list[str]:
    requested: set[str] = {"core"}
    for agent in agents:
        if agent not in bundle.runtimes:
            _fail("workspace.agents", f"runtime {agent!r} is unavailable in the bundle")
        requested.update(bundle.runtimes[agent]["components"])
    return _component_closure(bundle.manifest, sorted(requested))


def _assert_bundle_current(bundle: VerifiedBundle, field: str) -> None:
    try:
        verify_bundle(
            bundle.lock_path, bundle.root, expected_sha256=bundle.digest,
            source_mode=bundle.source_mode, role=bundle.role,
        )
    except BundleError as exc:
        raise BundleError(f"{field}: source or lock became stale: {exc}") from exc


def create_structural_plan(
    *,
    target_root: Path,
    workspace_config_path: Path,
    expected_config_sha256: str,
    candidate: VerifiedBundle,
    desired_components: Sequence[str],
    current: VerifiedBundle | None = None,
    current_components: Sequence[str] = (),
) -> dict[str, Any]:
    """Return a deterministic, digest-bound plan without mutating either tree."""
    target_root = _source_root(target_root, "target_root")
    try:
        if os.path.samefile(target_root, candidate.root):
            _fail("target_root", "must be separate from the candidate source root")
        if current is not None and os.path.samefile(target_root, current.root):
            _fail("target_root", "must be separate from the current source root")
    except OSError as exc:
        raise BundleError(f"cannot compare source and target roots: {exc}") from exc
    if current is not None and current.name != candidate.name:
        _fail("candidate_bundle", "cannot mix components from different bundle names")
    expected_config_sha256 = _sha256(expected_config_sha256, "expected_config_sha256")
    try:
        config_relative = workspace_config_path.absolute().relative_to(target_root).as_posix()
    except ValueError as exc:
        raise BundleError("workspace_config_path: must remain inside target_root") from exc
    config_path = _safe_path(
        target_root, config_relative, "workspace_config_path", missing_ok=False
    )
    config_bytes, _ = _read_snapshot(config_path, "workspace_config_path")
    config_digest = sha256_bytes(config_bytes)
    if config_digest != expected_config_sha256:
        _fail("expected_config_sha256", "workspace configuration is stale")
    authority = current if current is not None else candidate
    try:
        config_value = strict_json_loads(
            config_bytes.decode("utf-8"), source=config_relative
        )
        config = validate_workspace_config(
            config_value, known_runtime_ids=_runtime_ids(candidate)
        )
    except (UnicodeError, WorkspaceConfigError) as exc:
        raise BundleError(str(exc)) from exc
    if config["template"] != {"source": authority.name, "version": authority.version}:
        _fail("workspace.template", "does not match the current bundle identity")
    desired_ids = _component_closure(candidate.manifest, desired_components)
    required_desired_ids = _configured_components(candidate, config["agents"])
    if not set(required_desired_ids).issubset(desired_ids):
        missing = sorted(
            set(required_desired_ids) - set(desired_ids), key=portable_path_identity
        )
        _fail(
            "desired_components",
            "omits components required by configured agents: " + ", ".join(missing),
        )
    desired = _owned_records(candidate, desired_ids)
    base: dict[str, dict[str, Any]] = {}
    current_ids: list[str] = []
    if current is not None:
        current_ids = _component_closure(current.manifest, current_components)
        base = _owned_records(current, current_ids)
        all_current_ids = [item["id"] for item in current.manifest["components"]]
        all_current = _owned_records(current, all_current_ids)
        for relative in sorted(set(all_current) - set(base), key=portable_path_identity):
            if all_current[relative]["policy"] == "seed":
                continue
            target = _safe_path(
                target_root, relative, f"target.{relative}", missing_ok=True
            )
            if target.exists():
                _fail(
                    "current_components",
                    f"unclaimed materialized path {relative!r}; include component "
                    f"{all_current[relative]['owner']!r}",
                )
    elif current_components:
        _fail("current_components", "requires a current verified bundle")
    actions: list[dict[str, Any]] = []
    for relative in sorted(set(base) | set(desired), key=portable_path_identity):
        before = base.get(relative)
        after = desired.get(relative)
        executable_hint = (before or after or {}).get("executable", False)
        target = _safe_path(
            target_root, relative, f"target.{relative}", missing_ok=True
        )
        observed = _snapshot(target, f"target.{relative}", executable_hint=executable_hint)
        observed_value = _snapshot_value(observed)
        before_value = _snapshot_value(before)
        after_value = _snapshot_value(after)
        owner = (after or before)["owner"]
        policy = (after or before)["policy"]
        if before is None:
            if observed is not None:
                if policy == "seed":
                    action, reason = (
                        "preserve-seed",
                        "pre-existing seed path remains user-owned",
                    )
                else:
                    _fail(f"target.{relative}", "unowned target collides with a planned add")
            else:
                action, reason = "add", "selected component path is absent"
        elif after is None:
            if observed is None:
                action, reason = "noop", "previously owned path is already absent"
            elif policy == "seed":
                action, reason = "preserve-seed", "seed content is user-owned after creation"
            elif not _matches_base(observed_value, before_value):
                _fail(f"target.{relative}", "managed path is dirty and cannot be removed")
            else:
                action, reason = "remove", "pristine managed path is no longer selected"
        elif policy == "seed" or before["policy"] == "seed":
            if before["policy"] != after["policy"]:
                _fail(f"target.{relative}", "customization policy changed across bundles")
            if observed is None:
                action, reason = "preserve-seed", "deleted seed remains user-owned"
            else:
                action, reason = "preserve-seed", "existing seed remains user-owned"
        else:
            if before["owner"] != after["owner"] or before["policy"] != after["policy"]:
                _fail(f"target.{relative}", "ownership or customization policy changed across bundles")
            if observed is None:
                _fail(f"target.{relative}", "managed path is missing")
            if not _matches_base(observed_value, before_value):
                _fail(f"target.{relative}", "managed path is dirty")
            if before_value == after_value:
                action, reason = "noop", "managed path already matches candidate"
            else:
                action, reason = "replace", "pristine managed path changes in candidate"
        actions.append({
            "path": relative,
            "owner": owner,
            "policy": policy,
            "action": action,
            "base": before_value,
            "current": observed_value,
            "desired": after_value,
            "reason": reason,
        })
    _assert_bundle_current(candidate, "candidate_bundle")
    if current is not None:
        _assert_bundle_current(current, "current_bundle")
    config_after, _ = _read_snapshot(config_path, "workspace_config_path")
    if sha256_bytes(config_after) != config_digest:
        _fail("workspace_config_path", "changed while the plan was being created")
    for item in actions:
        relative = item["path"]
        before = base.get(relative)
        after = desired.get(relative)
        executable_hint = (before or after or {}).get("executable", False)
        target = _safe_path(
            target_root, relative, f"target.{relative}", missing_ok=True
        )
        observed = _snapshot(
            target, f"target.{relative}", executable_hint=executable_hint
        )
        if _snapshot_value(observed) != item["current"]:
            _fail(f"target.{relative}", "changed while the plan was being created")
    unsigned = {
        "schema_version": PLANNER_PROTOCOL_VERSION,
        "candidate_bundle_sha256": candidate.digest,
        "current_bundle_sha256": current.digest if current is not None else None,
        "workspace_config_sha256_raw": config_digest,
        "executable_modes_verified": {
            "candidate_source": candidate.mode_verified,
            "current_source": current.mode_verified if current is not None else None,
            "target": os.name != "nt",
        },
        "intended_workspace": {
            **config,
            "template": {"source": candidate.name, "version": candidate.version},
        },
        "current_components": current_ids,
        "desired_components": desired_ids,
        "actions": actions,
    }
    return {
        **unsigned,
        "plan_digest": sha256_bytes(canonical_json(unsigned).encode("utf-8")),
    }


def create_initial_structural_plan(
    *,
    target_root: Path,
    workspace_config: dict[str, Any],
    candidate: VerifiedBundle,
    desired_components: Sequence[str],
) -> dict[str, Any]:
    """Plan a first materialization without requiring tracked target state first."""
    target_root = _source_root(target_root, "target_root")
    try:
        if os.path.samefile(target_root, candidate.root):
            _fail("target_root", "must be separate from the candidate source root")
    except OSError as exc:
        raise BundleError(f"cannot compare source and target roots: {exc}") from exc
    try:
        config = validate_workspace_config(
            workspace_config, known_runtime_ids=_runtime_ids(candidate)
        )
    except WorkspaceConfigError as exc:
        raise BundleError(str(exc)) from exc
    if config["template"] != {
        "source": candidate.name,
        "version": candidate.version,
    }:
        _fail("workspace.template", "does not match the candidate bundle identity")
    desired_ids = _component_closure(candidate.manifest, desired_components)
    required_desired_ids = _configured_components(candidate, config["agents"])
    if not set(required_desired_ids).issubset(desired_ids):
        missing = sorted(
            set(required_desired_ids) - set(desired_ids), key=portable_path_identity
        )
        _fail(
            "desired_components",
            "omits components required by configured agents: " + ", ".join(missing),
        )
    desired = _owned_records(candidate, desired_ids)
    actions: list[dict[str, Any]] = []
    for relative in sorted(desired, key=portable_path_identity):
        after = desired[relative]
        target = _safe_path(
            target_root, relative, f"target.{relative}", missing_ok=True
        )
        observed = _snapshot(
            target,
            f"target.{relative}",
            executable_hint=after["executable"],
        )
        if observed is not None and after["policy"] != "seed":
            _fail(f"target.{relative}", "unowned target collides with a planned add")
        action = "preserve-seed" if observed is not None else "add"
        reason = (
            "pre-existing seed path remains user-owned"
            if observed is not None
            else "selected component path is absent"
        )
        actions.append({
            "path": relative,
            "owner": after["owner"],
            "policy": after["policy"],
            "action": action,
            "base": None,
            "current": _snapshot_value(observed),
            "desired": _snapshot_value(after),
            "reason": reason,
        })
    _assert_bundle_current(candidate, "candidate_bundle")
    for item in actions:
        target = _safe_path(
            target_root, item["path"], f"target.{item['path']}", missing_ok=True
        )
        observed = _snapshot(
            target,
            f"target.{item['path']}",
            executable_hint=desired[item["path"]]["executable"],
        )
        if _snapshot_value(observed) != item["current"]:
            _fail(f"target.{item['path']}", "changed while the plan was being created")
    config_digest = sha256_bytes(render_workspace_config(config).encode("utf-8"))
    unsigned = {
        "schema_version": PLANNER_PROTOCOL_VERSION,
        "candidate_bundle_sha256": candidate.digest,
        "current_bundle_sha256": None,
        "workspace_config_sha256_raw": config_digest,
        "executable_modes_verified": {
            "candidate_source": candidate.mode_verified,
            "current_source": None,
            "target": os.name != "nt",
        },
        "intended_workspace": config,
        "current_components": [],
        "desired_components": desired_ids,
        "actions": actions,
    }
    return {
        **unsigned,
        "plan_digest": sha256_bytes(canonical_json(unsigned).encode("utf-8")),
    }


def bundle_schema_document() -> dict[str, Any]:
    digest = {"type": "string", "pattern": SHA256_RE.pattern}
    text = {"type": "string", "minLength": 1}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$comment": (
            "Generated structural subset. contextos.bundle_schema is authoritative "
            "for exact keys, portable paths, compatibility, source verification, and digests."
        ),
        "title": "Context OS detached bundle lock",
        "type": "object",
        "additionalProperties": False,
        "required": sorted(LOCK_KEYS),
        "properties": {
            "schema_version": {"const": BUNDLE_LOCK_SCHEMA_VERSION},
            "bundle_sha256": digest,
            "bundle": {
                "type": "object",
                "additionalProperties": False,
                "required": sorted(BUNDLE_KEYS),
                "properties": {
                    "name": text,
                    "version": text,
                    "source_git_commit": {
                        "oneOf": [
                            {"type": "string", "pattern": GIT_COMMIT_RE.pattern},
                            {"type": "null"},
                        ]
                    },
                    "component_manifest_path": {"const": "components/manifest.json"},
                    "compatibility": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": sorted(COMPATIBILITY_KEYS),
                        "properties": {
                            key: {"type": "integer", "minimum": 1}
                            for key in sorted(COMPATIBILITY_KEYS)
                        },
                    },
                    "files": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": sorted(FILE_KEYS),
                            "properties": {
                                "path": text,
                                "sha256_raw": digest,
                                "sha256_text_lf": {
                                    "oneOf": [digest, {"type": "null"}]
                                },
                                "size": {"type": "integer", "minimum": 0},
                                "executable": {"type": "boolean"},
                            },
                        },
                    },
                },
            },
        },
    }
