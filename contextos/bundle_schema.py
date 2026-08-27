"""Immutable offline bundle locks and read-only structural composition plans."""

from __future__ import annotations

import hashlib
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
from .workspace_schema import (
    WORKSPACE_SCHEMA_VERSION,
    WorkspaceConfigError,
    strict_json_loads,
    validate_workspace_config,
    validate_workspace_path,
)


BUNDLE_LOCK_SCHEMA_VERSION = 1
PLANNER_PROTOCOL_VERSION = 1
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
BUNDLE_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")
LOCK_KEYS = {"schema_version", "bundle", "bundle_sha256"}
BUNDLE_KEYS = {
    "name", "version", "compatibility", "component_manifest_path", "files",
}
COMPATIBILITY_KEYS = {
    "component_manifest_schema", "runtime_descriptor_schema",
    "workspace_schema", "planner_protocol",
}
FILE_KEYS = {"path", "sha256_raw", "size", "executable"}


class BundleError(ValueError):
    """Raised when a lock, offline source, or structural plan is unsafe."""


@dataclass(frozen=True)
class VerifiedBundle:
    root: Path
    lock_path: Path
    source_mode: str
    mode_verified: bool
    lock: dict[str, Any]
    manifest: dict[str, Any]
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


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


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
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise BundleError(f"cannot inspect {path} without following links: {exc}") from exc
    tags = {
        getattr(stat, "IO_REPARSE_TAG_SYMLINK", -1),
        getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", -2),
    }
    return stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_reparse_tag", 0) in tags


def _source_root(root: Path, field: str) -> Path:
    if not isinstance(root, Path):
        _fail(field, "must be an explicit local Path")
    absolute = root.absolute()
    for ancestor in reversed((absolute, *absolute.parents)):
        if ancestor.exists() and _is_link_like(ancestor):
            _fail(field, f"must not traverse link-like path {ancestor}")
    try:
        metadata = absolute.lstat()
    except OSError as exc:
        _fail(field, f"cannot inspect local directory: {exc}")
    if not stat.S_ISDIR(metadata.st_mode):
        _fail(field, "must be an existing local directory")
    return absolute


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
                break
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
    if _is_link_like(path):
        _fail(field, "must not be link-like")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        _fail(field, f"cannot open regular-file snapshot: {exc}")
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            _fail(field, "must name a regular file")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        current = os.stat(path, follow_symlinks=False)
        fingerprint_after = (
            after.st_mode, after.st_size, after.st_mtime_ns, after.st_ctime_ns
        )
        current_content_fingerprint = (current.st_size, current.st_mtime_ns)
        if (
            before.st_mode, before.st_size, before.st_mtime_ns, before.st_ctime_ns
        ) != fingerprint_after or (
            after.st_size, after.st_mtime_ns
        ) != current_content_fingerprint or (
            after.st_dev, after.st_ino
        ) != (current.st_dev, current.st_ino):
            _fail(field, "changed while it was being read")
        return b"".join(chunks), after
    finally:
        os.close(descriptor)


def _record(path: str, data: bytes, executable: bool) -> dict[str, Any]:
    return {
        "path": path,
        "sha256_raw": sha256_bytes(data),
        "size": len(data),
        "executable": executable,
    }


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
            ["git", "-c", "core.fsmonitor=false", "ls-files", "--stage", "-z", "--"],
            cwd=root, env=_git_environment(),
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
            ["git", "cat-file", "blob", oid], cwd=root, check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_git_environment(),
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = (
            exc.stderr.decode("utf-8", errors="replace").strip()
            if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        )
        _fail(field, f"cannot read local Git blob: {detail}")
    return result.stdout


def _git_environment() -> dict[str, str]:
    """Keep local Git plumbing read-only, offline, and free of executable config."""
    environment = os.environ.copy()
    environment.update({
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
    })
    return environment


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


