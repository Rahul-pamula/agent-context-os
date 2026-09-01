from __future__ import annotations

import json
import io
import os
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from contextlib import redirect_stdout

from contextos.attachment import resolve_root_roles
from contextos.kernel import (
    ContextOSError,
    LOCAL_BINDING_MODE,
    PROJECT_BINDING_PATH,
    apply_proposal,
    canonical_json,
    create_proposal,
    create_project_attachment_proposal,
    hook_report,
    load_project_attachment,
    start_report,
    sha256_text,
)
from contextos.workspace_schema import render_workspace_config
from contextos.cli import main as cli_main


KERNEL_ROOT = Path(__file__).resolve().parents[1]
NOW = datetime.fromisoformat("2026-08-31T10:00:00-07:00")


def git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-c", f"safe.directory={root}", "-C", str(root), *arguments],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


def initialize_repository(root: Path, files: dict[str, str]) -> None:
    root.mkdir()
    git(root, "init", "--quiet")
    git(root, "config", "user.name", "Context OS Test")
    git(root, "config", "user.email", "context-os@example.invalid")
    git(root, "config", "core.autocrlf", "false")
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "--quiet", "-m", "fixture")


def tree_snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    return {
        path.relative_to(root).as_posix(): (
            path.read_bytes(),
            path.stat().st_mode & 0o7777,
        )
        for path in root.rglob("*")
        if path.is_file()
        and ".context-os" not in path.relative_to(root).parts
        and "__pycache__" not in path.relative_to(root).parts
    }


class AttachmentLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name).resolve()
        self.context_root = base / "context"
        self.working_root = base / "working"
        workspace = render_workspace_config({
            "schema_version": 1,
            "mode": "full-template",
            "agents": [],
            "paths": {
                "state_dir": "state",
                "sessions_dir": "sessions",
                "task_file": "TODO.md",
            },
            "template": {"version": "0.12.0", "source": "test"},
        })
        initialize_repository(
            self.context_root,
            {
                "contextos.workspace.json": workspace,
                "state/current.md": "**Last Updated:** 2026-08-31\n\n# Current\n",
                "state/current-log.md": "# Current log\n",
                "TODO.md": "# Tasks\n",
                ".gitignore": ".context-os/\n",
            },
        )
        initialize_repository(
            self.working_root,
            {"app.txt": "ordinary application bytes\n"},
        )
        self.roles = resolve_root_roles(
            kernel_root=KERNEL_ROOT,
            context_root=self.context_root,
            working_root=self.working_root,
        )

    def test_attach_apply_start_and_hook_keep_working_root_read_only(self) -> None:
        before = tree_snapshot(self.working_root)
        kernel_before = tree_snapshot(KERNEL_ROOT)
        proposal_path, proposal = create_project_attachment_proposal(
            self.roles, "sample-app", NOW
        )
        receipt_path, receipt = apply_proposal(
            self.context_root,
            proposal_path,
            proposal["proposal_digest"],
            "generic",
            roles=self.roles,
        )
        self.assertTrue(receipt_path.is_file())
        self.assertEqual(receipt["operation"], "project-attach")
        self.assertEqual(tree_snapshot(self.working_root), before)
        manifest, binding = load_project_attachment(self.roles)
        self.assertEqual(manifest["project_id"], "sample-app")
        self.assertIn("sample-app", binding["bindings"])
        self.assertTrue((self.context_root / PROJECT_BINDING_PATH).is_file())

        report = start_report(self.context_root, NOW, roles=self.roles)
        self.assertEqual(report["project"]["project_id"], "sample-app")
        self.assertEqual(
            report["working_repository"]["status"]["entries"],
            [],
            report["working_repository"]["status"],
        )
        self.assertTrue(report["working_repository"]["history"])
        self.assertEqual(tree_snapshot(self.working_root), before)

        relative = hook_report(
            self.context_root,
            "pre-write",
            {"file_path": "state/current.md"},
            roles=self.roles,
        )
        absolute = hook_report(
            self.context_root,
            "pre-write",
            {"file_path": str(self.context_root / "state/current.md")},
            roles=self.roles,
        )
        self.assertEqual(relative["findings"], [])
        self.assertEqual(len(absolute["findings"]), 1)
        self.assertEqual(tree_snapshot(KERNEL_ROOT), kernel_before)

    def test_rebind_is_a_second_digest_bound_transaction(self) -> None:
        proposal_path, proposal = create_project_attachment_proposal(
            self.roles, "sample-app", NOW
        )
        apply_proposal(
            self.context_root,
            proposal_path,
            proposal["proposal_digest"],
            "generic",
            roles=self.roles,
        )
        if os.name != "nt":
            os.chmod(self.context_root / PROJECT_BINDING_PATH, 0o644)
        later = datetime.fromisoformat("2026-08-31T11:00:00-07:00")
        rebind_path, rebind = create_project_attachment_proposal(
            self.roles, "sample-app", later, rebind=True
        )
        _, receipt = apply_proposal(
            self.context_root,
            rebind_path,
            rebind["proposal_digest"],
            "generic",
            roles=self.roles,
        )
        self.assertEqual(receipt["operation"], "project-rebind")
        registry = json.loads((self.context_root / PROJECT_BINDING_PATH).read_text())
        self.assertEqual(
            registry["bindings"]["sample-app"]["bound_at"], later.isoformat()
        )
        self.assertEqual(
            (self.context_root / PROJECT_BINDING_PATH).stat().st_mode & 0o7777,
            LOCAL_BINDING_MODE,
        )

    def test_cli_attach_apply_and_start_use_exact_roles(self) -> None:
        roots = [
            "--kernel-root", str(KERNEL_ROOT),
            "--context-root", str(self.context_root),
            "--working-root", str(self.working_root),
        ]
        output = io.StringIO()
        with redirect_stdout(output):
            result = cli_main([
                *roots,
                "project", "attach", "--id", "cli-app",
                "--now", NOW.isoformat(),
            ])
        self.assertEqual(result, 0)
        proposed = json.loads(output.getvalue())
        output = io.StringIO()
        with redirect_stdout(output):
            result = cli_main([
                *roots,
                "apply", proposed["proposal"],
                "--confirm", proposed["proposal_digest"],
                "--runtime", "generic",
            ])
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue())["operation"], "project-attach")
        output = io.StringIO()
        with redirect_stdout(output):
            result = cli_main([*roots, "start", "--now", NOW.isoformat()])
        self.assertEqual(result, 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["project"]["project_id"], "cli-app")
        self.assertEqual(report["root_roles"]["working_root"], str(self.working_root))

    def test_golden_claude_to_codex_handoff_stays_in_context_root(self) -> None:
        before = tree_snapshot(self.working_root)
        attach_path, attach = create_project_attachment_proposal(
            self.roles, "handoff-app", NOW
        )
        apply_proposal(
            self.context_root,
            attach_path,
            attach["proposal_digest"],
            "generic",
            roles=self.roles,
        )
        update_time = datetime.fromisoformat("2026-08-31T10:15:00-07:00")
        update_path, update = create_proposal(
            self.context_root,
            "update",
            {"progress": ["Claude recorded the implementation checkpoint"]},
            update_time,
        )
        _, claude_receipt = apply_proposal(
            self.context_root,
            update_path,
            update["proposal_digest"],
            "claude",
            roles=self.roles,
        )
        self.assertEqual(claude_receipt["runtime"], "claude")

        end_time = datetime.fromisoformat("2026-08-31T10:30:00-07:00")
        end_path, end = create_proposal(
            self.context_root,
            "end",
            {
                "what_happened": ["Codex resumed from Claude's durable checkpoint"],
                "decisions": [],
                "next_time": ["Continue in the attached application"],
            },
            end_time,
        )
        _, codex_receipt = apply_proposal(
            self.context_root,
            end_path,
            end["proposal_digest"],
            "codex",
            roles=self.roles,
        )
        self.assertEqual(codex_receipt["runtime"], "codex")
        session = (self.context_root / "sessions/2026-08-31.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("Claude recorded the implementation checkpoint", session)
        self.assertIn("Codex resumed from Claude's durable checkpoint", session)
        self.assertEqual(tree_snapshot(self.working_root), before)

    def test_resigned_project_proposal_cannot_escape_attachment_allowlist(self) -> None:
        before = tree_snapshot(self.working_root)
        proposal_path, proposal = create_project_attachment_proposal(
            self.roles, "tamper-app", NOW
        )
        proposal["changes"][0]["path"] = "../working/owned.txt"
        unsigned = dict(proposal)
        unsigned.pop("proposal_digest")
        proposal["proposal_digest"] = sha256_text(canonical_json(unsigned))
        proposal_path.write_text(
            json.dumps(proposal, indent=2) + "\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(ContextOSError, "invalid ordered path set"):
            apply_proposal(
                self.context_root,
                proposal_path,
                proposal["proposal_digest"],
                "generic",
                roles=self.roles,
            )
        self.assertEqual(tree_snapshot(self.working_root), before)

    def test_apply_rejects_explicit_roles_different_from_proposal(self) -> None:
        proposal_path, proposal = create_project_attachment_proposal(
            self.roles, "role-app", NOW
        )
        alternate = self.working_root.parent / "alternate-working"
        subprocess.run(
            ["git", "clone", "--quiet", str(self.working_root), str(alternate)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        alternate_roles = resolve_root_roles(
            kernel_root=KERNEL_ROOT,
            context_root=self.context_root,
            working_root=alternate,
        )
        with self.assertRaisesRegex(ContextOSError, "do not match explicit apply roles"):
            apply_proposal(
                self.context_root,
                proposal_path,
                proposal["proposal_digest"],
                "generic",
                roles=alternate_roles,
            )
        self.assertFalse((self.context_root / PROJECT_BINDING_PATH).exists())

    def test_apply_reasserts_canonical_context_configuration(self) -> None:
        before = tree_snapshot(self.working_root)
        proposal_path, proposal = create_project_attachment_proposal(
            self.roles, "config-app", NOW
        )
        config_path = self.context_root / "contextos.workspace.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config_path.write_text(
            json.dumps(config, separators=(",", ":")), encoding="utf-8"
        )
        with self.assertRaisesRegex(
            ContextOSError, "requires canonical contextos.workspace.json"
        ):
            apply_proposal(
                self.context_root,
                proposal_path,
                proposal["proposal_digest"],
                "generic",
                roles=self.roles,
            )
        self.assertFalse((self.context_root / PROJECT_BINDING_PATH).exists())
        self.assertEqual(tree_snapshot(self.working_root), before)

    def test_attachment_read_rejects_linked_local_binding(self) -> None:
        proposal_path, proposal = create_project_attachment_proposal(
            self.roles, "linked-binding-app", NOW
        )
        apply_proposal(
            self.context_root,
            proposal_path,
            proposal["proposal_digest"],
            "generic",
            roles=self.roles,
        )
        binding_path = self.context_root / PROJECT_BINDING_PATH
        external = self.context_root.parent / "external-binding.json"
        external.write_bytes(binding_path.read_bytes())
        binding_path.unlink()
        try:
            binding_path.symlink_to(external)
        except OSError:
            self.skipTest("file symlink creation is unavailable")
        with self.assertRaisesRegex(ContextOSError, "symlink or reparse point"):
            load_project_attachment(self.roles)


if __name__ == "__main__":
    unittest.main()
