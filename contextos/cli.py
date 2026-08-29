from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .bundle_schema import (
    BundleError,
    create_bundle_lock,
    create_structural_plan,
    verify_bundle,
)
from .kernel import (
    ContextOSError,
    agent_list_report,
    apply_proposal,
    create_agent_activation_proposal,
    create_proposal,
    create_workspace_migration_proposal,
    create_workspace_setup_proposal,
    discover_root,
    doctor,
    hook_report,
    install_runtime,
    migrate_legacy_runtime_state,
    parse_now,
    plan_workspace_migration,
    read_json,
    render_hook_payload,
    runtime_ids,
    runtime_manifest,
    runtime_surface,
    start_report,
    workspace_resolution_report,
)
from .materializer import (
    MATERIALIZE_OPERATION,
    create_composition_proposal,
    create_materialization_proposal,
)
from .workspace_schema import WorkspaceConfigError, parse_agent_selection


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="context-os", description="Deterministic Context OS lifecycle kernel")
    result.add_argument(
        "--root",
        type=Path,
        help=(
            "v0.12 discovery start for the nearest Context OS root "
            "(ContextRoot and nominal WorkingRoot; also KernelRoot for the "
            "full-template wrapper path)"
        ),
    )
    result.add_argument("--version", action="version", version=__version__)
    commands = result.add_subparsers(dest="command", required=True)

    start = commands.add_parser("start", help="Read workspace continuity as structured data")
    start.add_argument("--now", help="ISO-8601 timestamp for deterministic runs")

    propose = commands.add_parser("propose", help="Create a reviewable lifecycle proposal")
    propose.add_argument("workflow", choices=("setup", "update", "end"))
    propose.add_argument("--input", type=Path, required=True, help="Reviewed JSON payload")
    propose.add_argument("--now", help="ISO-8601 timestamp for deterministic runs")

    apply = commands.add_parser("apply", help="Apply one exact host-confirmed proposal")
    apply.add_argument("proposal", type=Path)
    apply.add_argument("--confirm", required=True, help="Exact proposal digest printed by propose")
    apply.add_argument("--runtime", metavar="RUNTIME", required=True)

    install = commands.add_parser(
        "install", help="Record local onboarding for one runtime and print host setup steps"
    )
    install.add_argument("--runtime", metavar="RUNTIME", required=True)

    agent = commands.add_parser(
        "agent", help="Inspect or propose changes to tracked agent activation"
    )
    agent_commands = agent.add_subparsers(dest="agent_command", required=True)
    agent_commands.add_parser(
        "list", help="List registered runtimes and their tracked/local status"
    )
    for agent_command in ("add", "enable", "disable"):
        activation = agent_commands.add_parser(
            agent_command,
            help=(
                "Create an exact proposal to disable one tracked runtime"
                if agent_command == "disable"
                else "Create an exact proposal to enable one bundled runtime"
            ),
        )
        activation.add_argument("--runtime", metavar="RUNTIME", required=True)
        activation.add_argument(
            "--now", help="ISO-8601 timestamp for deterministic proposal IDs"
        )

    diagnose = commands.add_parser(
        "doctor", help="Check workspace health with tracked agent-set awareness"
    )
    diagnose_selection = diagnose.add_mutually_exclusive_group()
    diagnose_selection.add_argument("--runtime", metavar="RUNTIME")
    diagnose_selection.add_argument(
        "--all", action="store_true", help="Strictly validate every shipped runtime"
    )

    workspace = commands.add_parser(
        "workspace", help="Inspect tracked workspace intent or preview migration"
    )
    workspace_commands = workspace.add_subparsers(
        dest="workspace_command", required=True
    )
    workspace_commands.add_parser(
        "show", help="Show effective tracked workspace configuration and precedence"
    )
    migrate = workspace_commands.add_parser(
        "migrate", help="Preview canonical tracked JSON without writing it"
    )
    selection = migrate.add_mutually_exclusive_group(required=True)
    selection.add_argument(
        "--agents", action="append", help="Comma-separated runtime ids, or none for core-only"
    )
    selection.add_argument(
        "--agent",
        action="append",
        help="Deprecated singleton compatibility alias; use --agents",
    )
    propose_migration = workspace_commands.add_parser(
        "propose-migration",
        help="Create a digest-bound proposal to write JSON and retire legacy YAML",
    )
    proposal_selection = propose_migration.add_mutually_exclusive_group(required=True)
    proposal_selection.add_argument(
        "--agents", action="append", help="Comma-separated runtime ids, or none for core-only"
    )
    proposal_selection.add_argument(
        "--agent",
        action="append",
        help="Deprecated singleton compatibility alias; use --agents",
    )
    propose_migration.add_argument(
        "--now", help="ISO-8601 timestamp for deterministic proposal IDs"
    )
    propose_setup = workspace_commands.add_parser(
        "propose-setup",
        help="Create an additive digest-bound setup proposal for tracked agents",
    )
    setup_selection = propose_setup.add_mutually_exclusive_group(required=True)
    setup_selection.add_argument(
        "--agents", action="append", help="Comma-separated runtime ids, or none for core-only"
    )
    setup_selection.add_argument(
        "--agent",
        action="append",
        help="Deprecated singleton compatibility alias; use --agents",
    )
    propose_setup.add_argument(
        "--now", help="ISO-8601 timestamp for deterministic proposal IDs"
    )
    workspace_commands.add_parser(
        "migrate-local-runtime",
        help="Atomically copy legacy local runtime state into hosts.json",
    )

    hook = commands.add_parser("hook", help="Run a normalized read-only lifecycle hook check")
    hook.add_argument("event", choices=("session-start", "pre-write"))
    hook.add_argument("--runtime", metavar="RUNTIME", required=True)
    hook.add_argument("--surface", metavar="SURFACE")

    bundle = commands.add_parser(
        "bundle", help="Generate, verify, or plan from immutable offline bundles"
    )
    bundle_commands = bundle.add_subparsers(dest="bundle_command", required=True)
    generate_bundle = bundle_commands.add_parser(
        "generate", help="Print a detached lock for one explicit local source"
    )
    generate_bundle.add_argument("--source", type=Path, required=True)
    generate_bundle.add_argument("--name", required=True)
    generate_bundle.add_argument("--bundle-version", required=True)
    check_bundle = bundle_commands.add_parser(
        "check", help="Verify a caller-pinned lock against local source bytes"
    )
    check_bundle.add_argument("--lock", type=Path, required=True)
    check_bundle.add_argument("--source", type=Path, required=True)
    check_bundle.add_argument("--expect-sha256", required=True)
    check_bundle.add_argument(
        "--source-mode", choices=("git-index", "directory"), default="directory"
    )
    plan_bundle = bundle_commands.add_parser(
        "plan", help="Print a deterministic read-only structural plan"
    )
    propose_bundle = bundle_commands.add_parser(
        "propose", help="Create a digest-bound materialization proposal"
    )
    compose_bundle = bundle_commands.add_parser(
        "compose", help="Create a first-install proposal for a clean target"
    )
    apply_bundle = bundle_commands.add_parser(
        "apply", help="Apply a materialization proposal to its explicit target"
    )
    apply_bundle.add_argument("--target", type=Path, required=True)
    apply_bundle.add_argument("--proposal", type=Path, required=True)
    apply_bundle.add_argument("--confirm", required=True)
    apply_bundle.add_argument("--runtime", default="generic")
    for command in (plan_bundle, propose_bundle, compose_bundle):
        command.add_argument("--lock", type=Path, required=True)
        command.add_argument("--source", type=Path, required=True)
        command.add_argument("--expect-sha256", required=True)
        command.add_argument(
            "--source-mode", choices=("git-index", "directory"), default="directory"
        )
        command.add_argument("--target", type=Path, required=True)
        command.add_argument("--workspace-config", type=Path, required=True)
        command.add_argument("--expect-config-sha256", required=True)
        command.add_argument("--components", required=True)
        command.add_argument("--current-lock", type=Path)
        command.add_argument("--current-source", type=Path)
        command.add_argument("--expect-current-sha256")
        command.add_argument(
            "--current-source-mode", choices=("git-index", "directory"),
            default="directory",
        )
        command.add_argument("--current-components")
    propose_bundle.add_argument("--now", help="ISO-8601 timestamp for deterministic proposal IDs")
    compose_bundle.add_argument("--workspace-config-input", type=Path, required=True)
    compose_bundle.add_argument("--now", help="ISO-8601 timestamp for deterministic proposal IDs")
    return result


