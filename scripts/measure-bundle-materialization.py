#!/usr/bin/env python3
"""Measure full-bundle Git-index composition and upgrade resource bounds."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
import tracemalloc
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from contextos.bundle_schema import create_bundle_lock, verify_bundle  # noqa: E402
from contextos import materializer  # noqa: E402
from contextos.kernel import apply_proposal  # noqa: E402
from contextos.materializer import (  # noqa: E402
    create_composition_proposal,
    create_materialization_proposal,
)
from contextos.primitives import git_environment  # noqa: E402


NOW = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)
EXPECTED_GIT_PROCESSES = {"compose": 48, "upgrade": 96}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def _run(command: Sequence[str], *, cwd: Path) -> None:
    subprocess.run(
        list(command), cwd=cwd, check=True, capture_output=True,
        env=git_environment(),
    )


def _build_source(source: Path) -> None:
    """Create a clean Git source containing every manifest-classified path."""
    manifest = json.loads(
        (ROOT / "components" / "manifest.json").read_text(encoding="utf-8")
    )
    paths = sorted({
        entry["path"]
        for component in manifest["components"]
        for entry in component["paths"]
    })
    source.mkdir()
    for relative in paths:
        source_path = ROOT / Path(relative)
        target_path = source / Path(relative)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(source_path.read_bytes())
    _run(["git", "init", "--quiet"], cwd=source)
    _run(["git", "config", "user.email", "measure@example.invalid"], cwd=source)
    _run(["git", "config", "user.name", "Bundle Measurement"], cwd=source)
    _run(["git", "config", "core.autocrlf", "false"], cwd=source)
    _run(["git", "add", "--force", "--all"], cwd=source)
    _run(["git", "commit", "--quiet", "-m", "measurement source"], cwd=source)


def _commit_candidate_delta(source: Path, manifest: dict[str, Any]) -> str:
    relative = next(
        entry["path"]
        for component in manifest["components"]
        for entry in component["paths"]
        if entry["policy"] == "managed" and entry["path"].endswith(".md")
    )
    path = source / relative
    path.write_bytes(path.read_bytes() + b"\nCandidate measurement delta.\n")
    _run(["git", "add", "--", relative], cwd=source)
    _run(["git", "commit", "--quiet", "-m", "candidate delta"], cwd=source)
    return relative


def _is_git_command(command: object) -> bool:
    if not isinstance(command, (list, tuple)) or not command:
        return False
    executable = Path(str(command[0])).name.casefold()
    return executable in {"git", "git.exe"}


@contextmanager
def _git_process_measurement() -> Iterator[list[list[str]]]:
    commands: list[list[str]] = []
    original_popen = subprocess.Popen

    def measured_popen(
        command: object, *args: Any, **kwargs: Any
    ) -> subprocess.Popen[Any]:
        if _is_git_command(command):
            commands.append([str(item) for item in command])  # type: ignore[union-attr]
        return original_popen(command, *args, **kwargs)

    with mock.patch("subprocess.Popen", side_effect=measured_popen):
        yield commands


@contextmanager
def _retained_payload_measurement() -> Iterator[list[int]]:
    samples: list[int] = []
    original = materializer.prepare_materialization_preflight

    def measured(*args: Any, **kwargs: Any) -> tuple[datetime, dict[str, bytes]]:
        result = original(*args, **kwargs)
        samples.append(sum(len(payload) for payload in result[1].values()))
        return result

    with mock.patch.object(
        materializer, "prepare_materialization_preflight", side_effect=measured
    ):
        yield samples


@contextmanager
def _python_peak_measurement() -> Iterator[list[int]]:
    peak = [0]
    tracemalloc.start()
    try:
        yield peak
    finally:
        _current, peak[0] = tracemalloc.get_traced_memory()
        tracemalloc.stop()


def _schema_paths(lock: dict[str, Any], role: str) -> set[str]:
    records = lock["bundle"]["files"]
    paths = {lock["bundle"]["component_manifest_path"]}
    if role == "candidate":
        paths.update(
            item["path"]
            for item in records
            if item["path"].startswith("runtimes/")
            and item["path"].endswith(".json")
            and item["path"] != "runtimes/schema.json"
        )
    return paths


def _logical_verification_peak(
    lock: dict[str, Any], *, role: str, retain_paths: set[str]
) -> int:
    """Mirror verify_bundle's payload lifetime without sampling process RSS."""
    retained_paths = retain_paths | _schema_paths(lock, role)
    retained_bytes = 0
    peak_bytes = 0
    previous_unretained_bytes = 0
    for item in lock["bundle"]["files"]:
        peak_bytes = max(
            peak_bytes,
            retained_bytes + previous_unretained_bytes + item["size"],
        )
        if item["path"] in retained_paths:
            retained_bytes += item["size"]
            previous_unretained_bytes = 0
        else:
            previous_unretained_bytes = item["size"]
    return peak_bytes