def create_bundle_lock(
    root: Path, *, name: str, version: str, source_mode: str = "git-index",
) -> dict[str, Any]:
    """Build a deterministic detached lock from one strict maintainer source tree."""
    root = _source_root(root, "source_root")
    name = _bundle_name(name, "bundle.name")
    version = _bundle_version(version, "bundle.version")
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
    files = []
    for item in paths:
        data, executable = source[item["path"]]
        files.append(_record(item["path"], data, executable))
    bundle = {
        "name": name,
        "version": version,
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
    compatibility = _exact_keys(
        bundle.get("compatibility"), COMPATIBILITY_KEYS, "bundle.compatibility"
    )
    expected_compatibility = {
        "component_manifest_schema": COMPONENT_MANIFEST_SCHEMA_VERSION,
        "runtime_descriptor_schema": RUNTIME_DESCRIPTOR_SCHEMA_VERSION,
        "workspace_schema": WORKSPACE_SCHEMA_VERSION,
        "planner_protocol": PLANNER_PROTOCOL_VERSION,
    }
    for key, expected in expected_compatibility.items():
        if type(compatibility.get(key)) is not int or compatibility[key] != expected:
            _fail(f"bundle.compatibility.{key}", f"must equal integer {expected}")
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
        if type(item.get("size")) is not int or item["size"] < 0:
            _fail(f"{field}.size", "must be a non-negative integer")
        if type(item.get("executable")) is not bool:
            _fail(f"{field}.executable", "must be a boolean")
        files.append(item)
    ordered = sorted(files, key=lambda item: portable_path_identity(item["path"]))
    if files != ordered:
        _fail("bundle.files", "must use portable path order")
    identities_in_order = [portable_path_identity(item["path"]).split("/") for item in files]
    for left, right in zip(identities_in_order, identities_in_order[1:]):
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


def verify_bundle(
    lock_path: Path, source_root: Path, *, expected_sha256: str,
    source_mode: str = "directory",
) -> VerifiedBundle:
    """Verify one caller-pinned lock and all of its local bytes without fetching."""
    expected_sha256 = _sha256(expected_sha256, "expected_sha256")
    lock_path = lock_path.absolute()
    lock = load_bundle_lock(lock_path)
    if lock["bundle_sha256"] != expected_sha256:
        _fail("expected_sha256", "does not match the supplied bundle lock")
    root = _source_root(source_root, "source_root")
    records = {item["path"]: item for item in lock["bundle"]["files"]}
    source = _source_entries(root, records, source_mode=source_mode)
    verified_bytes: dict[str, bytes] = {}
    for relative, record in records.items():
        data, executable = source[relative]
        if len(data) != record["size"] or sha256_bytes(data) != record["sha256_raw"]:
            _fail(f"source.{relative}", "raw bytes do not match the bundle lock")
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
    return VerifiedBundle(
        root=root, lock_path=lock_path, source_mode=source_mode,
        mode_verified=(source_mode == "git-index" or os.name != "nt"), lock=lock,
        manifest=manifest, records=records,
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
        "size": len(data),
        "executable": executable,
    }


def _snapshot_value(record: dict[str, Any] | None) -> dict[str, Any] | None:
    if record is None:
        return None
    return {
        "sha256_raw": record["sha256_raw"],
        "size": record["size"],
        "executable": record["executable"],
    }


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
    return sorted(
        PurePosixPath(path).stem
        for path in bundle.records
        if len(PurePosixPath(path).parts) == 2
        and PurePosixPath(path).parts[0] == "runtimes"
        and path.endswith(".json")
        and path != "runtimes/schema.json"
    )


def _configured_components(bundle: VerifiedBundle, agents: Sequence[str]) -> list[str]:
    requested: set[str] = {"core"}
    paths = [f"runtimes/{agent}.json" for agent in agents]
    for path in paths:
        if path not in bundle.records:
            _fail("workspace.agents", f"runtime descriptor {path!r} is unavailable in the bundle")
    source = _source_entries(bundle.root, paths, source_mode=bundle.source_mode)
    for agent, path in zip(agents, paths):
        data, executable = source[path]
        record = bundle.records[path]
        if (
            len(data) != record["size"]
            or sha256_bytes(data) != record["sha256_raw"]
            or (
                (bundle.source_mode == "git-index" or os.name != "nt")
                and executable != record["executable"]
            )
        ):
            _fail(path, "runtime descriptor became stale after bundle verification")
        try:
            descriptor = strict_json_loads(
                data.decode("utf-8"), source=path
            )
        except (UnicodeError, WorkspaceConfigError) as exc:
            raise BundleError(str(exc)) from exc
        if not isinstance(descriptor, dict) or descriptor.get("runtime") != agent:
            _fail(path, f"must declare runtime {agent!r}")
        components = descriptor.get("components")
        if not isinstance(components, list) or not components or any(
            not isinstance(item, str) for item in components
        ):
            _fail(path, "must declare a non-empty component array")
        requested.update(components)
    return _component_closure(bundle.manifest, sorted(requested))


def _assert_bundle_current(bundle: VerifiedBundle, field: str) -> None:
    try:
        verify_bundle(
            bundle.lock_path, bundle.root, expected_sha256=bundle.digest,
            source_mode=bundle.source_mode,
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
            config_value, known_runtime_ids=_runtime_ids(authority)
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
        configured_ids = _configured_components(current, config["agents"])
        if current_ids != configured_ids:
            _fail(
                "current_components",
                "must exactly match the component closure declared by workspace agents",
            )
        base = _owned_records(current, current_ids)
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
                _fail(f"target.{relative}", "unowned target collides with a planned add")
            action, reason = "add", "selected component path is absent"
        elif after is None:
            if observed is None:
                action, reason = "noop", "previously owned path is already absent"
            elif policy == "seed":
                action, reason = "preserve-seed", "seed content is user-owned after creation"
            elif observed_value != before_value:
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
            if observed_value != before_value:
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
                    "component_manifest_path": {"const": "components/manifest.json"},
                    "compatibility": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": sorted(COMPATIBILITY_KEYS),
                        "properties": {
                            "component_manifest_schema": {"const": COMPONENT_MANIFEST_SCHEMA_VERSION},
                            "runtime_descriptor_schema": {"const": RUNTIME_DESCRIPTOR_SCHEMA_VERSION},
                            "workspace_schema": {"const": WORKSPACE_SCHEMA_VERSION},
                            "planner_protocol": {"const": PLANNER_PROTOCOL_VERSION},
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
                                "size": {"type": "integer", "minimum": 0},
                                "executable": {"type": "boolean"},
                            },
                        },
                    },
                },
            },
        },
    }
