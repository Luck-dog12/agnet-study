"""Progressive project Skill discovery and loading tests; no network calls."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app import skills as skills_module
from app.agent import CodingAgent
from app.config import Settings
from app.observability import Observability, _REGISTERED_TOOL_NAMES
from app.schemas import ToolMessage
from app.skills import (
    SkillConfigurationError,
    SkillRuntime,
    build_skill_catalog_message,
    discover_skills,
)
from app.workflow import SessionEventWorkflow, tool_retry_safety
from app.schemas import ToolCall
from tool.policy import load_tool_policy, path_is_allowed
from tool.registry import TOOLSCHEMAS, call_tool


def _chunk(content: str):
    delta = SimpleNamespace(
        role="assistant", content=content, tool_calls=[], reasoning_content=None
    )
    return [SimpleNamespace(choices=[SimpleNamespace(index=0, delta=delta)])]


class _FakeCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return self.responses.pop(0)


class _FakeClient:
    def __init__(self, responses):
        self.completions = _FakeCompletions(responses)
        self.chat = SimpleNamespace(completions=self.completions)


class SkillTests(unittest.TestCase):
    def setUp(self):
        tests_directory = Path(__file__).resolve().parent
        self.temporary = tempfile.TemporaryDirectory(
            prefix="skill-tests-", dir=tests_directory
        )
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / "skills"
        self.root.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def write_skill(
        self,
        name: str,
        *,
        description: str | None = None,
        body: str = "# Instructions\n\nFollow the verified steps.\n",
        resources: dict[str, bytes | str] | None = None,
        manifest_name: str = "SKILL.md",
    ) -> Path:
        directory = self.root / name
        directory.mkdir(parents=True, exist_ok=True)
        description = description or f"Guide {name} work; use when {name} tasks are requested."
        manifest = (
            f"---\nname: {name}\ndescription: {description}\n---\n\n{body}"
        )
        (directory / manifest_name).write_text(manifest, encoding="utf-8")
        for relative, content in (resources or {}).items():
            target = directory / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                target.write_bytes(content)
            else:
                target.write_text(content, encoding="utf-8")
        return directory

    def invoke(self, name: str, arguments: dict, **kwargs) -> dict:
        raw = call_tool(
            {"id": "skill-call", "name": name, "arguments": arguments},
            **kwargs,
        )
        return json.loads(ToolMessage.model_validate_json(raw).model_dump_json())["result"]

    def test_discovery_reads_metadata_only_and_catalog_is_stable(self):
        first = self.write_skill(
            "z-last",
            resources={"references/large.txt": "SECRET_BODY" * 100},
        )
        self.write_skill("a-first", description="Do A; use for A requests.")
        (self.root / "README.md").write_text("guide", encoding="utf-8")
        (self.root / "_template").mkdir()
        (self.root / "_template" / "SKILL.md").write_text("ignored", encoding="utf-8")
        manifest_size = (first / "SKILL.md").stat().st_size
        bytes_requested: list[int] = []
        real_read = os.read

        def tracking_read(descriptor, count):
            bytes_requested.append(count)
            return real_read(descriptor, count)

        with patch("app.skills.os.read", side_effect=tracking_read), patch(
            "app.skills._read_exact_file",
            side_effect=AssertionError("discovery must not read complete files"),
        ):
            metadata = discover_skills(self.root)

        self.assertEqual([item.name for item in metadata], ["a-first", "z-last"])
        self.assertTrue(bytes_requested)
        self.assertTrue(all(count == 1 for count in bytes_requested))
        self.assertLess(len(bytes_requested), manifest_size * 2)
        catalog = build_skill_catalog_message(reversed(metadata))
        self.assertLess(catalog.index("a-first"), catalog.index("z-last"))
        self.assertNotIn("SECRET_BODY", catalog)
        self.assertIsNone(build_skill_catalog_message(()))

    def test_load_then_later_paginated_resource_read_and_token_rotation(self):
        self.write_skill(
            "python-helper",
            resources={"references/guide.txt": "abcdef", "scripts/example.py": "print('text only')\n"},
        )
        runtime = SkillRuntime(session_id="session", root=self.root)
        loaded = self.invoke(
            "load_skill",
            {"skill_name": "python-helper"},
            skill_runtime=runtime,
            skill_step=0,
        )
        self.assertEqual(loaded["status"], "success")
        output = loaded["output"]
        self.assertIn("# Instructions", output["content"])
        self.assertEqual(
            output["resources"],
            ["references/guide.txt", "scripts/example.py"],
        )
        self.assertRegex(output["skill_token"], r"^[0-9a-f]{64}$")

        same_step = self.invoke(
            "read_skill_resource",
            {
                "skill_name": "python-helper",
                "skill_token": output["skill_token"],
                "resource_path": "references/guide.txt",
                "offset": 0,
                "max_bytes": 2,
            },
            skill_runtime=runtime,
            skill_step=0,
        )
        self.assertEqual(same_step["status"], "denied")
        first_page = self.invoke(
            "read_skill_resource",
            {
                "skill_name": "python-helper",
                "skill_token": output["skill_token"],
                "resource_path": "references/guide.txt",
                "offset": 0,
                "max_bytes": 2,
            },
            skill_runtime=runtime,
            skill_step=1,
        )
        self.assertEqual(first_page["status"], "success")
        self.assertEqual(first_page["output"]["content"], "ab")
        self.assertEqual(first_page["output"]["bytes_read"], 2)
        self.assertTrue(first_page["output"]["truncated"])

        reloaded = self.invoke(
            "load_skill",
            {"skill_name": "python-helper"},
            skill_runtime=runtime,
            skill_step=2,
        )
        self.assertEqual(reloaded["status"], "success")
        self.assertNotEqual(reloaded["output"]["skill_token"], output["skill_token"])
        stale = self.invoke(
            "read_skill_resource",
            {
                "skill_name": "python-helper",
                "skill_token": output["skill_token"],
                "resource_path": "references/guide.txt",
            },
            skill_runtime=runtime,
            skill_step=3,
        )
        self.assertEqual(stale["status"], "denied")

    def test_utf8_pagination_never_splits_a_multibyte_character(self):
        self.write_skill(
            "utf8-helper", resources={"references/guide.txt": "你a好"}
        )
        runtime = SkillRuntime(session_id="session", root=self.root)
        loaded = self.invoke(
            "load_skill", {"skill_name": "utf8-helper"},
            skill_runtime=runtime, skill_step=0,
        )["output"]
        common = {
            "skill_name": "utf8-helper",
            "skill_token": loaded["skill_token"],
            "resource_path": "references/guide.txt",
        }
        first = self.invoke(
            "read_skill_resource", {**common, "offset": 0, "max_bytes": 5},
            skill_runtime=runtime, skill_step=1,
        )
        self.assertEqual(first["status"], "success")
        self.assertEqual(first["output"]["content"], "你a")
        self.assertEqual(first["output"]["bytes_read"], 4)
        self.assertTrue(first["output"]["truncated"])
        second = self.invoke(
            "read_skill_resource", {**common, "offset": 4, "max_bytes": 5},
            skill_runtime=runtime, skill_step=2,
        )
        self.assertEqual(second["output"]["content"], "好")
        self.assertEqual(second["output"]["bytes_read"], 3)
        split_offset = self.invoke(
            "read_skill_resource", {**common, "offset": 1, "max_bytes": 5},
            skill_runtime=runtime, skill_step=2,
        )
        self.assertEqual(split_offset["status"], "denied")

    def test_token_is_rejected_across_skills_and_runtime_instances(self):
        self.write_skill("first-skill", resources={"reference.txt": "first"})
        self.write_skill("second-skill", resources={"reference.txt": "second"})
        first_runtime = SkillRuntime(session_id="session-a", root=self.root)
        second_runtime = SkillRuntime(session_id="session-b", root=self.root)
        token = self.invoke(
            "load_skill", {"skill_name": "first-skill"},
            skill_runtime=first_runtime, skill_step=0,
        )["output"]["skill_token"]
        arguments = {
            "skill_name": "first-skill",
            "skill_token": token,
            "resource_path": "reference.txt",
        }
        cross_runtime = self.invoke(
            "read_skill_resource", arguments,
            skill_runtime=second_runtime, skill_step=1,
        )
        self.assertEqual(cross_runtime["status"], "denied")
        cross_skill = self.invoke(
            "read_skill_resource", {**arguments, "skill_name": "second-skill"},
            skill_runtime=first_runtime, skill_step=1,
        )
        self.assertEqual(cross_skill["status"], "denied")

    def test_registry_denies_unbound_bad_token_path_utf8_and_changed_snapshot(self):
        directory = self.write_skill(
            "safe-skill", resources={"references/data.txt": "valid text"}
        )
        runtime = SkillRuntime(session_id="session", root=self.root)
        unbound = self.invoke("load_skill", {"skill_name": "safe-skill"})
        self.assertEqual(unbound["status"], "denied")
        with patch("app.skills.os.read", side_effect=OSError("raced filesystem")):
            interrupted = self.invoke(
                "load_skill", {"skill_name": "safe-skill"},
                skill_runtime=runtime, skill_step=0,
            )
        self.assertEqual(interrupted["status"], "denied")
        loaded = self.invoke(
            "load_skill", {"skill_name": "safe-skill"},
            skill_runtime=runtime, skill_step=0,
        )["output"]
        common = {
            "skill_name": "safe-skill",
            "resource_path": "references/data.txt",
        }
        for arguments in (
            {**common, "skill_token": "f" * 64},
            {**common, "skill_token": loaded["skill_token"], "resource_path": "../data.txt"},
            {**common, "skill_token": loaded["skill_token"], "offset": 999},
        ):
            with self.subTest(arguments=arguments):
                result = self.invoke(
                    "read_skill_resource", arguments,
                    skill_runtime=runtime, skill_step=1,
                )
                self.assertIn(result["status"], {"error", "denied"})

        resource = directory / "references" / "data.txt"
        resource.write_bytes(b"\xff\xfe")
        changed = self.invoke(
            "read_skill_resource",
            {**common, "skill_token": loaded["skill_token"]},
            skill_runtime=runtime,
            skill_step=1,
        )
        self.assertEqual(changed["status"], "denied")

        fresh = self.invoke(
            "load_skill", {"skill_name": "safe-skill"},
            skill_runtime=runtime, skill_step=2,
        )["output"]
        invalid_utf8 = self.invoke(
            "read_skill_resource",
            {**common, "skill_token": fresh["skill_token"]},
            skill_runtime=runtime,
            skill_step=3,
        )
        self.assertEqual(invalid_utf8["status"], "denied")

    def test_manifest_change_invalidates_resource_token(self):
        directory = self.write_skill(
            "changing-skill", resources={"references/data.txt": "one"}
        )
        runtime = SkillRuntime(session_id="session", root=self.root)
        loaded = self.invoke(
            "load_skill", {"skill_name": "changing-skill"},
            skill_runtime=runtime, skill_step=0,
        )["output"]
        manifest = directory / "SKILL.md"
        manifest.write_text(
            manifest.read_text(encoding="utf-8") + "\nChanged body.\n",
            encoding="utf-8",
        )
        result = self.invoke(
            "read_skill_resource",
            {
                "skill_name": "changing-skill",
                "skill_token": loaded["skill_token"],
                "resource_path": "references/data.txt",
            },
            skill_runtime=runtime, skill_step=1,
        )
        self.assertEqual(result["status"], "denied")

    def test_load_rejects_metadata_swap_between_validation_and_snapshot(self):
        directory = self.write_skill("racing-skill", description="Original metadata.")
        runtime = SkillRuntime(session_id="session", root=self.root)
        original_load_snapshot = skills_module._load_snapshot

        def replace_then_load(root, name):
            (directory / "SKILL.md").write_text(
                "---\nname: racing-skill\ndescription: Replaced metadata.\n---\n\n"
                "# Replaced instructions\n",
                encoding="utf-8",
            )
            return original_load_snapshot(root, name)

        with patch("app.skills._load_snapshot", side_effect=replace_then_load):
            result = self.invoke(
                "load_skill", {"skill_name": "racing-skill"},
                skill_runtime=runtime, skill_step=0,
            )
        self.assertEqual(result["status"], "denied")

    def test_invalid_tree_and_front_matter_fail_closed(self):
        invalid_manifests = (
            "name: bad\ndescription: no delimiters\n",
            "---\nname: bad\nname: bad\ndescription: duplicate\n---\n",
            "---\nname: bad\ndescription: x\nunknown: y\n---\n",
            "---\nname: other\ndescription: mismatch\n---\n",
            "---\nname: bad\ndescription: has\x00nul\n---\n",
        )
        for index, manifest in enumerate(invalid_manifests):
            directory = self.root / "bad"
            directory.mkdir()
            (directory / "SKILL.md").write_bytes(manifest.encode("utf-8"))
            with self.subTest(index=index), self.assertRaises(SkillConfigurationError):
                discover_skills(self.root)
            for child in directory.iterdir():
                child.unlink()
            directory.rmdir()

        directory = self.write_skill("duplicate")
        (directory / "skill.MD").write_text("duplicate", encoding="utf-8")
        with self.assertRaises(SkillConfigurationError):
            discover_skills(self.root)
        for child in directory.iterdir():
            child.unlink()
        directory.rmdir()

        # A body control character is intentionally deferred until full load.
        directory = self.root / "body-control"
        directory.mkdir()
        (directory / "SKILL.md").write_bytes(
            b"---\nname: body-control\ndescription: Validate bodies; use for body tests.\n---\nbody\x00"
        )
        self.assertEqual(discover_skills(self.root)[0].name, "body-control")
        runtime = SkillRuntime(session_id="session", root=self.root)
        denied = self.invoke(
            "load_skill", {"skill_name": "body-control"},
            skill_runtime=runtime, skill_step=0,
        )
        self.assertEqual(denied["status"], "denied")

    def test_size_limits_and_unexpected_root_objects_fail_closed(self):
        directory = self.write_skill("large-manifest")
        manifest = directory / "SKILL.md"
        manifest.write_text(
            "---\nname: large-manifest\ndescription: Check size; use for size tests.\n---\n"
            + ("x" * (64 * 1024)),
            encoding="utf-8",
        )
        with self.assertRaises(SkillConfigurationError):
            discover_skills(self.root)
        for child in directory.iterdir():
            child.unlink()
        directory.rmdir()

        directory = self.write_skill(
            "large-resource", resources={"references/huge.txt": b"x" * (1024 * 1024 + 1)}
        )
        with self.assertRaises(SkillConfigurationError):
            discover_skills(self.root)
        for child in sorted(directory.rglob("*"), reverse=True):
            child.rmdir() if child.is_dir() else child.unlink()
        directory.rmdir()

        (self.root / "unexpected.txt").write_text("unexpected", encoding="utf-8")
        with self.assertRaises(SkillConfigurationError):
            discover_skills(self.root)

        missing = self.base / "does-not-exist"
        self.assertEqual(discover_skills(missing), ())

    def test_root_entries_and_per_skill_directories_are_bounded(self):
        self.write_skill("bounded-skill")
        (self.root / "README.md").write_text("guide", encoding="utf-8")
        (self.root / "_template").mkdir()
        with patch("app.skills._MAX_ROOT_ENTRIES", 2):
            with self.assertRaises(SkillConfigurationError):
                discover_skills(self.root)

        (self.root / "README.md").unlink()
        (self.root / "_template").rmdir()
        skill_directory = self.root / "bounded-skill"
        (skill_directory / "one").mkdir()
        (skill_directory / "two").mkdir()
        with patch("app.skills._MAX_SKILL_DIRECTORIES", 2):
            with self.assertRaises(SkillConfigurationError):
                discover_skills(self.root)

        (skill_directory / "one").rmdir()
        (skill_directory / "two").rmdir()
        (self.root / "_draft").mkdir()
        with self.assertRaises(SkillConfigurationError):
            discover_skills(self.root)

    def test_symlink_and_hardlink_are_rejected_when_supported(self):
        directory = self.write_skill("linked", resources={"references/data.txt": "text"})
        original = directory / "references" / "data.txt"
        hardlink = directory / "references" / "hard.txt"
        try:
            os.link(original, hardlink)
        except OSError as exc:
            self.skipTest(f"hard links unavailable: {type(exc).__name__}")
        with self.assertRaises(SkillConfigurationError):
            discover_skills(self.root)
        hardlink.unlink()
        symlink = directory / "references" / "link.txt"
        try:
            symlink.symlink_to(original)
        except OSError:
            return
        with self.assertRaises(SkillConfigurationError):
            discover_skills(self.root)

    def test_agent_catalog_is_sdk_only_cwd_independent_and_visible_in_plan(self):
        self.write_skill("planning-helper", description="Plan safely; use for planning tasks.")
        original_cwd = Path.cwd()
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        try:
            os.chdir(elsewhere)
            with patch("app.skills.SKILLS_ROOT", self.root):
                client = _FakeClient([_chunk("done")])
                agent = CodingAgent(
                    client,
                    Settings(api_key="fake", model="fake"),
                    system_prompt="system",
                    observability=Mock(spec=Observability),
                    session_workflow=SessionEventWorkflow.in_memory(),
                )
                agent.run("request")
        finally:
            os.chdir(original_cwd)
        request_messages = client.completions.requests[0]["messages"]
        self.assertEqual(request_messages[1]["role"], "user")
        self.assertIn("planning-helper", request_messages[1]["content"])
        self.assertNotIn("# Instructions", request_messages[1]["content"])
        self.assertEqual([message.role for message in agent.messages], ["system", "user", "assistant"])
        self.assertNotIn("planning-helper", json.dumps([event.model_dump() for event in agent.session_events]))

        # Plan receives the same ephemeral catalog while tools remain disabled.
        with patch("app.skills.SKILLS_ROOT", self.root):
            plan_client = _FakeClient([_chunk(json.dumps({
                "kind": "final",
                "plan": {
                    "title": "Plan", "objective": "Deliver safely.",
                    "in_scope": ["Skill"], "out_of_scope": ["Other"],
                    "assumptions": ["Available"],
                    "steps": [{"number": 1, "title": "Load", "actions": ["Inspect"], "verification": "Pass"}],
                    "acceptance_criteria": ["Pass"],
                    "risks": [{"risk": "Drift", "mitigation": "Reload"}],
                    "open_questions": [],
                },
            }))])
            plan_agent = CodingAgent(
                plan_client,
                Settings(api_key="fake", model="fake"),
                system_prompt="system",
                observability=Mock(spec=Observability),
                session_workflow=SessionEventWorkflow.in_memory(),
            )
            plan_agent.plan("make a plan")
        plan_request = plan_client.completions.requests[0]
        self.assertIn("planning-helper", plan_request["messages"][1]["content"])
        self.assertNotIn("tools", plan_request)

    def test_schema_policy_observability_and_recovery_safety_are_synchronized(self):
        names = [item["function"]["name"] for item in TOOLSCHEMAS]
        self.assertIn("load_skill", names)
        self.assertIn("read_skill_resource", names)
        load_schema = next(
            item for item in TOOLSCHEMAS if item["function"]["name"] == "load_skill"
        )["function"]["parameters"]
        self.assertEqual(load_schema["required"], ["skill_name"])
        self.assertEqual(_REGISTERED_TOOL_NAMES, frozenset(names))
        self.assertFalse(path_is_allowed("skills/example/SKILL.md", load_tool_policy()))
        for name in ("load_skill", "read_skill_resource"):
            self.assertEqual(
                tool_retry_safety(ToolCall(id="x", name=name, arguments={})),
                "safe",
            )


if __name__ == "__main__":
    unittest.main()
