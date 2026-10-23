"""数据约定的离线测试；不读取密钥，也不发起网络请求。"""

import json
import unittest

from pydantic import TypeAdapter, ValidationError

from app.schemas import (
    AssistantMessage,
    BashShellArguments,
    BashShellOutput,
    CodeInterpreterArguments,
    CodeInterpreterOutput,
    FileApprovalState,
    FileApprovalStatus,
    FinalOutput,
    Message,
    SystemMessage,
    ToolCall,
    ToolMessage,
    ToolResult,
    UserMessage,
)


class SchemaTests(unittest.TestCase):
    def final_data(self) -> dict:
        return {
            "summary": "已说明项目结构。",
            "changed_files": [],
            "verification": {"status": "not_run", "details": "此次没有运行测试。"},
        }

    def tool_call(self, call_id: str = "call_1") -> ToolCall:
        return ToolCall(id=call_id, name="read_file", arguments={"path": "app/client.py"})

    def test_final_json_round_trip_and_required_fields(self):
        data = self.final_data()
        result = FinalOutput.model_validate_json(json.dumps(data))
        self.assertEqual(result.model_dump(), data)
        self.assertEqual(FinalOutput.model_validate_json(result.model_dump_json()), result)
        self.assertEqual(set(FinalOutput.model_json_schema()["required"]), set(data))
        for missing in data:
            with self.subTest(missing=missing), self.assertRaises(ValidationError):
                FinalOutput.model_validate({key: value for key, value in data.items() if key != missing})

    def test_final_rejects_invalid_json_types_and_fields(self):
        for text in ("not JSON", '{"summary":', "[]"):
            with self.subTest(text=text), self.assertRaises(ValidationError):
                FinalOutput.model_validate_json(text)
        for field, value in (
            ("summary", 123),
            ("summary", " \n"),
            ("changed_files", "app/client.py"),
            ("changed_files", [123]),
            ("changed_files", [" \n"]),
            ("unknown", True),
            ("verification", {"status": "passed"}),
            ("verification", {"status": "unknown", "details": "说明"}),
            ("verification", {"status": "not_run", "details": "\t"}),
            ("verification", {"status": "not_run", "details": "说明", "extra": 1}),
        ):
            with self.subTest(field=field, value=value), self.assertRaises(ValidationError):
                FinalOutput.model_validate_json(json.dumps(self.final_data() | {field: value}))

    def test_tool_arguments_accept_only_json_values(self):
        arguments = {"nested": [None, True, 2, 1.5, "text", {"items": []}]}
        call = ToolCall(id="call_1", name="read_file", arguments=arguments)
        self.assertEqual(call.arguments, arguments)
        for invalid in (object(), b"bytes", (1, 2), {1: "number key"}, float("nan"), float("inf")):
            with self.subTest(value=repr(invalid)), self.assertRaises(ValidationError):
                ToolCall(id="call_1", name="read_file", arguments={"nested": [invalid]})
        for number in ("NaN", "Infinity", "-Infinity", "1e999"):
            with self.subTest(number=number), self.assertRaises(ValidationError):
                ToolCall.model_validate_json(
                    '{"id":"call_1","name":"read_file","arguments":{"x":[{"y":' + number + "}]}}"
                )

    def test_tool_call_rejects_invalid_identity_types_and_fields(self):
        data = self.tool_call().model_dump()
        for field, value in (
            ("id", " \n"),
            ("id", 1),
            ("name", "read file"),
            ("name", "read/file"),
            ("name", "x" * 65),
            ("arguments", "{}"),
            ("unknown", True),
        ):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                ToolCall.model_validate(data | {field: value})

    def test_tool_result_status_and_error_must_agree(self):
        self.assertEqual(ToolResult(status="success").output, "")
        for status in ("error", "denied"):
            self.assertEqual(ToolResult(status=status, error="具体原因").error, "具体原因")
        for data in (
            {"status": "success", "error": "失败原因"},
            {"status": "success", "error": ""},
            {"status": "error"},
            {"status": "denied", "error": " \n"},
            {"status": "error", "error": 123},
            {"status": "success", "output": 123},
            {"status": "error", "error": "失败原因", "output": "残留结果"},
        ):
            with self.subTest(data=data), self.assertRaises(ValidationError):
                ToolResult.model_validate(data)

    def test_bash_shell_models_are_strict_and_bounded(self):
        arguments = BashShellArguments(command="compileall", paths=["app", "tool"])
        self.assertEqual(arguments.timeout_seconds, 5)
        output = BashShellOutput(
            command="compileall",
            paths=["app", "tool"],
            exit_code=0,
            stdout="",
            stderr="",
            truncated=False,
        )
        self.assertEqual(ToolResult(status="success", output=output).output, output)
        format_arguments = BashShellArguments(
            command="format_json", paths=["b.json", "a.json"]
        )
        format_output = BashShellOutput(
            command="format_json",
            paths=["b.json", "a.json"],
            exit_code=0,
            stdout="已规范格式化 2 个 JSON 文件。",
            stderr="",
            truncated=False,
        )
        self.assertEqual(format_arguments.paths, format_output.paths)
        for data in (
            {"command": "python", "paths": ["app"]},
            {"command": "compileall", "paths": []},
            {"command": "compileall", "paths": ["app"], "timeout_seconds": True},
            {"command": "compileall", "paths": ["app"], "extra": True},
        ):
            with self.subTest(data=data), self.assertRaises(ValidationError):
                BashShellArguments.model_validate(data)
        with self.assertRaises(ValidationError):
            BashShellOutput(
                command="compileall", paths=["../app"], exit_code=0,
                stdout="", stderr="", truncated=False,
            )

    def test_code_interpreter_and_file_approval_models_are_strict(self):
        arguments = CodeInterpreterArguments(code="sum(range(10))")
        output = CodeInterpreterOutput(
            exit_code=0, stdout="", result="45", stderr="", truncated=False
        )
        self.assertEqual(ToolResult(status="success", output=output).output, output)
        for data in (
            {"code": ""},
            {"code": "1", "timeout_seconds": 0},
            {"code": "1", "timeout_seconds": True},
            {"code": "1", "argv": []},
        ):
            with self.subTest(data=data), self.assertRaises(ValidationError):
                CodeInterpreterArguments.model_validate(data)

        state = FileApprovalState(path="app/new.py")
        self.assertEqual(state.status, FileApprovalStatus.UNAPPROVED)
        with self.assertRaises(ValidationError):
            FileApprovalState(
                path="app/new.py",
                status=FileApprovalStatus.APPROVED,
            )
        with self.assertRaises(ValidationError):
            FileApprovalState(
                path="app/new.py",
                request_id="stale",
            )
        diff_state = FileApprovalState(
            path="app/new.py",
            status=FileApprovalStatus.DIFF_PENDING,
            request_id="request",
            tool_name="write_file",
            operation="write_create",
            pending_file_id="a" * 32,
            diff_sha256="b" * 64,
        )
        self.assertEqual(diff_state.status, FileApprovalStatus.DIFF_PENDING)
        with self.assertRaises(ValidationError):
            FileApprovalState(
                path="app/new.py",
                status=FileApprovalStatus.DIFF_APPROVED,
                request_id="request",
                tool_name="write_file",
                operation="write_create",
            )

    def test_assistant_allows_text_and_multiple_calls(self):
        calls = [self.tool_call("call_1"), self.tool_call("call_2")]
        reply = AssistantMessage(content="开始检查。", tool_calls=calls, reasoning_content=" 原样回传\n")
        self.assertEqual(len(reply.tool_calls), 2)
        self.assertEqual(reply.reasoning_content, " 原样回传\n")
        self.assertIsNone(AssistantMessage(tool_calls=calls).content)
        self.assertEqual(AssistantMessage(content="", tool_calls=calls).content, "")
        self.assertIsNot(AssistantMessage(content="甲").tool_calls, AssistantMessage(content="乙").tool_calls)
        for data in (
            {},
            {"content": " \n"},
            {"reasoning_content": "只有兼容字段"},
            {"content": 1},
            {"tool_calls": [self.tool_call(), self.tool_call()]},
        ):
            with self.subTest(data=data), self.assertRaises(ValidationError):
                AssistantMessage.model_validate(data)

    def test_roles_are_discriminated_and_body_whitespace_is_preserved(self):
        body = "\n    print('保留缩进')\n"
        messages = [
            SystemMessage(content=body),
            UserMessage(content=body),
            AssistantMessage(content=body),
            ToolMessage(tool_call_id="call_1", result=ToolResult(status="success", output=body)),
        ]
        adapter = TypeAdapter(list[Message])
        restored = adapter.validate_json(adapter.dump_json(messages))
        self.assertEqual(restored, messages)
        self.assertEqual([type(message) for message in restored], [type(message) for message in messages])
        for message in restored[:3]:
            self.assertEqual(message.content, body)
        self.assertEqual(restored[3].result.output, body)
        for role in ("unknown", "developer"):
            with self.subTest(role=role), self.assertRaises(ValidationError):
                adapter.validate_python([{"role": role, "content": "正文"}])

    def test_messages_reject_invalid_required_content_and_roles(self):
        for model in (SystemMessage, UserMessage):
            for content in ("", " \n", 1):
                with self.subTest(model=model, content=content), self.assertRaises(ValidationError):
                    model(content=content)
        with self.assertRaises(ValidationError):
            UserMessage(role="assistant", content="正文")
        with self.assertRaises(ValidationError):
            ToolMessage(tool_call_id=" ", result=ToolResult(status="success"))
        with self.assertRaises(ValidationError):
            UserMessage(content="正文", unexpected=True)


if __name__ == "__main__":
    unittest.main()