def _logical_verification_bound(
    lock: dict[str, Any], *, role: str, retain_paths: set[str]
) -> int:
    records = {item["path"]: item["size"] for item in lock["bundle"]["files"]}
    schema_only = _schema_paths(lock, role) - retain_paths
    return (
        sum(records[path] for path in retain_paths)
        + sum(records[path] for path in schema_only)
        + 2 * max(records.values())
    )


def _bundle_references(proposal: dict[str, Any]) -> set[str]:
    return {
        reference["path"]
        for change in proposal["changes"]
        if change["action"] == "write"
        for reference in [change["content_ref"]]
        if reference["kind"] == "bundle" and reference["role"] == "candidate"
    }


def _metric(
    name: str,
    started: float,
    commands: list[list[str]],
    lock: dict[str, Any],
    retained_paths: set[str],
    observed_retained_payload_bytes: int,
    traced_python_peak_bytes: int,
) -> dict[str, Any]:
    batch_commands = [
        command for command in commands if "cat-file" in command and "--batch" in command
    ]
    per_blob_commands = [
        command
        for command in commands
        if "cat-file" in command and "blob" in command and "--batch" not in command
    ]
    logical_peak = max(
        _logical_verification_peak(lock, role="candidate", retain_paths=set()),
        _logical_verification_peak(
            lock, role="candidate", retain_paths=retained_paths
        ),
    )
    logical_bound = max(
        _logical_verification_bound(lock, role="candidate", retain_paths=set()),
        _logical_verification_bound(
            lock, role="candidate", retain_paths=retained_paths
        ),
    )
    return {
        "git_subprocess_count": len(commands),
        "git_batch_process_count": len(batch_commands),
        "git_per_blob_process_count": len(per_blob_commands),
        "wall_seconds": round(time.perf_counter() - started, 6),
        "logical_peak_retained_payload_bytes": logical_peak,
        "logical_retained_payload_bound_bytes": logical_bound,
        "observed_staging_payload_bytes": observed_retained_payload_bytes,
        "tracemalloc_peak_bytes": traced_python_peak_bytes,
        "retained_bundle_path_count": len(retained_paths),
        "matches_expected_git_process_count": (
            len(commands) == EXPECTED_GIT_PROCESSES[name]
        ),
    }


