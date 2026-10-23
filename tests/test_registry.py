"""统一注册表和外置工具权限策略的独立测试。"""

from __future__ import annotations

from dataclasses import replace
import importlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from app.schemas import (
    BashShellArguments,
    BashShellOutput,
    CodeInterpreterArguments,
    ToolMessage,
)
from tool.policy import FileAccessPolicy, path_is_allowed
from tool.registry import TOOLSCHEMAS, call_tool


access = importlib.import_module("tool.file_access")
policy_module = importlib.import_module("tool.policy")
registry = importlib.import_module("tool.registry")


class RegistryAndPolicyTests(unittest.TestCase):
    def setUp(self):
        test_directory = Path(__file__).resolve().parent
        self.temporary = tempfile.TemporaryDirectory(
            prefix="registry-tests-", dir=test_directory
        )
        self.fixture = Path(self.temporary.name).resolve()
        self.root = self.fixture / "project"
        self.root.mkdir()
        self.root_patch = patch.object(access, "_PROJECT_ROOT", self.root)
        self.root_patch.start()

    def tearDown(self):
        self.root_patch.stop()
        self.temporary.cleanup()

    def write(self, path: str, content: str = "text") -> Path:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def invoke(self, name: str, arguments: dict, callback=None) -> dict:
        raw = call_tool(
            {"id": "registry_1", "name": name, "arguments": arguments},
            confirmation_callback=callback,
        )
        return json.loads(ToolMessage.model_validate_json(raw).model_dump_json())["result"]

    def test_registry_and_openai_schemas_have_one_exact_entry_per_tool(self):
        expected = [
            "read_file", "editor_file", "list_files", "grep", "write_file",
            "list_pending_changes", "apply_pending_change", "inspect_recovery",
            "resolve_recovery", "load_skill", "read_skill_resource",
            "bash_shell", "code_interpreter",
            "web_fetch", "web_search", "get_async_task", "cancel_async_task",
        ]
        self.assertEqual(list(registry._REGISTRY), expected)
        self.assertEqual([item["function"]["name"] for item in TOOLSCHEMAS], expected)
        self.assertEqual(len({item["function"]["name"] for item in TOOLSCHEMAS}), len(expected))
        self.assertEqual(
            next(item for item in TOOLSCHEMAS if item["function"]["name"] == "bash_shell")
            ["function"]["parameters"],
            BashShellArguments.model_json_schema(),
        )

    def test_missing_or_malformed_policy_fails_closed_for_file_scan_and_shell(self):
        self.write("data.txt", "needle")
        self.write("tests/test_sample.py", "import unittest\n")
        missing = self.fixture / "missing-policy.json"
        malformed = self.fixture / "bad-policy.json"
        malformed.write_text("{}", encoding="utf-8")
        calls = (
            ("read_file", {"path": "data.txt"}),
            ("editor_file", {"path": "data.txt", "old_text": "needle", "new_text": "x"}),
            ("list_files", {"pattern": "**/*"}),
            ("grep", {"pattern": "needle"}),
            ("write_file", {"path": "new.txt", "content": "x", "mode": "create"}),
            ("bash_shell", {"command": "compileall", "paths": ["tests"]}),
        )
        for policy_file in (missing, malformed):
            with patch.object(policy_module, "_POLICY_FILE", policy_file):
                for name, arguments in calls:
                    with self.subTest(policy=policy_file.name, name=name):
                        result = self.invoke(name, arguments)
                        self.assertEqual(result["status"], "denied")
        self.assertEqual((self.root / "data.txt").read_text(encoding="utf-8"), "needle")
        self.assertFalse((self.root / "new.txt").exists())

    def test_old_or_network_omitting_policy_is_rejected_instead_of_enabling_network(self):
        valid = policy_module.load_tool_policy().model_dump(mode="json")
        variants = []
        old = dict(valid)
        old["version"] = 3
        variants.append(old)
        missing_network = dict(valid)
        missing_network.pop("network")
        variants.append(missing_network)
        missing_search_gate = json.loads(json.dumps(valid))
        missing_search_gate["network"].pop("search_enabled")
        variants.append(missing_search_gate)
        for index, payload in enumerate(variants):
            path = self.fixture / f"invalid-network-{index}.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.subTest(index=index), patch.object(policy_module, "_POLICY_FILE", path):
                with self.assertRaises(policy_module.PolicyLoadError):
                    policy_module.load_tool_policy()

    def test_external_allowlist_can_restrict_paths_but_not_unprotect_policy(self):
        self.write("src/allowed.txt", "allowed")
        self.write("data/blocked.txt", "blocked")
        self.write(".agent/tool_policy.json", "host copy")
        restrictive = self.fixture / "restrictive.json"
        restrictive.write_text(json.dumps({
            "version": 5,
            "file_access": {
                "allowed_path_patterns": ["src/**/*"],
                "protected_path_segments": [],
                "protected_file_names": [],
                "protected_file_prefixes": [],
            },
            "shell_commands": {
                "compileall": {
                    "enabled": True, "requires_confirmation": False, "max_paths": 1
                }
            },
            "code_interpreter": {
                "enabled": True,
                "max_ast_nodes": 512,
                "max_timeout_seconds": 10,
                "max_output_characters": 16384,
                "max_memory_mb": 256,
                "max_processes": 2,
            },
            "network": {
                "fetch_enabled": False,
                "search_enabled": False,
                "allow_http": False,
                "max_response_bytes": 1024,
                "max_timeout_seconds": 1,
                "max_redirects": 0,
                "search_max_response_bytes": 1024,
                "search_max_timeout_seconds": 1,
            },
        }), encoding="utf-8")
        with patch.object(policy_module, "_POLICY_FILE", restrictive):
            self.assertEqual(
                self.invoke("read_file", {"path": "src/allowed.txt"})["status"],
                "success",
            )
            self.assertEqual(
                self.invoke("read_file", {"path": "data/blocked.txt"})["status"],
                "denied",
            )
            self.assertEqual(
                self.invoke("read_file", {"path": ".agent/tool_policy.json"})["status"],
                "denied",
            )

    def test_internal_state_and_system_prompt_are_protected(self):
        policy = policy_module.load_tool_policy()
        self.assertFalse(path_is_allowed("data/pending/candidate.bin", policy))
        self.assertFalse(path_is_allowed("DATA/PENDING", policy))
        self.assertFalse(path_is_allowed("data/sessions/current/0000000000000001.json", policy))
        self.assertFalse(path_is_allowed("DATA/SESSIONS", policy))
        self.assertFalse(path_is_allowed("code.md", policy))
        self.assertFalse(path_is_allowed("CODE.MD", policy))
        self.assertFalse(path_is_allowed("skills/example/SKILL.md", policy))
        self.assertFalse(path_is_allowed("SKILLS/EXAMPLE/skill.md", policy))
        self.assertTrue(path_is_allowed("other/pending/file.txt", policy))
        self.assertTrue(path_is_allowed("data/pending-notes.txt", policy))

        self.write("data/pending/candidate.bin", "SECRET_CANDIDATE")
        self.write("data/sessions/current/0000000000000001.json", "SECRET_SESSION")
        self.assertEqual(
            self.invoke("read_file", {"path": "data/pending/candidate.bin"})["status"],
            "denied",
        )
        self.assertEqual(
            self.invoke(
                "read_file", {"path": "data/sessions/current/0000000000000001.json"}
            )["status"],
            "denied",
        )
        self.assertEqual(
            self.invoke("write_file", {
                "path": "data/pending/new.bin", "content": "bad", "mode": "create"
            })["status"],
            "denied",
        )
        prompt = self.write("code.md", "PERSISTENT_SYSTEM_PROMPT")
        for name, arguments in (
            ("read_file", {"path": "code.md"}),
            ("editor_file", {"path": "code.md", "old_text": "PERSISTENT", "new_text": "bad"}),
            ("write_file", {"path": "code.md", "content": "bad", "mode": "overwrite"}),
            ("bash_shell", {"command": "clear_file", "paths": ["code.md"]}),
        ):
            with self.subTest(name=name):
                self.assertEqual(self.invoke(name, arguments)["status"], "denied")
        listed = self.invoke("list_files", {"pattern": "**/*"})
        searched = self.invoke("grep", {"pattern": "PERSISTENT_SYSTEM_PROMPT"})
        self.assertEqual(listed["status"], "success")
        self.assertNotIn("code.md", [entry["path"] for entry in listed["output"]["entries"]])
        self.assertEqual(searched["status"], "success")
        self.assertEqual(searched["output"]["matches"], [])
        self.assertEqual(prompt.read_text(encoding="utf-8"), "PERSISTENT_SYSTEM_PROMPT")

        skill = self.write("skills/example/SKILL.md", "PROTECTED_SKILL_INSTRUCTIONS")
        for name, arguments in (
            ("read_file", {"path": "skills/example/SKILL.md"}),
            ("editor_file", {
                "path": "skills/example/SKILL.md",
                "old_text": "PROTECTED",
                "new_text": "bad",
            }),
            ("write_file", {
                "path": "skills/example/SKILL.md",
                "content": "bad",
                "mode": "overwrite",
            }),
            ("bash_shell", {
                "command": "clear_file",
                "paths": ["skills/example/SKILL.md"],
            }),
        ):
            with self.subTest(protected="skills", name=name):
                self.assertEqual(self.invoke(name, arguments)["status"], "denied")
        listed = self.invoke("list_files", {"pattern": "**/*"})
        searched = self.invoke("grep", {"pattern": "PROTECTED_SKILL_INSTRUCTIONS"})
        self.assertNotIn(
            "skills/example/SKILL.md",
            [entry["path"] for entry in listed["output"]["entries"]],
        )
        self.assertEqual(searched["output"]["matches"], [])
        self.assertEqual(skill.read_text(encoding="utf-8"), "PROTECTED_SKILL_INSTRUCTIONS")

        base = {
            "allowed_path_patterns": ["**/*"],
            "protected_path_segments": [],
            "protected_file_names": [],
            "protected_file_prefixes": [],
        }
        for invalid in (
            "/data/pending", "../pending", "data//pending", "data\\pending",
            "data/./pending", "C:/pending", "data/pending/",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(Exception):
                FileAccessPolicy.model_validate(
                    base | {"protected_relative_paths": [invalid]}
                )

    def test_bash_output_is_revalidated_and_correlated(self):
        arguments = {"command": "compileall", "paths": ["app"]}
        valid = {
            "command": "compileall", "paths": ["app"], "exit_code": 0,
            "stdout": "", "stderr": "", "truncated": False,
        }
        invalid = (
            valid | {"command": "validate_json"},
            valid | {"paths": ["tool"]},
            valid | {"stdout": "x" * 32769},
            valid | {"unknown": True},
            BashShellOutput.model_construct(**(valid | {"paths": ["../app"]})),
        )
        for value in invalid:
            with self.subTest(value=type(value).__name__):
                entry = replace(
                    registry._REGISTRY["bash_shell"], handler=Mock(return_value=value)
                )
                with patch.dict(registry._REGISTRY, {"bash_shell": entry}):
                    self.assertEqual(
                        self.invoke("bash_shell", arguments)["status"], "error"
                    )

    def test_registry_only_forwards_confirmation_to_opted_in_handler(self):
        callback = Mock(return_value=True)
        shell_handler = Mock(return_value={
            "command": "compileall", "paths": ["app"], "exit_code": 0,
            "stdout": "", "stderr": "", "truncated": False,
        })
        shell_entry = replace(registry._REGISTRY["bash_shell"], handler=shell_handler)
        read_handler = Mock(return_value={
            "path": "a.txt", "encoding": "utf-8", "content": "a", "offset": 0,
            "bytes_read": 1, "total_bytes": 1, "truncated": False,
        })
        read_entry = replace(registry._REGISTRY["read_file"], handler=read_handler)
        with patch.dict(registry._REGISTRY, {
            "bash_shell": shell_entry, "read_file": read_entry,
        }):
            self.assertEqual(
                self.invoke("bash_shell", {"command": "compileall", "paths": ["app"]}, callback)["status"],
                "success",
            )
            self.assertEqual(
                self.invoke("read_file", {"path": "a.txt"}, callback)["status"],
                "success",
            )
        shell_handler.assert_called_once()
        self.assertIs(shell_handler.call_args.args[1], callback)
        read_handler.assert_called_once()
        self.assertEqual(len(read_handler.call_args.args), 1)


if __name__ == "__main__":
    unittest.main()
