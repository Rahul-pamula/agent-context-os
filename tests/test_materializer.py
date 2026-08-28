from __future__ import annotations

import io
import hashlib
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from contextos.bundle_schema import BundleError
from contextos.cli import main as cli_main
from contextos.kernel import (
    ContextOSError,
    _recover_pending_agent_journals,
    apply_proposal,
)
from contextos.materializer import (
    INSTALLED_STATE_PATH,
    create_composition_proposal,
    create_materialization_proposal,
)
try:
    from tests.test_bundle_lock import BundleFixture, workspace
except ModuleNotFoundError:
    from test_bundle_lock import BundleFixture, workspace


NOW = datetime(2026, 8, 28, 18, 0, tzinfo=timezone.utc)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class MaterializerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        current_root = self.root / "current-source"
        candidate_root = self.root / "candidate-source"
        current_root.mkdir()
        candidate_root.mkdir()
        self.current_fixture = BundleFixture(
            current_root,
            version="1.0.0",
            managed=b"binary\x00v1\n",
            addon=False,
        )
        self.candidate_fixture = BundleFixture(
            candidate_root,
            version="2.0.0",
            managed=b"binary\x00v2\n",
            addon=True,
        )
        self.current = self.current_fixture.verify(role="current")
        self.candidate = self.candidate_fixture.verify()
        self.target = self.root / "target"
        shutil.copytree(current_root, self.target)
        (self.target / "seed.txt").write_text("personal seed\n", encoding="utf-8")
        self.config = self.target / "contextos.workspace.json"
        self.config.write_text(
            json.dumps(workspace("fixture-template", "1.0.0"), indent=2) + "\n",
            encoding="utf-8",
        )

    def propose(self):
        return create_materialization_proposal(
            target_root=self.target,
            workspace_config_path=self.config,
            expected_config_sha256=digest(self.config),
            candidate=self.candidate,
            desired_components=["addon"],
            current=self.current,
            current_components=["core"],
            now=NOW,
        )

    def composition_input(self) -> tuple[Path, Path]:
        target = self.root / "clean-target"
        target.mkdir()
        config_input = self.root / "compose-workspace.json"
        config_input.write_text(
            json.dumps(workspace("fixture-template", "2.0.0"), indent=2) + "\n",
            encoding="utf-8",
        )
        return target, config_input

    def git_candidate(self, *, version: str, managed: bytes) -> BundleFixture:
        source = self.root / f"git-candidate-{version}"
        source.mkdir()
        return BundleFixture(
            source,
            version=version,
            managed=managed,
            addon=True,
            source_mode="git-index",
        )

    def compose(self, target: Path, config_input: Path):
        return create_composition_proposal(
            target_root=target,
            workspace_config_path=target / "contextos.workspace.json",
            workspace_config_input_path=config_input,
            expected_config_input_sha256=digest(config_input),
            candidate=self.candidate,
            desired_components=["addon"],
            now=NOW,
        )

    def test_clean_composition_installs_config_binary_components_and_state(self) -> None:
        target, config_input = self.composition_input()
        (target / "seed.txt").write_text("personal seed\n", encoding="utf-8")
        proposal_path, proposal = self.compose(target, config_input)

        _receipt_path, receipt = apply_proposal(
            target, proposal_path, proposal["proposal_digest"], "generic"
        )

        self.assertEqual(b"binary\x00v2\n", (target / "managed.bin").read_bytes())
        self.assertEqual("addon 2.0.0\n", (target / "addon.txt").read_text())
        self.assertEqual("personal seed\n", (target / "seed.txt").read_text())
        self.assertEqual(
            workspace("fixture-template", "2.0.0"),
            json.loads((target / "contextos.workspace.json").read_text()),
        )
        self.assertEqual("compose", proposal["authorization"]["mode"])
        self.assertEqual("component-materialize", receipt["operation"])

    def test_git_index_upgrade_uses_verified_blobs_not_smudged_worktree(self) -> None:
        fixture = self.git_candidate(version="3.0.0", managed=b"index binary\x00v3\n")
        candidate = fixture.verify()
        (fixture.root / "managed.bin").write_bytes(b"smudged working tree\r\n")
        (fixture.root / "addon.txt").write_text(
            "filtered working tree\n", encoding="utf-8"
        )

        proposal_path, proposal = create_materialization_proposal(
            target_root=self.target,
            workspace_config_path=self.config,
            expected_config_sha256=digest(self.config),
            candidate=candidate,
            desired_components=["addon"],
            current=self.current,
            current_components=["core"],
            now=NOW,
        )
        apply_proposal(
            self.target, proposal_path, proposal["proposal_digest"], "generic"
        )

        self.assertEqual(b"index binary\x00v3\n", (self.target / "managed.bin").read_bytes())
        self.assertEqual(
            candidate.verified_bytes["addon.txt"],
            (self.target / "addon.txt").read_bytes(),
        )

    def test_git_index_cli_compose_propose_and_apply_use_index_bytes(self) -> None:
        fixture = self.git_candidate(version="3.0.0", managed=b"index line\n")
        (fixture.root / "managed.bin").write_bytes(b"index line\r\n")
        target = self.root / "cli-compose-target"
        target.mkdir()
        config_input = self.root / "cli-compose-workspace.json"
        config_input.write_text(
            json.dumps(workspace("fixture-template", "3.0.0"), indent=2) + "\n",
            encoding="utf-8",
        )
        compose_output = io.StringIO()
        with redirect_stdout(compose_output):
            self.assertEqual(
                0,
                cli_main([
                    "bundle", "compose",
                    "--lock", str(fixture.lock_path),
                    "--source", str(fixture.root),
                    "--expect-sha256", fixture.lock["bundle_sha256"],
                    "--source-mode", "git-index",
                    "--target", str(target),
                    "--workspace-config", str(target / "contextos.workspace.json"),
                    "--workspace-config-input", str(config_input),
                    "--expect-config-sha256", digest(config_input),
                    "--components", "addon",
                    "--now", NOW.isoformat(),
                ]),
            )
        compose_report = json.loads(compose_output.getvalue())
        apply_output = io.StringIO()
        with redirect_stdout(apply_output):
            self.assertEqual(
                0,
                cli_main([
                    "bundle", "apply",
                    "--target", str(target),
                    "--proposal", compose_report["proposal"],
                    "--confirm", compose_report["proposal_digest"],
                ]),
            )
        self.assertEqual("component-materialize", json.loads(apply_output.getvalue())["operation"])
        self.assertEqual(b"index line\n", (target / "managed.bin").read_bytes())

        propose_output = io.StringIO()
        with redirect_stdout(propose_output):
            self.assertEqual(
                0,
                cli_main([
                    "bundle", "propose",
                    "--lock", str(fixture.lock_path),
                    "--source", str(fixture.root),
                    "--expect-sha256", fixture.lock["bundle_sha256"],
                    "--source-mode", "git-index",
                    "--target", str(self.target),
                    "--workspace-config", str(self.config),
                    "--expect-config-sha256", digest(self.config),
                    "--components", "addon",
                    "--current-lock", str(self.current_fixture.lock_path),
                    "--current-source", str(self.current_fixture.root),
                    "--expect-current-sha256", self.current_fixture.lock["bundle_sha256"],
                    "--current-source-mode", "directory",
                    "--current-components", "core",
                    "--now", NOW.isoformat(),
                ]),
            )
        self.assertTrue(json.loads(propose_output.getvalue())["proposal"].endswith(".json"))

    def test_bundle_apply_rejects_non_materialization_proposal(self) -> None:
        proposal = self.target / "not-materialization.json"
        proposal.write_text('{"operation":"agent-config"}\n', encoding="utf-8")
        errors = io.StringIO()
        with redirect_stderr(errors):
            status = cli_main([
                "bundle", "apply", "--target", str(self.target),
                "--proposal", str(proposal), "--confirm", "unused",
            ])
        self.assertEqual(2, status)
        self.assertIn("only materialization proposals", errors.getvalue())

    def test_clean_composition_rejects_managed_collision(self) -> None:
        target, config_input = self.composition_input()
        (target / "managed.bin").write_bytes(b"local\n")
        with self.assertRaisesRegex(BundleError, "collides with a planned add"):
            self.compose(target, config_input)

    def test_clean_composition_requires_the_canonical_workspace_marker(self) -> None:
        target, config_input = self.composition_input()
        with self.assertRaisesRegex(BundleError, "must equal target_root"):
            create_composition_proposal(
                target_root=target,
                workspace_config_path=target / "notes/workspace.json",
                workspace_config_input_path=config_input,
                expected_config_input_sha256=digest(config_input),
                candidate=self.candidate,
                desired_components=["addon"],
                now=NOW,
            )

    def test_clean_composition_rejects_stale_installed_state_during_proposal(self) -> None:
        target, config_input = self.composition_input()
        state_path = target / INSTALLED_STATE_PATH
        state_path.parent.mkdir()
        state_path.write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(BundleError, "explicit current bundle"):
            self.compose(target, config_input)

    def test_clean_composition_input_drift_fails_without_target_writes(self) -> None:
        target, config_input = self.composition_input()
        proposal_path, proposal = self.compose(target, config_input)
        config_input.write_text("{}\n", encoding="utf-8")

        with self.assertRaisesRegex(BundleError, "became stale"):
            apply_proposal(
                target, proposal_path, proposal["proposal_digest"], "generic"
            )

        self.assertFalse((target / "managed.bin").exists())
        self.assertFalse((target / "contextos.workspace.json").exists())

    def test_binary_upgrade_add_and_installed_state_use_shared_apply(self) -> None:
        proposal_path, proposal = self.propose()
        receipt_path, receipt = apply_proposal(
            self.target, proposal_path, proposal["proposal_digest"], "generic"
        )

        self.assertEqual(b"binary\x00v2\n", (self.target / "managed.bin").read_bytes())
        self.assertEqual("addon 2.0.0\n", (self.target / "addon.txt").read_text())
        self.assertEqual("personal seed\n", (self.target / "seed.txt").read_text())
        config = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual("2.0.0", config["template"]["version"])
        installed = json.loads(
            (self.target / INSTALLED_STATE_PATH).read_text(encoding="utf-8")
        )
        self.assertEqual(self.candidate.digest, installed["bundle"]["sha256"])
        self.assertEqual(["core", "addon"], installed["components"])
        self.assertEqual(
            proposal["authorization"]["plan"]["plan_digest"],
            installed["plan_digest"],
        )
        self.assertTrue(receipt_path.is_file())
        self.assertEqual("component-materialize", receipt["operation"])
        self.assertEqual(proposal["invariants"], receipt["invariants_checked"])

    def test_candidate_drift_after_proposal_fails_without_target_writes(self) -> None:
        proposal_path, proposal = self.propose()
        before = {
            path.relative_to(self.target).as_posix(): path.read_bytes()
            for path in self.target.rglob("*")
            if path.is_file() and ".context-os" not in path.parts
        }
        (self.candidate.root / "managed.bin").write_bytes(b"tampered\n")
        with self.assertRaisesRegex((BundleError, ContextOSError), "raw bytes"):
            apply_proposal(
                self.target, proposal_path, proposal["proposal_digest"], "generic"
            )
        after = {
            path.relative_to(self.target).as_posix(): path.read_bytes()
            for path in self.target.rglob("*")
            if path.is_file() and ".context-os" not in path.parts
        }
        self.assertEqual(before, after)

    def test_failure_after_binary_publication_rolls_back_replace_and_add(self) -> None:
        proposal_path, proposal = self.propose()
        original_publish = __import__(
            "contextos.kernel", fromlist=["_publish_exclusive"]
        )._publish_exclusive
        publications = 0
        injected = False

        def fail_after_binary_replace(source: Path, destination: Path):
            nonlocal publications, injected
            result = original_publish(source, destination)
            if (
                destination.is_relative_to(self.target.resolve())
                and ".context-os" not in destination.parts
            ):
                publications += 1
                if destination.name == "managed.bin" and not injected:
                    injected = True
                    raise OSError("injected materialization failure")
            return result

        with mock.patch(
            "contextos.kernel._publish_exclusive", side_effect=fail_after_binary_replace
        ), self.assertRaisesRegex(ContextOSError, "rolled back"):
            apply_proposal(
                self.target, proposal_path, proposal["proposal_digest"], "generic"
            )
        self.assertEqual(b"binary\x00v1\n", (self.target / "managed.bin").read_bytes())
        self.assertFalse((self.target / "addon.txt").exists())
        self.assertEqual("1.0.0", json.loads(self.config.read_text())["template"]["version"])
        self.assertFalse((self.target / INSTALLED_STATE_PATH).exists())
        self.assertGreater(publications, 1)

    def test_materialized_component_removal_preserves_seed_and_local_state(self) -> None:
        proposal_path, proposal = self.propose()
        apply_proposal(
            self.target, proposal_path, proposal["proposal_digest"], "generic"
        )
        next_root = self.root / "next-source"
        next_root.mkdir()
        next_fixture = BundleFixture(
            next_root,
            version="3.0.0",
            managed=b"binary\x00v3\n",
            addon=False,
        )
        next_candidate = next_fixture.verify()
        current = self.candidate_fixture.verify(role="current")
        next_path, next_proposal = create_materialization_proposal(
            target_root=self.target,
            workspace_config_path=self.config,
            expected_config_sha256=digest(self.config),
            candidate=next_candidate,
            desired_components=["core"],
            current=current,
            current_components=["addon"],
            now=NOW.replace(minute=1),
        )
        apply_proposal(
            self.target,
            next_path,
            next_proposal["proposal_digest"],
            "generic",
        )
        self.assertFalse((self.target / "addon.txt").exists())
        self.assertEqual(b"binary\x00v3\n", (self.target / "managed.bin").read_bytes())
        self.assertEqual("personal seed\n", (self.target / "seed.txt").read_text())
        installed = json.loads(
            (self.target / INSTALLED_STATE_PATH).read_text(encoding="utf-8")
        )
        self.assertEqual(["core"], installed["components"])
        self.assertEqual("3.0.0", installed["bundle"]["version"])

    def test_upgrade_rejects_installed_state_that_contradicts_current_components(self) -> None:
        proposal_path, proposal = self.propose()
        apply_proposal(
            self.target, proposal_path, proposal["proposal_digest"], "generic"
        )
        state_path = self.target / INSTALLED_STATE_PATH
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["components"] = ["core"]
        state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

        with self.assertRaisesRegex(BundleError, "contradicts current_components"):
            create_materialization_proposal(
                target_root=self.target,
                workspace_config_path=self.config,
                expected_config_sha256=digest(self.config),
                candidate=self.candidate_fixture.verify(),
                desired_components=["addon"],
                current=self.candidate_fixture.verify(role="current"),
                current_components=["addon"],
                now=NOW.replace(minute=2),
            )

    @unittest.skipUnless(os.name == "nt", "Windows case alias control")
    def test_upgrade_normalizes_case_aliased_config_source_key(self) -> None:
        proposal_path, proposal = create_materialization_proposal(
            target_root=Path(str(self.target).lower()),
            workspace_config_path=Path(str(self.config).lower()),
            expected_config_sha256=digest(self.config),
            candidate=self.candidate,
            desired_components=["addon"],
            current=self.current,
            current_components=["core"],
            now=NOW,
        )

        apply_proposal(
            self.target, proposal_path, proposal["proposal_digest"], "generic"
        )
        self.assertEqual(b"binary\x00v2\n", (self.target / "managed.bin").read_bytes())

    def test_committed_materialization_journal_recovers_without_source_policy_widening(self) -> None:
        proposal_path, proposal = self.propose()
        with mock.patch(
            "contextos.kernel._discard_agent_journal",
            side_effect=OSError("retain committed journal"),
        ):
            receipt_path, _receipt = apply_proposal(
                self.target, proposal_path, proposal["proposal_digest"], "generic"
            )
        journal = self.target / ".context-os/journals" / proposal["proposal_id"]
        self.assertTrue(receipt_path.is_file())
        self.assertTrue(journal.is_dir())

        _recover_pending_agent_journals(self.target)

        self.assertFalse(journal.exists())
        self.assertEqual(b"binary\x00v2\n", (self.target / "managed.bin").read_bytes())