def emit(value: object) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False))


def component_selection(raw: str, field: str) -> list[str]:
    values = [item.strip() for item in raw.split(",")]
    if not values or any(not item for item in values):
        raise BundleError(f"{field}: must be a comma-separated component list")
    return values


def _bundle_main(args: argparse.Namespace) -> int:
    if args.bundle_command == "apply":
        root = args.target.absolute().resolve()
        proposal = args.proposal if args.proposal.is_absolute() else root / args.proposal
        if read_json(proposal).get("operation") != MATERIALIZE_OPERATION:
            raise BundleError("bundle apply accepts only materialization proposals")
        receipt_path, receipt = apply_proposal(
            root, proposal, args.confirm, args.runtime
        )
        emit({"receipt": receipt_path.relative_to(root).as_posix(), **receipt})
        return 0
    if args.bundle_command == "generate":
        lock = create_bundle_lock(
            args.source,
            name=args.name,
            version=args.bundle_version,
            source_mode="git-index",
        )
        print(json.dumps(lock, indent=2, ensure_ascii=False))
        return 0
    candidate = verify_bundle(
        args.lock, args.source, expected_sha256=args.expect_sha256,
        source_mode=args.source_mode,
        retain_paths=(),
    )
    if args.bundle_command == "check":
        emit({
            "schema_version": candidate.lock["schema_version"],
            "bundle": {"name": candidate.name, "version": candidate.version},
            "bundle_sha256": candidate.digest,
            "source_git_commit": candidate.lock["bundle"]["source_git_commit"],
            "files": len(candidate.records),
            "source_mode": candidate.source_mode,
            "executable_modes_verified": candidate.mode_verified,
            "unlocked_files_ignored": True,
            "writes": False,
        })
        return 0
    current_values = (
        args.current_lock,
        args.current_source,
        args.expect_current_sha256,
        args.current_components,
    )
    if any(value is not None for value in current_values) and not all(
        value is not None for value in current_values
    ):
        raise BundleError(
            "current_bundle: --current-lock, --current-source, "
            "--expect-current-sha256, and --current-components are all required together"
        )
    current = None
    current_components: list[str] = []
    if args.current_lock is not None:
        current = verify_bundle(
            args.current_lock,
            args.current_source,
            expected_sha256=args.expect_current_sha256,
            source_mode=args.current_source_mode,
            role="current",
            retain_paths=(),
        )
        current_components = component_selection(
            args.current_components, "current_components"
        )
    desired_components = component_selection(args.components, "components")
    if args.bundle_command == "compose":
        if current is not None or current_components:
            raise BundleError("compose: current bundle inputs are not allowed")
        proposal_path, proposal = create_composition_proposal(
            target_root=args.target,
            workspace_config_path=args.workspace_config,
            workspace_config_input_path=args.workspace_config_input,
            expected_config_input_sha256=args.expect_config_sha256,
            candidate=candidate,
            desired_components=desired_components,
            now=parse_now(args.now),
        )
        emit({
            "proposal": proposal_path.relative_to(args.target.absolute()).as_posix(),
            "proposal_id": proposal["proposal_id"],
            "proposal_digest": proposal["proposal_digest"],
            "plan_digest": proposal["authorization"]["plan"]["plan_digest"],
            "source_mode": candidate.source_mode,
            "source_git_commit": candidate.lock["bundle"]["source_git_commit"],
            "changes": [
                {
                    "action": change["action"],
                    "path": change["path"],
                    "owner": change["authorization"]["owner"],
                    "policy": change["authorization"]["policy"],
                    "before_sha256_raw": change["before_raw_sha256"],
                    "after_sha256_raw": change["after_raw_sha256"],
                    "summary": change["diff"].strip(),
                }
                for change in proposal["changes"]
            ],
            "writes": True,
            "applied": False,
        })
        return 0
    if args.bundle_command == "propose":
        proposal_path, proposal = create_materialization_proposal(
            target_root=args.target,
            workspace_config_path=args.workspace_config,
            expected_config_sha256=args.expect_config_sha256,
            candidate=candidate,
            desired_components=desired_components,
            current=current,
            current_components=current_components,
            now=parse_now(args.now),
        )
        emit({
            "proposal": proposal_path.relative_to(args.target.absolute()).as_posix(),
            "proposal_id": proposal["proposal_id"],
            "proposal_digest": proposal["proposal_digest"],
            "plan_digest": proposal["authorization"]["plan"]["plan_digest"],
            "source_mode": candidate.source_mode,
            "source_git_commit": candidate.lock["bundle"]["source_git_commit"],
            "changes": [
                {
                    "action": change["action"],
                    "path": change["path"],
                    "owner": change["authorization"]["owner"],
                    "policy": change["authorization"]["policy"],
                    "before_sha256_raw": change["before_raw_sha256"],
                    "after_sha256_raw": change["after_raw_sha256"],
                    "summary": change["diff"].strip(),
                }
                for change in proposal["changes"]
            ],
            "writes": True,
            "applied": False,
        })
        return 0
    plan = create_structural_plan(
        target_root=args.target,
        workspace_config_path=args.workspace_config,
        expected_config_sha256=args.expect_config_sha256,
        candidate=candidate,
        desired_components=desired_components,
        current=current,
        current_components=current_components,
    )
    emit({**plan, "writes": False})
    return 0