def measure() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="contextos-bundle-measure-") as temporary:
        root = Path(temporary)
        current_source = root / "current-source"
        candidate_source = root / "candidate-source"
        _build_source(current_source)
        shutil.copytree(current_source, candidate_source)

        manifest = json.loads(
            (current_source / "components" / "manifest.json").read_text(
                encoding="utf-8"
            )
        )
        changed_path = _commit_candidate_delta(candidate_source, manifest)

        current_lock = create_bundle_lock(
            current_source, name="agent-context-os-template", version="1.0.0"
        )
        candidate_lock = create_bundle_lock(
            candidate_source, name="agent-context-os-template", version="2.0.0"
        )
        current_lock_path = root / "current.lock.json"
        candidate_lock_path = root / "candidate.lock.json"
        _write_json(current_lock_path, current_lock)
        _write_json(candidate_lock_path, candidate_lock)

        all_components = [item["id"] for item in manifest["components"]]
        runtime_ids = sorted(
            path.stem for path in (current_source / "runtimes").glob("*.json")
            if path.name != "schema.json"
        )
        target = root / "target"
        target.mkdir()
        config_input = root / "workspace-v1.json"
        _write_json(config_input, {
            "schema_version": 1,
            "mode": "full-template",
            "agents": runtime_ids,
            "paths": {
                "state_dir": "state",
                "sessions_dir": "sessions",
                "task_file": "TODO.md",
            },
            "template": {
                "source": "agent-context-os-template",
                "version": "1.0.0",
            },
        })

        with (
            _git_process_measurement() as compose_commands,
            _retained_payload_measurement() as compose_retained,
            _python_peak_measurement() as compose_python_peak,
        ):
            compose_started = time.perf_counter()
            current = verify_bundle(
                current_lock_path, current_source,
                expected_sha256=current_lock["bundle_sha256"],
                source_mode="git-index", retain_paths=(),
            )
            compose_path, compose_proposal = create_composition_proposal(
                target_root=target,
                workspace_config_path=target / "contextos.workspace.json",
                workspace_config_input_path=config_input,
                expected_config_input_sha256=_sha256(config_input),
                candidate=current,
                desired_components=all_components,
                now=NOW,
            )
            apply_proposal(
                target, compose_path, compose_proposal["proposal_digest"], "generic"
            )
        compose = _metric(
            "compose", compose_started, compose_commands, current_lock,
            _bundle_references(compose_proposal),
            max(compose_retained, default=0), compose_python_peak[0],
        )

        with (
            _git_process_measurement() as upgrade_commands,
            _retained_payload_measurement() as upgrade_retained,
            _python_peak_measurement() as upgrade_python_peak,
        ):
            upgrade_started = time.perf_counter()
            candidate = verify_bundle(
                candidate_lock_path, candidate_source,
                expected_sha256=candidate_lock["bundle_sha256"],
                source_mode="git-index", retain_paths=(),
            )
            current = verify_bundle(
                current_lock_path, current_source,
                expected_sha256=current_lock["bundle_sha256"],
                source_mode="git-index", role="current", retain_paths=(),
            )
            config_path = target / "contextos.workspace.json"
            upgrade_path, upgrade_proposal = create_materialization_proposal(
                target_root=target,
                workspace_config_path=config_path,
                expected_config_sha256=_sha256(config_path),
                candidate=candidate,
                desired_components=all_components,
                current=current,
                current_components=all_components,
                now=NOW.replace(second=1),
            )
            apply_proposal(
                target, upgrade_path, upgrade_proposal["proposal_digest"], "generic"
            )
        upgrade = _metric(
            "upgrade", upgrade_started, upgrade_commands, candidate_lock,
            _bundle_references(upgrade_proposal),
            max(upgrade_retained, default=0), upgrade_python_peak[0],
        )

        return {
            "schema_version": 1,
            "measurement": "full-bundle-materialization-resource-bounds",
            "bundle_file_count": len(candidate_lock["bundle"]["files"]),
            "bundle_payload_bytes": sum(
                item["size"] for item in candidate_lock["bundle"]["files"]
            ),
            "upgrade_changed_bundle_path": changed_path,
            "compose": compose,
            "upgrade": upgrade,
        }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true",
        help="fail on per-blob Git processes or a fixed process-count regression",
    )
    args = parser.parse_args(argv)
    report = measure()
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if args.check:
        for name in ("compose", "upgrade"):
            metric = report[name]
            if metric["git_per_blob_process_count"] != 0:
                print(f"{name}: per-blob Git subprocesses detected", file=sys.stderr)
                return 1
            if not metric["matches_expected_git_process_count"]:
                print(
                    f"{name}: expected {EXPECTED_GIT_PROCESSES[name]} Git subprocesses, "
                    f"observed {metric['git_subprocess_count']}",
                    file=sys.stderr,
                )
                return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
