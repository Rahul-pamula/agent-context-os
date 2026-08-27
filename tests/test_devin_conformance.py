from __future__ import annotations

import json
import os
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DESCRIPTOR = json.loads((ROOT / "runtimes/devin.json").read_text(encoding="utf-8"))
GUIDE_PATH = ROOT / "adapters/devin/README.md"


class DevinDescriptorTest(unittest.TestCase):
    def test_session_and_review_remain_separate_and_unversioned(self) -> None:
        self.assertEqual("experimental", DESCRIPTOR["support_tier"])
        self.assertEqual({"session", "review"}, set(DESCRIPTOR["surfaces"]))
        session = DESCRIPTOR["surfaces"]["session"]
        review = DESCRIPTOR["surfaces"]["review"]
        self.assertEqual("cloud", session["kind"])
        self.assertEqual("review", review["kind"])
        self.assertEqual("experimental", session["support_tier"])
        self.assertEqual("compatibility", review["support_tier"])
        self.assertEqual([], DESCRIPTOR["evidence"]["tested_versions"])
        self.assertEqual([], session["binary_probes"])
        self.assertEqual([], review["binary_probes"])

    def test_session_uses_only_repository_native_contract_files(self) -> None:
        session = DESCRIPTOR["surfaces"]["session"]
        self.assertEqual(
            ["AGENTS.md"],
            [source["path"] for source in session["instruction_sources"]],
        )
        self.assertEqual(
            [".agents/skills"],
            [source["path"] for source in session["skill_sources"]],
        )
        self.assertEqual(
            {name: f"@skills:context-{name}" for name in ("setup", "start", "update", "end")},
            session["invocation"],
        )
        self.assertEqual("native", session["capabilities"]["agent_skills"])
        self.assertEqual("advisory", session["capabilities"]["explicit_invocation"])

    def test_review_does_not_inherit_session_lifecycle_or_skills(self) -> None:
        review = DESCRIPTOR["surfaces"]["review"]
        self.assertEqual([], review["skill_sources"])
        self.assertTrue(all(value is None for value in review["invocation"].values()))
        self.assertEqual(
            {"AGENTS.md"},
            {source["path"] for source in review["instruction_sources"]},
        )
        self.assertTrue(
            all(value == "unsupported" for value in review["capabilities"].values())
        )
        self.assertNotIn("devin-skills", review["evidence"])

    def test_no_managed_account_state_is_encoded_as_a_repo_artifact(self) -> None:
        serialized = json.dumps(DESCRIPTOR).lower()
        for forbidden in (".devin/", "blueprint.yaml", "memory.md"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, serialized)
        paths = {
            source["path"]
            for surface in DESCRIPTOR["surfaces"].values()
            for field in ("instruction_sources", "skill_sources")
            for source in surface[field]
        }
        self.assertEqual({"AGENTS.md", ".agents/skills"}, paths)

    def test_unsupported_host_features_are_not_claimed(self) -> None:
        for surface_name, surface in DESCRIPTOR["surfaces"].items():
            with self.subTest(surface=surface_name):
                self.assertEqual("unsupported", surface["capabilities"]["project_hooks"])
                self.assertEqual(
                    "unsupported", surface["capabilities"]["blocking_pre_tool_hook"]
                )
                self.assertEqual("unsupported", surface["capabilities"]["native_memory"])
                self.assertEqual("unsupported", surface["capabilities"]["skill_allowlists"])
                self.assertIsNone(surface["hook_output"])

    def test_guide_preserves_repo_account_and_data_transfer_boundaries(self) -> None:
        guide = " ".join(GUIDE_PATH.read_text(encoding="utf-8").split())
        for required in (
            "Git-based blueprints are not currently supported",
            "Context OS ships no `.devin/` file or blueprint YAML",
            "That means only \"selected for this workspace.\"",
            "does not certify the Devin account",
            "Secrets are injected by Devin rather than committed here",
            "can persist it in the snapshot",
            "Devin Review is not a session",
            "sends the diff and file contents to Devin servers",
            "local git access for the Review CLI does not prove Devin account access",
            "documentation or local registration alone must never turn them green",
            "Review needs its own fixtures",
        ):
            with self.subTest(required=required):
                self.assertIn(required, guide)

    def test_adapter_does_not_ship_fake_devin_configuration(self) -> None:
        if os.environ.get("CONTEXTOS_VALIDATION_PROFILE") == "workspace":
            self.skipTest("workspace-owned future host configuration is outside maintainer inventory")
        for path in (".devin", "devin.yaml", "devin.yml", "blueprint.yaml"):
            with self.subTest(path=path):
                self.assertFalse((ROOT / path).exists())


@unittest.skipUnless(
    os.environ.get("CONTEXTOS_DEVIN_LIVE_ACCOUNT") == "1",
    "set CONTEXTOS_DEVIN_LIVE_ACCOUNT=1 only in a dedicated synthetic account fixture",
)
class DevinLiveAccountGate(unittest.TestCase):
    def test_live_account_fixture_is_not_yet_implemented(self) -> None:
        self.fail(
            "live Devin conformance is intentionally unimplemented; do not mark the account verified"
        )


if __name__ == "__main__":
    unittest.main()