def selected_workspace_agents(args: argparse.Namespace, root: Path) -> list[str]:
    selections = args.agents if args.agents is not None else args.agent
    if len(selections) != 1:
        raise ContextOSError("workspace migration selection may be specified only once")
    raw_selection = selections[0]
    if args.agent is not None and "," in raw_selection:
        raise ContextOSError(
            "--agent is a deprecated singleton alias and accepts exactly one runtime id"
        )
    selected_agents = parse_agent_selection(
        raw_selection, known_runtime_ids=runtime_ids(root)
    )
    if selected_agents is None:
        raise ContextOSError("workspace migration requires explicit agents")
    return selected_agents


def workspace_proposal_report(
    root: Path,
    path: Path | None,
    document: dict[str, object] | None,
    notices: list[str],
) -> dict[str, object]:
    if path is None or document is None:
        return {
            "schema_version": 1,
            "writes": False,
            "action": "noop",
            "proposal": None,
            "proposal_id": None,
            "proposal_digest": None,
            "changes": [],
            "notices": notices,
        }
    changes = document["changes"]
    source_hashes = document["source_hashes"]
    assert isinstance(changes, list)
    assert isinstance(source_hashes, dict)
    return {
        "schema_version": document["schema_version"],
        "writes": False,
        "action": "proposed",
        "workflow": document["workflow"],
        "operation": document["operation"],
        "proposal": path.relative_to(root).as_posix(),
        "proposal_id": document["proposal_id"],
        "proposal_digest": document["proposal_digest"],
        "changes": [
            {
                "action": item["action"],
                "path": item["path"],
                "owner": item["authorization"]["owner"],
                "policy": item["authorization"]["policy"],
                "before_sha256_raw": item["before_raw_sha256"],
                "after_sha256_raw": item["after_raw_sha256"],
                "diff": item["diff"],
            }
            for item in changes
        ],
        "authorization_inputs": [
            {"path": source, "sha256_raw": digest}
            for source, digest in source_hashes.items()
        ],
        "source_git_head": document["source_git_head"],
        "authorization": document["authorization"],
        "notices": notices,
    }


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    hook_output: str | None = None
    try:
        if args.command == "bundle":
            return _bundle_main(args)
        root = discover_root(args.root)
        if args.command == "start":
            emit(start_report(root, parse_now(args.now)))
        elif args.command == "propose":
            path, document = create_proposal(root, args.workflow, read_json(args.input), parse_now(args.now))
            emit({
                "proposal": path.relative_to(root).as_posix(),
                "proposal_id": document["proposal_id"],
                "proposal_digest": document["proposal_digest"],
                "changes": [{"path": item["path"], "diff": item["diff"]} for item in document["changes"]],
            })
        elif args.command == "apply":
            proposal = args.proposal if args.proposal.is_absolute() else root / args.proposal
            receipt_path, receipt = apply_proposal(root, proposal, args.confirm, args.runtime)
            emit({"receipt": receipt_path.relative_to(root).as_posix(), **receipt})
        elif args.command == "install":
            path, manifest = install_runtime(root, args.runtime)
            relative = path.relative_to(root).as_posix()
            emit({"host_state": relative, "runtime_file": relative, **manifest})
        elif args.command == "agent":
            if args.agent_command == "list":
                emit(agent_list_report(root))
            else:
                enabled = args.agent_command in {"add", "enable"}
                path, document = create_agent_activation_proposal(
                    root, args.runtime, enabled, parse_now(args.now)
                )
                notices = [
                    "agent add is an alias for agent enable"
                    if args.agent_command == "add"
                    else "agent disable changes tracked intent only; bundled files remain"
                    if not enabled
                    else "agent enable changes tracked intent only",
                ]
                emit(workspace_proposal_report(root, path, document, notices))
        elif args.command == "doctor":
            report = doctor(root, args.runtime, all_runtimes=args.all)
            emit(report)
            return 1 if report["status"] == "fail" else 0
        elif args.command == "workspace":
            if args.workspace_command == "show":
                emit(workspace_resolution_report(root))
            elif args.workspace_command == "migrate":
                selected_agents = selected_workspace_agents(args, root)
                report = plan_workspace_migration(root, selected_agents)
                if args.agent is not None:
                    report["notices"].append(
                        "--agent is a deprecated singleton compatibility alias; use --agents"
                    )
                emit(report)
            elif args.workspace_command == "propose-migration":
                selected_agents = selected_workspace_agents(args, root)
                path, document = create_workspace_migration_proposal(
                    root, selected_agents, parse_now(args.now)
                )
                notices = []
                if args.agent is not None:
                    notices.append(
                        "--agent is a deprecated singleton compatibility alias; use --agents"
                    )
                emit(workspace_proposal_report(root, path, document, notices))
            elif args.workspace_command == "propose-setup":
                selected_agents = selected_workspace_agents(args, root)
                path, document = create_workspace_setup_proposal(
                    root, selected_agents, parse_now(args.now)
                )
                notices = [
                    "setup selection is additive and never removes configured agents"
                ]
                if args.agent is not None:
                    notices.append(
                        "--agent is a deprecated singleton compatibility alias; use --agents"
                    )
                emit(workspace_proposal_report(root, path, document, notices))
            elif args.workspace_command == "migrate-local-runtime":
                path, state, changed, migrated_runtime = migrate_legacy_runtime_state(root)
                emit({
                    "host_state": path.relative_to(root).as_posix(),
                    "changed": changed,
                    "migrated_runtime": migrated_runtime,
                    "legacy_runtime_retained": False,
                    **state,
                })
        elif args.command == "hook":
            hook_manifest = runtime_manifest(root, args.runtime, check_paths=False)
            surface_outputs = {
                surface.get("hook_output")
                for surface in hook_manifest["surfaces"].values()
            }
            if len(surface_outputs) == 1:
                hook_output = next(iter(surface_outputs))
            hook_output = runtime_surface(hook_manifest, args.surface).get("hook_output")
            raw = sys.stdin.read().strip()
            payload = json.loads(raw) if raw else {}
            if not isinstance(payload, dict):
                raise ContextOSError("hook input must be a JSON object")
            report = hook_report(root, args.event, payload)
            messages = [item["message"] for item in report["findings"]]
            rendered = render_hook_payload(hook_output, messages)
            if rendered is not None:
                emit(rendered)
        return 0
    except (
        ContextOSError,
        BundleError,
        WorkspaceConfigError,
        json.JSONDecodeError,
        OSError,
        UnicodeError,
    ) as exc:
        if getattr(args, "command", None) == "hook":
            message = f"Context OS advisory hook could not run: {exc}"
            # If no validated descriptor established a host protocol, silence
            # is safer than emitting another runtime's incompatible envelope.
            rendered = render_hook_payload(hook_output, [message])
            if rendered is not None:
                emit(rendered)
            return 0
        print(f"context-os: {exc}", file=sys.stderr)
        return 2
