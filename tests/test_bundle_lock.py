from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from contextos.bundle_schema import (
    BundleError,
    bundle_schema_document,
    canonical_json,
    create_bundle_lock,
    create_structural_plan,
    validate_bundle_lock,
    verify_bundle,
    _git_index,
    _safe_path,
)
from contextos.kernel import canonical_json as kernel_canonical_json
from contextos.kernel import git_head
from contextos.primitives import (
    SnapshotError,
    canonical_json as primitive_canonical_json,
    git_repository_identity,
)


ROOT = Path(__file__).resolve().parents[1]


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def workspace(source: str, version: str) -> dict:
    return {
        "schema_version": 1,
        "mode": "full-template",
        "agents": ["codex"],
        "paths": {
            "state_dir": "state",
            "sessions_dir": "sessions",
            "task_file": "TODO.md",
        },
        "template": {"source": source, "version": version},
    }


class BundleFixture:
    def __init__(
        self, root: Path, *, version: str, managed: bytes, addon: bool,
        runtime_addon: bool | None = None, include_seed: bool = True,
        addon_policy: str = "managed",
    ) -> None:
        self.root = root
        (root / "components").mkdir(parents=True)
        (root / "runtimes").mkdir()
        (root / "dev").mkdir()
        (root / "managed.bin").write_bytes(managed)
        (root / "seed.txt").write_bytes(b"seed\n")
        if runtime_addon is None:
            runtime_addon = addon
        runtime_components = ["core", "addon"] if runtime_addon else ["core"]
        runtime_descriptor = json.loads(
            (ROOT / "runtimes/codex.json").read_text(encoding="utf-8")
        )
        runtime_descriptor["components"] = runtime_components
        (root / "runtimes/codex.json").write_text(
            json.dumps(runtime_descriptor, indent=2) + "\n",
            encoding="utf-8",
        )
        (root / "dev/test.txt").write_text("not shipped\n", encoding="utf-8")
        components = [
            {
                "id": "core",
                "description": "Core fixture.",
                "depends_on": [],
                "paths": [
                    {"path": "components/manifest.json", "policy": "managed"},
                    {"path": "managed.bin", "policy": "managed"},
                    {"path": "runtimes/codex.json", "policy": "managed"},
                    {"path": "dev/test.txt", "policy": "development"},
                ],
            }
        ]
        if include_seed:
            components[0]["paths"].append({"path": "seed.txt", "policy": "seed"})
        if addon:
            (root / "addon.txt").write_text(f"addon {version}\n", encoding="utf-8")
            components.append({
                "id": "addon",
                "description": "Optional fixture.",
                "depends_on": ["core"],
                "paths": [{"path": "addon.txt", "policy": addon_policy}],
            })
        manifest = {
            "schema_version": 1,
            "extensible_paths": ["contextos.workspace.json"],
            "extensible_roots": ["state"],
            "components": components,
        }
        (root / "components/manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        self.lock = create_bundle_lock(
            root, name="fixture-template", version=version, source_mode="directory"
        )
        self.lock_path = root.parent / f"lock-{version}.json"
        self.lock_path.write_text(json.dumps(self.lock, indent=2) + "\n", encoding="utf-8")

    def verify(self, *, role: str = "candidate"):
        return verify_bundle(
            self.lock_path,
            self.root,
            expected_sha256=self.lock["bundle_sha256"],
            source_mode="directory",
            role=role,
        )


class BundleLockTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.fixture = BundleFixture(
            self.source, version="1.0.0", managed=b"binary\x00v1\r\n", addon=True
        )

    def test_lock_is_deterministic_digest_bound_and_excludes_development(self) -> None:
        repeated = create_bundle_lock(
            self.source,
            name="fixture-template",
            version="1.0.0",
            source_mode="directory",
        )
        self.assertEqual(self.fixture.lock, repeated)
        self.assertEqual(
            self.fixture.lock["bundle_sha256"],
            hashlib.sha256(
                canonical_json(self.fixture.lock["bundle"]).encode("utf-8")
            ).hexdigest(),
        )
        paths = [item["path"] for item in self.fixture.lock["bundle"]["files"]]
        self.assertIn("managed.bin", paths)
        self.assertNotIn("dev/test.txt", paths)

    def test_kernel_and_bundle_share_one_canonical_digest_primitive(self) -> None:
        self.assertIs(primitive_canonical_json, canonical_json)
        self.assertIs(primitive_canonical_json, kernel_canonical_json)
        fixture = {"z": "caf\u00e9", "a": {"b": [2, 1]}}
        self.assertEqual(
            '{"a":{"b":[2,1]},"z":"caf\u00e9"}', canonical_json(fixture)
        )

    def test_checked_in_schema_matches_authoritative_contract(self) -> None:
        self.assertEqual(
            bundle_schema_document(),
            json.loads((ROOT / "bundles/schema.json").read_text(encoding="utf-8")),
        )

    def test_expected_digest_and_every_source_byte_are_required(self) -> None:
        with self.assertRaisesRegex(BundleError, "expected_sha256"):
            verify_bundle(
                self.fixture.lock_path, self.source,
                expected_sha256="0" * 64, source_mode="directory",
            )
        original = (self.source / "managed.bin").read_bytes()
        (self.source / "managed.bin").write_bytes(original + b"changed")
        with self.assertRaisesRegex(BundleError, "raw bytes"):
            self.fixture.verify()
        (self.source / "managed.bin").write_bytes(original)
        self.assertEqual(self.fixture.lock["bundle_sha256"], self.fixture.verify().digest)

    def test_closed_schema_portable_paths_and_digest_tampering_fail(self) -> None:
        mutations = []
        unknown = copy.deepcopy(self.fixture.lock)
        unknown["surprise"] = True
        mutations.append((unknown, "unknown surprise"))
        bad_path = copy.deepcopy(self.fixture.lock)
        bad_path["bundle"]["files"][0]["path"] = "../escape"
        mutations.append((bad_path, "canonical lexical path"))
        duplicate = copy.deepcopy(self.fixture.lock)
        duplicate["bundle"]["files"][1]["path"] = duplicate["bundle"]["files"][0]["path"].upper()
        mutations.append((duplicate, "portable path collision"))
        bad_version = copy.deepcopy(self.fixture.lock)
        bad_version["bundle"]["version"] = "latest"
        mutations.append((bad_version, "exact offline version"))
        tampered = copy.deepcopy(self.fixture.lock)
        tampered["bundle"]["name"] = "other-template"
        mutations.append((tampered, "does not match bundle payload"))
        for value, message in mutations:
            with self.subTest(message=message), self.assertRaisesRegex(BundleError, message):
                validate_bundle_lock(value)

    def test_symlink_sources_fail_closed(self) -> None:
        source = self.source / "managed.bin"
        replacement = self.root / "replacement.bin"
        replacement.write_bytes(source.read_bytes())
        source.unlink()
        try:
            source.symlink_to(replacement)
        except OSError:
            self.skipTest("symlink creation unavailable")
        with self.assertRaisesRegex(BundleError, "link-like"):
            self.fixture.verify()

    def test_hardlink_sources_fail_closed(self) -> None:
        source = self.source / "managed.bin"
        replacement = self.root / "replacement.bin"
        replacement.write_bytes(source.read_bytes())
        source.unlink()
        try:
            os.link(replacement, source)
        except OSError:
            self.skipTest("hard-link creation unavailable")
        with self.assertRaisesRegex(BundleError, "multiply linked"):
            self.fixture.verify()

    def test_schema_rejects_file_descendant_conflicts(self) -> None:
        value = copy.deepcopy(self.fixture.lock)
        value["bundle"]["files"][1]["path"] = value["bundle"]["files"][0]["path"] + "/child"
        value["bundle"]["files"] = sorted(
            value["bundle"]["files"], key=lambda item: item["path"].casefold()
        )
        value["bundle_sha256"] = hashlib.sha256(
            canonical_json(value["bundle"]).encode("utf-8")
        ).hexdigest()
        with self.assertRaisesRegex(BundleError, "file/descendant"):
            validate_bundle_lock(value)

    def test_git_index_disables_executable_and_network_configuration(self) -> None:
        completed = subprocess.CompletedProcess([], 0, stdout=b"", stderr=b"")
        with mock.patch.dict(os.environ, {"GIT_DIR": "redirected"}):
            with mock.patch("subprocess.run", return_value=completed) as run:
                self.assertEqual({}, _git_index(self.source))
        command = run.call_args.args[0]
        environment = run.call_args.kwargs["env"]
        self.assertEqual(
            [
                "git", "-c", f"safe.directory={self.source.absolute()}",
                "-c", "core.fsmonitor=false", "-C", str(self.source.absolute()),
                "ls-files", "--stage", "-z", "--",
            ],
            command,
        )
        self.assertNotIn("GIT_DIR", environment)
        self.assertEqual("1", environment["GIT_NO_LAZY_FETCH"])
        self.assertEqual("1", environment["GIT_NO_REPLACE_OBJECTS"])
        self.assertEqual("0", environment["GIT_OPTIONAL_LOCKS"])

    def test_git_identity_disables_repository_fsmonitor_for_every_command(self) -> None:
        (self.source / ".git").mkdir()
        completed = [
            subprocess.CompletedProcess([], 0, stdout=str(self.source).encode(), stderr=b""),
            subprocess.CompletedProcess([], 0, stdout=b"a" * 40, stderr=b""),
            subprocess.CompletedProcess([], 0, stdout=b"", stderr=b""),
        ]
        with mock.patch("subprocess.run", side_effect=completed) as run:
            self.assertEqual(
                "a" * 40,
                git_repository_identity(
                    self.source, require_clean_index=True, require_toplevel=True
                ),
            )
        for call in run.call_args_list:
            command = call.args[0]
            self.assertIn("core.fsmonitor=false", command)
            self.assertEqual(1, command.count("core.fsmonitor=false"))

    def test_git_index_rejects_symlink_mode(self) -> None:
        repository = self.root / "git-source"
        repository.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
        oid = subprocess.run(
            ["git", "hash-object", "-w", "--stdin"], cwd=repository,
            input=b"target.txt", check=True, stdout=subprocess.PIPE,
        ).stdout.decode("ascii").strip()
        subprocess.run(
            ["git", "update-index", "--add", "--cacheinfo", "120000", oid, "linked"],
            cwd=repository, check=True,
        )
        with self.assertRaisesRegex(BundleError, "unsupported non-regular mode 120000"):
            _git_index(repository)

    def test_git_generation_is_pinned_to_clean_head_and_ignores_ambient_git_dir(self) -> None:
        subprocess.run(["git", "init", "-q"], cwd=self.source, check=True)
        subprocess.run(
            ["git", "config", "user.email", "fixture@example.com"],
            cwd=self.source, check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Fixture"], cwd=self.source, check=True
        )
        subprocess.run(["git", "add", "."], cwd=self.source, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "fixture"], cwd=self.source, check=True
        )
        other = self.root / "other"
        other.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=other, check=True)
        with mock.patch.dict(os.environ, {"GIT_DIR": str(other / ".git")}):
            lock = create_bundle_lock(
                self.source,
                name="fixture-template",
                version="1.0.0",
                source_mode="git-index",
            )
        expected_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.source, check=True,
            stdout=subprocess.PIPE,
        ).stdout.decode("ascii").strip()
        self.assertEqual(expected_commit, lock["bundle"]["source_git_commit"])
        (self.source / "managed.bin").write_bytes(b"staged change")
        subprocess.run(["git", "add", "managed.bin"], cwd=self.source, check=True)
        with self.assertRaisesRegex(BundleError, "index differs from HEAD"):
            create_bundle_lock(
                self.source,
                name="fixture-template",
                version="1.0.0",
                source_mode="git-index",
            )

    def test_kernel_git_identity_ignores_ambient_git_dir(self) -> None:
        subprocess.run(["git", "init", "-q"], cwd=self.source, check=True)
        subprocess.run(
            ["git", "config", "user.email", "fixture@example.com"],
            cwd=self.source, check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Fixture"], cwd=self.source, check=True
        )
        subprocess.run(["git", "add", "."], cwd=self.source, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "fixture"], cwd=self.source, check=True
        )
        expected = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.source, check=True,
            text=True, stdout=subprocess.PIPE,
        ).stdout.strip()
        other = self.root / "other-kernel"
        other.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=other, check=True)
        with mock.patch.dict(os.environ, {"GIT_DIR": str(other / ".git")}):
            self.assertEqual(expected, git_head(self.source))
            nested = self.source / "nested-workspace"
            nested.mkdir()
            self.assertEqual(expected, git_head(nested))
            with self.assertRaisesRegex(SnapshotError, "top-level"):
                git_repository_identity(
                    nested, require_clean_index=False, require_toplevel=True
                )

    def test_current_bundle_only_requires_compatible_component_manifest(self) -> None:
        older = copy.deepcopy(self.fixture.lock)
        compatibility = older["bundle"]["compatibility"]
        compatibility["runtime_descriptor_schema"] = 1
        compatibility["workspace_schema"] = 99
        compatibility["planner_protocol"] = 99
        older["bundle_sha256"] = hashlib.sha256(
            canonical_json(older["bundle"]).encode("utf-8")
        ).hexdigest()
        lock_path = self.root / "older-lock.json"
        lock_path.write_text(json.dumps(older) + "\n", encoding="utf-8")
        verified = verify_bundle(
            lock_path, self.source, expected_sha256=older["bundle_sha256"],
            source_mode="directory", role="current",
        )
        self.assertEqual("current", verified.role)
        with self.assertRaisesRegex(BundleError, "unsupported for candidate"):
            verify_bundle(
                lock_path, self.source, expected_sha256=older["bundle_sha256"],
                source_mode="directory", role="candidate",
            )

    def test_invalid_runtime_descriptor_cannot_be_locked(self) -> None:
        descriptor_path = self.source / "runtimes/codex.json"
        descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
        descriptor["surprise"] = True
        descriptor_path.write_text(json.dumps(descriptor) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(BundleError, "unknown surprise"):
            create_bundle_lock(
                self.source,
                name="fixture-template",
                version="1.0.0",
                source_mode="directory",
            )

    def test_link_like_root_is_rejected_but_ancestor_alias_is_allowed(self) -> None:
        alias = self.root / "alias"
        try:
            alias.symlink_to(self.root, target_is_directory=True)
        except OSError:
            self.skipTest("directory symlink creation unavailable")
        with self.assertRaisesRegex(BundleError, "source_root: must not be link-like"):
            create_bundle_lock(
                alias,
                name="fixture-template",
                version="1.0.0",
                source_mode="directory",
            )
        lock = create_bundle_lock(
            alias / "source",
            name="fixture-template",
            version="1.0.0",
            source_mode="directory",
        )
        self.assertEqual("fixture-template", lock["bundle"]["name"])


class StructuralPlannerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        current_root = self.root / "current-source"
        candidate_root = self.root / "candidate-source"
        current_root.mkdir()
        candidate_root.mkdir()
        self.current_fixture = BundleFixture(
            current_root, version="1.0.0", managed=b"binary\x00v1\n", addon=False
        )
        self.candidate_fixture = BundleFixture(
            candidate_root, version="2.0.0", managed=b"binary\x00v2\n", addon=True
        )
        self.current = self.current_fixture.verify()
        self.candidate = self.candidate_fixture.verify()
        self.target = self.root / "target"
        shutil.copytree(current_root, self.target)
        (self.target / "seed.txt").write_text("personal seed\n", encoding="utf-8")
        self.config = self.target / "contextos.workspace.json"
        self.config.write_text(
            json.dumps(workspace("fixture-template", "1.0.0"), indent=2) + "\n",
            encoding="utf-8",
        )

    def plan(self) -> dict:
        return create_structural_plan(
            target_root=self.target,
            workspace_config_path=self.config,
            expected_config_sha256=digest(self.config),
            candidate=self.candidate,
            desired_components=["addon"],
            current=self.current,
            current_components=["core"],
        )

    def snapshot(self) -> dict[str, bytes]:
        return {
            path.relative_to(self.target).as_posix(): path.read_bytes()
            for path in self.target.rglob("*") if path.is_file()
        }

    def test_plan_is_deterministic_read_only_and_policy_aware(self) -> None:
        before = self.snapshot()
        first = self.plan()
        second = self.plan()
        self.assertEqual(first, second)
        self.assertEqual(before, self.snapshot())
        actions = {item["path"]: item["action"] for item in first["actions"]}
        self.assertEqual("add", actions["addon.txt"])
        self.assertEqual("replace", actions["managed.bin"])
        self.assertEqual("preserve-seed", actions["seed.txt"])
        self.assertEqual(
            {"source": "fixture-template", "version": "2.0.0"},
            first["intended_workspace"]["template"],
        )
        self.assertIn("target", first["executable_modes_verified"])
        self.assertEqual(
            first["plan_digest"],
            hashlib.sha256(
                canonical_json({
                    key: value for key, value in first.items() if key != "plan_digest"
                }).encode("utf-8")
            ).hexdigest(),
        )

    def test_dirty_managed_target_fails_without_writes(self) -> None:
        (self.target / "managed.bin").write_bytes(b"locally changed\n")
        before = self.snapshot()
        with self.assertRaisesRegex(BundleError, "managed path is dirty"):
            self.plan()
        self.assertEqual(before, self.snapshot())

    def test_desired_components_cannot_omit_candidate_agent_requirements(self) -> None:
        with self.assertRaisesRegex(BundleError, "required by configured agents"):
            create_structural_plan(
                target_root=self.target,
                workspace_config_path=self.config,
                expected_config_sha256=digest(self.config),
                candidate=self.candidate,
                desired_components=["core"],
                current=self.current,
                current_components=["core"],
            )

    def test_current_components_must_claim_every_materialized_component(self) -> None:
        before = self.snapshot()
        with self.assertRaisesRegex(BundleError, "unclaimed materialized path"):
            create_structural_plan(
                target_root=self.target,
                workspace_config_path=self.config,
                expected_config_sha256=digest(self.config),
                candidate=self.candidate,
                desired_components=["addon"],
                current=self.current,
                current_components=[],
            )
        self.assertEqual(before, self.snapshot())

    def test_crlf_checkout_matches_lf_base_without_becoming_dirty(self) -> None:
        manifest = self.target / "components/manifest.json"
        lf_bytes = manifest.read_bytes().replace(b"\r\n", b"\n")
        manifest.write_bytes(lf_bytes.replace(b"\n", b"\r\n"))
        plan = self.plan()
        manifest_action = next(
            item for item in plan["actions"]
            if item["path"] == "components/manifest.json"
        )
        self.assertEqual("replace", manifest_action["action"])

    def test_full_template_materialization_is_independent_of_configured_agents(self) -> None:
        current_root = self.root / "full-current"
        current_root.mkdir()
        fixture = BundleFixture(
            current_root, version="1.0.0", managed=b"binary\x00v1\n",
            addon=True, runtime_addon=False,
        )
        current = fixture.verify(role="current")
        target = self.root / "full-target"
        shutil.copytree(current_root, target)
        config = target / "contextos.workspace.json"
        config.write_text(
            json.dumps(workspace("fixture-template", "1.0.0"), indent=2) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(BundleError, "unclaimed materialized path"):
            create_structural_plan(
                target_root=target, workspace_config_path=config,
                expected_config_sha256=digest(config), candidate=self.candidate,
                desired_components=["addon"], current=current,
                current_components=["core"],
            )
        plan = create_structural_plan(
            target_root=target, workspace_config_path=config,
            expected_config_sha256=digest(config), candidate=self.candidate,
            desired_components=["addon"], current=current,
            current_components=["addon"],
        )
        self.assertEqual(["core", "addon"], plan["current_components"])

    def test_new_seed_preserves_a_preexisting_user_file(self) -> None:
        current_root = self.root / "seedless-current"
        current_root.mkdir()
        fixture = BundleFixture(
            current_root, version="1.0.0", managed=b"binary\x00v1\n",
            addon=False, include_seed=False,
        )
        current = fixture.verify(role="current")
        target = self.root / "seedless-target"
        shutil.copytree(current_root, target)
        (target / "seed.txt").write_text("user file\n", encoding="utf-8")
        config = target / "contextos.workspace.json"
        config.write_text(
            json.dumps(workspace("fixture-template", "1.0.0"), indent=2) + "\n",
            encoding="utf-8",
        )
        plan = create_structural_plan(
            target_root=target, workspace_config_path=config,
            expected_config_sha256=digest(config), candidate=self.candidate,
            desired_components=["addon"], current=current,
            current_components=["core"],
        )
        seed_action = next(item for item in plan["actions"] if item["path"] == "seed.txt")
        self.assertEqual("preserve-seed", seed_action["action"])

    def test_uninstalled_component_seed_is_not_inferred_as_materialized(self) -> None:
        current_root = self.root / "seed-component-current"
        candidate_root = self.root / "seed-component-candidate"
        current_root.mkdir()
        candidate_root.mkdir()
        current_fixture = BundleFixture(
            current_root, version="1.0.0", managed=b"binary\x00v1\n",
            addon=True, runtime_addon=False, addon_policy="seed",
        )
        candidate_fixture = BundleFixture(
            candidate_root, version="2.0.0", managed=b"binary\x00v2\n",
            addon=True, runtime_addon=True, addon_policy="seed",
        )
        current = current_fixture.verify(role="current")
        candidate = candidate_fixture.verify()
        target = self.root / "seed-component-target"
        shutil.copytree(current_root, target)
        (target / "addon.txt").write_text("user-created\n", encoding="utf-8")
        config = target / "contextos.workspace.json"
        config.write_text(
            json.dumps(workspace("fixture-template", "1.0.0"), indent=2) + "\n",
            encoding="utf-8",
        )
        plan = create_structural_plan(
            target_root=target, workspace_config_path=config,
            expected_config_sha256=digest(config), candidate=candidate,
            desired_components=["addon"], current=current,
            current_components=["core"],
        )
        addon_action = next(item for item in plan["actions"] if item["path"] == "addon.txt")
        self.assertEqual("preserve-seed", addon_action["action"])

    def test_missing_safe_path_returns_requested_leaf_not_first_missing_ancestor(self) -> None:
        expected = self.target / "absent" / "nested" / "file.txt"
        actual = _safe_path(
            self.target, "absent/nested/file.txt", "target.fixture", missing_ok=True
        )
        self.assertEqual(expected, actual)

    def test_stale_config_hash_unavailable_component_and_unowned_collision_fail(self) -> None:
        with self.assertRaisesRegex(BundleError, "configuration is stale"):
            create_structural_plan(
                target_root=self.target,
                workspace_config_path=self.config,
                expected_config_sha256="0" * 64,
                candidate=self.candidate,
                desired_components=["addon"],
                current=self.current,
                current_components=["core"],
            )
        with self.assertRaisesRegex(BundleError, "unknown components"):
            create_structural_plan(
                target_root=self.target,
                workspace_config_path=self.config,
                expected_config_sha256=digest(self.config),
                candidate=self.candidate,
                desired_components=["unavailable"],
                current=self.current,
                current_components=["core"],
            )
        (self.target / "addon.txt").write_text("unowned\n", encoding="utf-8")
        with self.assertRaisesRegex(BundleError, "unowned target collides"):
            self.plan()

    @unittest.skipIf(os.name == "nt", "case-only siblings cannot coexist on Windows")
    def test_portable_destination_alias_fails(self) -> None:
        (self.target / "ADDON.TXT").write_text("alias\n", encoding="utf-8")
        with self.assertRaisesRegex(BundleError, "portable alias"):
            self.plan()

    def test_source_and_target_must_be_separate(self) -> None:
        with self.assertRaisesRegex(BundleError, "separate from the candidate"):
            create_structural_plan(
                target_root=self.candidate.root,
                workspace_config_path=self.candidate.root / "contextos.workspace.json",
                expected_config_sha256="0" * 64,
                candidate=self.candidate,
                desired_components=["core"],
            )
