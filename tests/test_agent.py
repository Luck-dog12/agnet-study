"""Agent 协调层的离线测试；Fake client 不发送真实网络请求。"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
from queue import Queue
import stat
import tempfile
import time
from types import SimpleNamespace
from threading import Event, Lock, Thread
import unittest
from unittest.mock import Mock, call, patch

from app.agent import (
    AgentLoopLimitError,
    AgentPlanCancelledError,
    AgentProtocolError,
    AgentRequestError,
    CodingAgent,
    SystemPromptLoadError,
    load_system_prompt,
    message_to_sdk,
)
from app.config import Settings
from app.observability import Observability
from app.schemas import (
    AssistantMessage, AssistantMessageEventData, SessionUserMessageSnapshot,
    StepStartEventData, ToolCall, ToolCallEventData, ToolMessage, ToolResult,
    TurnStartEventData, UserMessageEventData,
)
from app.workflow import (
    SessionEventError, SessionEventWorkflow, assistant_message_snapshot,
    tool_call_snapshot, tool_retry_safety,
)


def chunk(*, content=None, calls=None, reasoning=None, choices=True):
    tool_calls = []
    for index, call_id, name, arguments in calls or []:
        tool_calls.append(SimpleNamespace(
            index=index,
            id=call_id,
            type="function",
            function=SimpleNamespace(name=name, arguments=arguments),
        ))
    if not choices:
        return SimpleNamespace(choices=[])
    delta = SimpleNamespace(
        role="assistant", content=content, tool_calls=tool_calls,
        reasoning_content=reasoning,
    )
    return SimpleNamespace(choices=[SimpleNamespace(index=0, delta=delta)])


def reply(*, content=None, calls=None, reasoning=None):
    streamed_calls = [
        (index, call_id, name, arguments)
        for index, (call_id, name, arguments) in enumerate(calls or [])
    ]
    return [chunk(content=content, calls=streamed_calls, reasoning=reasoning)]


def plan_question_json(question_id: str, question: str) -> str:
    return json.dumps({
        "kind": "clarify",
        "clarification": {
            "id": question_id,
            "question": question,
            "options": [
                {"label": "快速", "description": "缩小范围并尽快交付", "recommended": True},
                {"label": "平衡", "description": "兼顾范围与交付速度", "recommended": False},
                {"label": "完整", "description": "扩大覆盖但增加成本", "recommended": False},
            ],
        },
    }, ensure_ascii=False)


def final_plan_json(
    title: str = "实施计划", *, open_questions: list[str] | None = None
) -> str:
    return json.dumps({
        "kind": "final",
        "plan": {
            "title": title,
            "objective": "交付一个可验证的实现。",
            "in_scope": ["核心功能"],
            "out_of_scope": ["无关重构"],
            "assumptions": ["现有测试可运行"],
            "steps": [
                {"number": 1, "title": "实现", "actions": ["修改代码"], "verification": "测试通过"}
            ],
            "acceptance_criteria": ["功能符合需求"],
            "risks": [{"risk": "兼容性", "mitigation": "运行回归测试"}],
            "open_questions": [] if open_questions is None else open_questions,
        },
    }, ensure_ascii=False)


class FakeCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeClient:
    def __init__(self, responses):
        self.completions = FakeCompletions(responses)
        self.chat = SimpleNamespace(completions=self.completions)
        self.closed = False

    def close(self):
        self.closed = True


class AgentTests(unittest.TestCase):
    def setUp(self):
        from tests.model_service_helpers import ready_model_service_patch

        self.enterContext(ready_model_service_patch())

    def agent(self, responses, **kwargs):
        client = FakeClient(responses)
        settings = Settings(api_key="fake-key", model="fake-model")
        kwargs.setdefault("observability", Mock(spec=Observability))
        kwargs.setdefault("session_workflow", SessionEventWorkflow.in_memory())
        # These tests assert the conversation protocol, independently of local
        # user-installed Skills. Skill catalog injection has its own suite.
        with patch("app.agent.build_skill_catalog_message", return_value=None):
            agent = CodingAgent(client, settings, **kwargs)
        return agent, client

    def test_direct_final_text_is_returned_and_history_is_kept(self):
        agent, client = self.agent([reply(content="已完成项目检查。", reasoning="保留")])
        result = agent.run("检查项目")
        self.assertEqual(result, "已完成项目检查。")
        self.assertEqual([message.role for message in agent.messages], ["system", "user", "assistant"])
        self.assertEqual(agent.messages[-1].reasoning_content, "保留")
        request = client.completions.requests[0]
        self.assertEqual(request["model"], "fake-model")
        self.assertEqual(request["tool_choice"], "auto")
        self.assertIs(request["stream"], True)
        self.assertEqual(request["messages"][1], {"role": "user", "content": "检查项目"})
        self.assertTrue(request["tools"])
        self.assertEqual(
            [event.type for event in agent.session_events],
            [
                "turn/start",
                "user/message",
                "step/start",
                "assistant/message",
                "step/end",
                "turn/end",
            ],
        )
        stream = agent.session_events[3].data.stream
        self.assertEqual(len(stream), 1)
        self.assertIn('"content":"已完成项目检查。"', stream[0].chunk_json)

    def test_plan_model_drives_multiple_clarifications_then_commits_only_final_batch(self):
        answers = iter([1, 0])
        seen_questions = []

        def clarify(question):
            seen_questions.append(question)
            return next(answers)

        agent, client = self.agent([
            reply(content=final_plan_json(
                "尚未完成的计划", open_questions=["需要先确认交付范围"]
            )),
            reply(content=plan_question_json("scope", "采用哪种范围？")),
            reply(content=plan_question_json("quality", "采用哪种质量目标？")),
            reply(content=final_plan_json("模型驱动计划")),
        ], plan_clarification_callback=clarify)
        deltas: list[str] = []
        result = agent.plan("规划新功能", on_text_delta=deltas.append)

        self.assertIn("# 模型驱动计划", result)
        self.assertGreater(len(deltas), 1)
        self.assertEqual("".join(deltas), result)
        self.assertTrue(deltas[0].startswith("# 模型驱动计划"))
        self.assertEqual([item.id for item in seen_questions], ["scope", "quality"])
        self.assertEqual(len(client.completions.requests), 4)
        for request in client.completions.requests:
            self.assertNotIn("tools", request)
            self.assertNotIn("tool_choice", request)
            self.assertEqual(request["response_format"], {"type": "json_object"})
        correction_messages = client.completions.requests[1]["messages"]
        self.assertIn("open_questions 必须为 []", correction_messages[-1]["content"])
        self.assertEqual(correction_messages[-2]["role"], "assistant")
        answer_messages = client.completions.requests[2]["messages"]
        answer_payload = json.loads(answer_messages[-1]["content"])
        self.assertEqual(answer_payload["type"], "plan_clarification_answer")
        self.assertEqual(answer_payload["selected_option"]["index"], 2)
        self.assertEqual(
            [event.type for event in agent.session_events],
            ["turn/start", "user/message", "step/start", "assistant/message", "step/end", "turn/end"],
        )
        self.assertIn("平衡", agent.messages[1].content)
        self.assertEqual(agent.messages[-1].content, result)
        # 事件保留供应商的原始 JSON 流；严格重放该 JSON 必须生成
        # 与持久化 assistant 正文完全相同的可读计划。
        from app.plan_mode import parse_plan_model_response, render_plan_document

        stream = agent.session_events[3].data.stream
        raw_response = json.loads(stream[0].chunk_json)["choices"][0]["delta"]["content"]
        parsed = parse_plan_model_response(raw_response)
        self.assertIsNotNone(parsed.plan)
        self.assertEqual(render_plan_document(parsed.plan), result)

    def test_plan_cancel_and_protocol_error_leave_no_partial_session_events(self):
        cancel_agent, _ = self.agent(
            [reply(content=plan_question_json("scope", "采用哪种范围？"))],
            plan_clarification_callback=lambda _question: None,
        )
        with self.assertRaises(AgentPlanCancelledError):
            cancel_agent.plan("规划")
        self.assertEqual(cancel_agent.session_events, ())
        self.assertEqual([item.role for item in cancel_agent.messages], ["system"])

        invalid = reply(content='{"kind":"final","plan":null}')
        invalid_agent, invalid_client = self.agent([invalid, invalid, invalid])
        with self.assertRaises(AgentProtocolError):
            invalid_agent.plan("规划")
        self.assertEqual(invalid_agent.session_events, ())
        self.assertEqual(len(invalid_client.completions.requests), 3)

    def test_plan_clarification_limit_and_repeated_question_are_bounded_without_persistence(self):
        limited, _ = self.agent([
            reply(content=plan_question_json("one", "第一个问题？")),
            reply(content=plan_question_json("two", "第二个问题？")),
        ], plan_clarification_callback=lambda _question: 0, max_plan_clarifications=1)
        with self.assertRaises(AgentLoopLimitError):
            limited.plan("规划")
        self.assertEqual(limited.session_events, ())

        repeated, _ = self.agent([
            reply(content=plan_question_json("same", "重复问题？")),
            reply(content=plan_question_json("same", "重复问题？")),
        ], plan_clarification_callback=lambda _question: 0)
        with self.assertRaises(AgentProtocolError):
            repeated.plan("规划")
        self.assertEqual(repeated.session_events, ())

    def test_default_system_prompt_is_loaded_from_fixed_utf8_file_per_instance(self):
        with tempfile.TemporaryDirectory(prefix="system-prompt-tests-") as directory:
            prompt_file = Path(directory) / "code.md"
            prompt_file.write_bytes("第一种角色\n".encode("utf-8"))
            with patch("app.agent._SYSTEM_PROMPT_FILE", prompt_file):
                first, _ = self.agent([])
                prompt_file.write_bytes("第二种性格\n".encode("utf-8"))
                second, _ = self.agent([])
                previous = Path.cwd()
                try:
                    os.chdir(directory)
                    self.assertEqual(load_system_prompt(), "第二种性格\n")
                finally:
                    os.chdir(previous)
            self.assertEqual(first.messages[0].content, "第一种角色\n")
            self.assertEqual(second.messages[0].content, "第二种性格\n")

    def test_explicit_system_prompt_override_does_not_read_file(self):
        missing = Path("definitely-missing-code.md")
        with patch("app.agent._SYSTEM_PROMPT_FILE", missing):
            agent, _ = self.agent([], system_prompt="嵌入方覆盖角色")
        self.assertEqual(agent.messages[0].content, "嵌入方覆盖角色")

    def test_system_prompt_rejects_invalid_content_size_and_hard_links(self):
        with tempfile.TemporaryDirectory(prefix="system-prompt-tests-") as directory:
            root = Path(directory)
            prompt_file = root / "code.md"
            with patch("app.agent._SYSTEM_PROMPT_FILE", root / "missing.md"):
                with self.assertRaises(SystemPromptLoadError):
                    load_system_prompt()
            cases = (b"", b" \r\n\t", b"bad\xff", b"bad\x00prompt")
            for raw in cases:
                with self.subTest(raw=raw):
                    prompt_file.write_bytes(raw)
                    with patch("app.agent._SYSTEM_PROMPT_FILE", prompt_file):
                        with self.assertRaises(SystemPromptLoadError):
                            load_system_prompt()

            prompt_file.write_bytes(b"x" * (256 * 1024 + 1))
            with patch("app.agent._SYSTEM_PROMPT_FILE", prompt_file):
                with self.assertRaises(SystemPromptLoadError):
                    load_system_prompt()

            prompt_file.write_text("safe", encoding="utf-8")
            real_info = os.lstat(prompt_file)
            fake_base = {
                "st_dev": real_info.st_dev,
                "st_ino": real_info.st_ino,
                "st_nlink": 1,
                "st_size": real_info.st_size,
                "st_mtime_ns": real_info.st_mtime_ns,
            }
            for unsafe_info in (
                SimpleNamespace(**fake_base, st_mode=stat.S_IFLNK | 0o777, st_file_attributes=0),
                SimpleNamespace(**fake_base, st_mode=stat.S_IFREG | 0o600, st_file_attributes=0x400),
            ):
                with self.subTest(file_kind="link_or_reparse"), patch(
                    "app.agent._SYSTEM_PROMPT_FILE", prompt_file
                ), patch("app.agent.os.lstat", return_value=unsafe_info):
                    with self.assertRaises(SystemPromptLoadError):
                        load_system_prompt()

            changed_info = SimpleNamespace(
                **(fake_base | {"st_mtime_ns": real_info.st_mtime_ns + 1}),
                st_mode=real_info.st_mode,
                st_file_attributes=0,
            )
            with patch("app.agent._SYSTEM_PROMPT_FILE", prompt_file), patch(
                "app.agent.os.lstat", side_effect=[real_info, changed_info]
            ):
                with self.assertRaises(SystemPromptLoadError):
                    load_system_prompt()

            hard_link = root / "prompt-copy.md"
            os.link(prompt_file, hard_link)
            with patch("app.agent._SYSTEM_PROMPT_FILE", prompt_file):
                with self.assertRaises(SystemPromptLoadError):
                    load_system_prompt()

    def test_text_is_delivered_incrementally_without_streaming_reasoning(self):
        response = [
            chunk(content="流", reasoning="隐藏"),
            chunk(content="式"),
            chunk(content="输出"),
            chunk(choices=False),
        ]
        agent, _ = self.agent([response])
        deltas: list[str] = []
        result = agent.run("测试流式", on_text_delta=deltas.append)
        self.assertEqual(result, "流式输出")
        self.assertEqual(deltas, ["流", "式", "输出"])
        self.assertEqual(agent.messages[-1].reasoning_content, "隐藏")

    def test_partial_stream_failure_appends_attempt_without_projecting_answer(self):
        def failing_stream():
            yield chunk(content="已收到一半")
            raise RuntimeError("SENSITIVE_STREAM_FAILURE")

        agent, _ = self.agent([failing_stream()])
        deltas: list[str] = []
        with self.assertRaisesRegex(AgentRequestError, "模型请求失败") as caught:
            agent.run("测试中断流", on_text_delta=deltas.append)
        self.assertNotIn("SENSITIVE", str(caught.exception))
        self.assertEqual(deltas, ["已收到一半"])
        self.assertEqual([message.role for message in agent.messages], ["system", "system"])
        self.assertNotIn("测试中断流", str(agent.to_sdk_messages()))
        self.assertEqual(
            [event.type for event in agent.session_events],
            [
                "turn/start",
                "user/message",
                "step/start",
                "assistant/attempt",
                "step/end",
                "turn/end",
            ],
        )
        attempt = agent.session_events[3].data
        self.assertEqual((attempt.kind, len(attempt.stream)), ("error", 1))
        self.assertIn('"content":"已收到一半"', attempt.stream[0].chunk_json)

    @patch("app.agent.call_tool")
    def test_fragmented_stream_tool_call_is_reassembled_by_index(self, mocked_call):
        mocked_call.return_value = ToolMessage(
            tool_call_id="call_1", result=ToolResult(status="success", output="ok")
        ).model_dump_json()
        fragmented = [
            chunk(calls=[(0, "call_", "read_", '{"path":')]),
            chunk(calls=[(0, "1", "file", '"a.py"}')]),
        ]
        agent, _ = self.agent([fragmented, reply(content="完成")])
        self.assertEqual(agent.run("读取"), "完成")
        call = mocked_call.call_args.args[0]
        self.assertEqual((call.id, call.name, call.arguments), ("call_1", "read_file", {"path": "a.py"}))

    @patch("app.agent.call_tool")
    def test_tool_call_event_is_committed_before_tool_execution(self, mocked_call):
        session = SessionEventWorkflow.in_memory()
        observed = []

        def execute(call, **_kwargs):
            observed.append(session.events[-1].type)
            return ToolMessage(
                tool_call_id=call.id,
                result=ToolResult(status="success", output="ok"),
            ).model_dump_json()

        mocked_call.side_effect = execute
        agent, _ = self.agent(
            [reply(calls=[("safe", "read_file", '{"path":"a.py"}')]), reply(content="完成")],
            session_workflow=session,
        )
        self.assertEqual(agent.run("读取"), "完成")
        self.assertEqual(observed, ["tool/call"])
        event_types = [event.type for event in session.events]
        self.assertLess(event_types.index("tool/call"), event_types.index("tool/result"))

    def test_agent_autonomously_inspects_and_resolves_unknown_write(self):
        session = SessionEventWorkflow.in_memory()
        old_turn = "a" * 32
        old_call = ToolCall(
            id="old-write",
            name="write_file",
            arguments={"path": "answer.txt", "content": "x", "mode": "create"},
        )
        session.append("turn/start", TurnStartEventData(turn_id=old_turn))
        session.append("user/message", UserMessageEventData(
            turn_id=old_turn,
            message=SessionUserMessageSnapshot(content="创建 answer.txt"),
        ))
        session.append("step/start", StepStartEventData(turn_id=old_turn, step=0))
        session.append("assistant/message", AssistantMessageEventData(
            turn_id=old_turn,
            step=0,
            stream=(),
            message=assistant_message_snapshot(AssistantMessage(tool_calls=[old_call])),
        ))
        session.append("tool/call", ToolCallEventData(
            turn_id=old_turn,
            step=0,
            call=tool_call_snapshot(old_call),
            retry_safety=tool_retry_safety(old_call),
        ))
        report = session.recover_incomplete()
        self.assertEqual(report.repaired[0].status, "outcome_unknown")

        target = {"turn_id": old_turn, "step": 0, "call_id": old_call.id}
        token = "c" * 64
        agent, client = self.agent([
            reply(calls=[(
                "inspect-current",
                "inspect_recovery",
                json.dumps(target),
            )]),
            reply(calls=[(
                "resolve-current",
                "resolve_recovery",
                json.dumps({
                    **target,
                    "inspection_token": token,
                    "resolution": "not_applied",
                    "evidence": "目标仍不存在，按当前可验证后置条件判定未应用。",
                }, ensure_ascii=False),
            )]),
            reply(content="已自主核验并解除恢复闸门。"),
        ], session_workflow=session)

        with tempfile.TemporaryDirectory() as root, patch(
            "tool.file_access._PROJECT_ROOT", Path(root)
        ), patch("app.recovery.token_hex", return_value=token):
            self.assertEqual(
                agent.run("继续自动恢复"),
                "已自主核验并解除恢复闸门。",
            )
            self.assertFalse((Path(root) / "answer.txt").exists())

        self.assertFalse(agent.pending_recovery())
        old_dispatches = [
            event for event in agent.session_events
            if event.type == "tool/call" and event.data.call.id == old_call.id
        ]
        self.assertEqual(len(old_dispatches), 1, "旧修改调用不得重放")
        resolutions = [
            event for event in agent.session_events
            if event.type == "recovery/resolution"
        ]
        self.assertEqual(len(resolutions), 1)
        self.assertEqual(resolutions[0].data.resolution, "confirmed_not_applied")

        self.assertIn(token, client.completions.requests[1]["messages"][-1]["content"])
        final_messages = client.completions.requests[2]["messages"]
        for index, message in enumerate(final_messages):
            calls = message.get("tool_calls", [])
            if calls:
                self.assertEqual(
                    [item["role"] for item in final_messages[index + 1:index + 1 + len(calls)]],
                    ["tool"] * len(calls),
                )
        resolve_index = next(
            index for index, message in enumerate(final_messages)
            if any(
                item["id"] == "resolve-current"
                for item in message.get("tool_calls", [])
            )
        )
        self.assertEqual(final_messages[resolve_index + 1]["role"], "tool")
        self.assertEqual(
            final_messages[resolve_index + 1]["tool_call_id"], "resolve-current"
        )
        self.assertEqual(final_messages[resolve_index + 2]["role"], "user")
        self.assertIn(
            "confirmed_not_applied",
            final_messages[resolve_index + 2]["content"],
        )
        agent.close()

    @patch("app.agent.call_tool")
    def test_session_commit_failure_prevents_tool_side_effect(self, mocked_call):
        session = SessionEventWorkflow.in_memory()
        agent, _ = self.agent(
            [reply(calls=[("safe", "read_file", '{"path":"a.py"}')])],
            session_workflow=session,
        )
        original = session.append

        def fail_call_event(event_type, data):
            if event_type == "tool/call":
                raise SessionEventError("SENSITIVE_STORAGE_FAILURE")
            return original(event_type, data)

        with patch.object(session, "append", side_effect=fail_call_event):
            with self.assertRaisesRegex(Exception, "会话事件无法可靠保存") as caught:
                agent.run("读取")
        self.assertNotIn("SENSITIVE", str(caught.exception))
        mocked_call.assert_not_called()

    def test_malformed_stream_indexes_and_non_iterable_response_are_rejected(self):
        malformed = [
            [chunk(calls=[(1, "call_1", "read_file", "{}")])],
            SimpleNamespace(choices=[]),
        ]
        for response in malformed:
            with self.subTest(response_type=type(response).__name__):
                agent, _ = self.agent([response])
                with self.assertRaises(AgentProtocolError):
                    agent.run("测试")

    def test_final_reply_without_nonblank_text_is_rejected(self):
        for content in (None, "", " \n"):
            with self.subTest(content=content):
                agent, _ = self.agent([reply(content=content)])
                with self.assertRaises(AgentProtocolError):
                    agent.ask("执行任务")
                # 无效回答和失败需求均不进入可执行上下文，保留固定撤销说明。
                self.assertEqual([item.role for item in agent.messages], ["system", "system"])
                self.assertNotIn("执行任务", str(agent.to_sdk_messages()))

    @patch("app.agent.call_tool")
    def test_multiple_tools_execute_and_full_order_is_sent_back(self, mocked_call):
        def result(call, **kwargs):
            return ToolMessage(
                tool_call_id=call.id,
                result=ToolResult(status="success", output=f"result-{call.id}"),
            ).model_dump_json()

        mocked_call.side_effect = result
        agent, client = self.agent([
            reply(
                content="先检查。",
                reasoning="兼容字段",
                calls=[
                    ("call_1", "read_file", '{"path":"a.py"}'),
                    ("call_2", "read_file", '{"path":"b.py","offset":1}'),
                ],
            ),
            reply(content="两个文件均已读取。"),
        ])

        agent.ask("读取文件")

        self.assertEqual(mocked_call.call_count, 2)
        self.assertEqual(
            [item.role for item in agent.messages],
            ["system", "user", "assistant", "tool", "tool", "assistant"],
        )
        sent = client.completions.requests[1]["messages"]
        self.assertEqual([item["role"] for item in sent], ["system", "user", "assistant", "tool", "tool"])
        self.assertEqual(sent[2]["reasoning_content"], "兼容字段")
        self.assertIsInstance(sent[2]["tool_calls"][0]["function"]["arguments"], str)
        self.assertEqual(json.loads(sent[2]["tool_calls"][0]["function"]["arguments"]), {"path": "a.py"})
        self.assertEqual(sent[3]["tool_call_id"], "call_1")
        self.assertEqual(json.loads(sent[3]["content"]), {"status": "success", "output": "result-call_1", "error": None})

    def test_message_adapter_sends_only_tool_result_json(self):
        message = ToolMessage(
            tool_call_id="call_9",
            result=ToolResult(status="error", error="工具失败"),
        )
        converted = message_to_sdk(message)
        self.assertEqual(set(converted), {"role", "tool_call_id", "content"})
        self.assertEqual(json.loads(converted["content"]), message.result.model_dump())
        assistant = AssistantMessage(
            tool_calls=[ToolCall(id="call_9", name="read_file", arguments={"path": "中文.py"})]
        )
        arguments = message_to_sdk(assistant)["tool_calls"][0]["function"]["arguments"]
        self.assertEqual(json.loads(arguments), {"path": "中文.py"})

    @patch("app.agent.call_tool")
    def test_malformed_or_non_object_arguments_never_execute_tools(self, mocked_call):
        for arguments in ("not-json", "[]", "null", "{\"x\":NaN}"):
            with self.subTest(arguments=arguments):
                agent, _ = self.agent([reply(calls=[("bad", "read_file", arguments)])])
                with self.assertRaises(AgentProtocolError):
                    agent.run("读取")
        mocked_call.assert_not_called()

    @patch("app.agent.call_tool")
    def test_tool_round_limit_stops_before_excess_call(self, mocked_call):
        mocked_call.side_effect = lambda call, **kwargs: ToolMessage(
            tool_call_id=call.id,
            result=ToolResult(status="success", output="ok"),
        ).model_dump_json()
        agent, _ = self.agent([
            reply(calls=[("one", "read_file", '{"path":"a"}')]),
            reply(calls=[("two", "read_file", '{"path":"b"}')]),
        ], max_tool_rounds=1)
        with self.assertRaises(AgentLoopLimitError):
            agent.run("循环")
        mocked_call.assert_called_once()
        self.assertEqual(
            [item.role for item in agent.messages],
            ["system", "user", "assistant", "tool", "assistant", "tool"],
        )
        self.assertEqual(agent.messages[-1].tool_call_id, "two")
        self.assertEqual(agent.messages[-1].result.status, "error")

    @patch("app.agent.call_tool")
    def test_one_assistant_step_may_close_multiple_calls_before_next_step(
        self, mocked_call
    ):
        mocked_call.side_effect = lambda tool_call, **kwargs: ToolMessage(
            tool_call_id=tool_call.id,
            result=ToolResult(status="success", output=tool_call.name),
        ).model_dump_json()
        agent, _ = self.agent(
            [
                reply(calls=[
                    ("first", "read_file", '{"path":"a.py"}'),
                    ("second", "list_directory", '{"path":"."}'),
                ]),
                reply(content="两个结果都已处理。"),
            ],
            max_tool_rounds=1,
        )

        self.assertEqual(agent.run("检查两个位置"), "两个结果都已处理。")
        self.assertEqual(mocked_call.call_count, 2)
        events = agent.session_events
        first_step_end = next(
            event.seq
            for event in events
            if event.type == "step/end" and event.data.step == 0
        )
        first_step_results = [
            event
            for event in events
            if event.type == "tool/result" and event.data.step == 0
        ]
        second_step_start = next(
            event.seq
            for event in events
            if event.type == "step/start" and event.data.step == 1
        )
        self.assertEqual(
            [event.data.call_id for event in first_step_results],
            ["first", "second"],
        )
        self.assertTrue(all(event.seq < first_step_end for event in first_step_results))
        self.assertLess(first_step_end, second_step_start)
        agent.close()

    @patch("app.agent.call_tool")
    def test_confirmation_callback_is_injected_into_registry_call(self, mocked_call):
        callback = Mock(return_value=True)
        observer = Mock(spec=Observability)
        mocked_call.return_value = ToolMessage(
            tool_call_id="danger",
            result=ToolResult(status="denied", error="模拟拒绝"),
        ).model_dump_json()
        agent, _ = self.agent([
            reply(calls=[(
                "danger",
                "bash_shell",
                '{"command":"remove_file","paths":["tmp/file.txt"]}',
            )]),
            reply(content="操作未执行。"),
        ], confirmation_callback=callback, observability=observer)
        self.assertEqual(agent.run("删除文件"), "操作未执行。")
        mocked_call.assert_called_once()
        forwarded = mocked_call.call_args.kwargs["confirmation_callback"]
        request = object()
        self.assertIs(forwarded(request), True)
        callback.assert_called_once_with(request)
        self.assertIs(mocked_call.call_args.kwargs["observability"], observer)

    def test_cli_confirmation_only_accepts_explicit_yes(self):
        from app.main import _confirm_tool_operation
        from tool.bash_shell import ToolConfirmationRequest

        request = ToolConfirmationRequest(
            tool_name="bash_shell",
            operation="remove_file",
            paths=("tmp/file.txt",),
            description="永久删除一个项目文件",
            request_id="request_1",
        )
        for answer, expected in (("yes", True), (" YES ", True), ("y", False), ("", False)):
            with self.subTest(answer=answer), patch("builtins.input", return_value=answer):
                self.assertIs(_confirm_tool_operation(request), expected)
        with patch("builtins.input", side_effect=EOFError):
            self.assertFalse(_confirm_tool_operation(request))

        diff_request = ToolConfirmationRequest(
            tool_name="editor_file",
            operation="edit_replace_text",
            paths=("app/file.py",),
            description="审阅文件 diff 并提交正式文件",
            request_id="a" * 32,
            phase="diff",
            diff_preview="--- before\n+++ after\n-old\n+new\n",
            pending_file="data/pending/" + "b" * 32 + ".bin",
        )
        terminal = io.StringIO()
        with patch("builtins.input", return_value="yes"), patch("sys.stdout", terminal):
            self.assertTrue(_confirm_tool_operation(diff_request))
        self.assertIn("目标路径：app/file.py", terminal.getvalue())
        self.assertIn("diff 预览不在终端显示", terminal.getvalue())
        self.assertNotIn("-old", terminal.getvalue())
        self.assertNotIn("+new", terminal.getvalue())
        self.assertIn(diff_request.pending_file, terminal.getvalue())
        with patch("builtins.input", return_value="no"), patch("sys.stdout", io.StringIO()):
            self.assertFalse(_confirm_tool_operation(diff_request))

    def test_cli_input_broker_routes_approval_through_single_reader(self):
        from app.main import _InputBroker
        from tool.bash_shell import ToolConfirmationRequest

        inputs: Queue[str] = Queue()
        inputs.put("yes")
        reads: list[tuple[str, bool]] = []

        def reader(prompt: str, complete_commands: bool) -> str:
            reads.append((prompt, complete_commands))
            return inputs.get(timeout=2)

        broker = _InputBroker(reader=reader)
        broker.start()
        request = ToolConfirmationRequest(
            tool_name="bash_shell",
            operation="unittest",
            paths=("tests",),
            description="执行测试",
            request_id="a" * 32,
        )
        result: list[bool] = []
        waiter = Thread(target=lambda: result.append(broker.confirm(request)))
        waiter.start()
        approval = None
        for _ in range(100):
            approval = broker.poll_approval()
            if approval is not None:
                break
            time.sleep(0.001)
        self.assertIsNotNone(approval)
        # 审批到达前已经挂起的主命令读取由审批显式接管；不能再启动
        # 第二个 stdin reader，也不能把 yes 遗留给下一轮普通命令。
        broker.request_line("› act ", complete_commands=True)
        self.assertTrue(broker.request_or_adopt_approval_line())
        answer = broker.get_line(timeout=2)
        broker.resolve_approval(approval, answer)
        waiter.join(2)
        self.assertEqual(result, [True])
        self.assertEqual(reads, [("› act ", True)])
        broker.close()

    def test_input_broker_enqueues_approval_before_close_can_drain(self):
        from app.main import _InputBroker
        from tool.bash_shell import ToolConfirmationRequest

        broker = _InputBroker()
        original_put = broker._approvals.put
        enqueued = Event()
        atomicity_violated = Event()

        def guarded_put(value):
            if not broker._lock.locked():
                atomicity_violated.set()
            original_put(value)
            enqueued.set()

        request = ToolConfirmationRequest(
            tool_name="bash_shell",
            operation="unittest",
            paths=("tests",),
            description="执行测试",
            request_id="c" * 32,
        )
        result: list[bool] = []
        with patch.object(broker._approvals, "put", side_effect=guarded_put):
            waiter = Thread(target=lambda: result.append(broker.confirm(request)))
            waiter.start()
            try:
                self.assertTrue(enqueued.wait(1))
            finally:
                broker.close()
                waiter.join(1)

        self.assertFalse(waiter.is_alive())
        self.assertFalse(atomicity_violated.is_set())
        self.assertEqual(result, [False])
        self.assertIsNone(broker.poll_approval())

    def test_input_broker_enqueues_plan_choice_before_close_can_drain(self):
        from app.main import _InputBroker

        broker = _InputBroker()
        original_put = broker._plan_choices.put
        enqueued = Event()
        atomicity_violated = Event()

        def guarded_put(value):
            if not broker._lock.locked():
                atomicity_violated.set()
            original_put(value)
            enqueued.set()

        result: list[int | None] = []
        with patch.object(broker._plan_choices, "put", side_effect=guarded_put):
            waiter = Thread(
                target=lambda: result.append(
                    broker.choose_plan_option(SimpleNamespace(id="plan-race"))
                )
            )
            waiter.start()
            try:
                self.assertTrue(enqueued.wait(1))
            finally:
                broker.close()
                waiter.join(1)

        self.assertFalse(waiter.is_alive())
        self.assertFalse(atomicity_violated.is_set())
        self.assertEqual(result, [None])
        self.assertIsNone(broker.poll_plan_choice())

    def test_input_broker_resolves_concurrent_approvals_by_exact_request(self):
        from app.main import _InputBroker
        from tool.bash_shell import ToolConfirmationRequest

        broker = _InputBroker()
        request_count = 8
        all_enqueued = Event()
        atomicity_violated = Event()
        count_lock = Lock()
        enqueued_count = 0
        original_put = broker._approvals.put

        def tracked_put(value):
            nonlocal enqueued_count
            if not broker._lock.locked():
                atomicity_violated.set()
            original_put(value)
            with count_lock:
                enqueued_count += 1
                if enqueued_count == request_count:
                    all_enqueued.set()

        results: dict[str, bool] = {}
        result_lock = Lock()

        def wait_for_approval(request):
            approved = broker.confirm(request)
            with result_lock:
                results[request.request_id] = approved

        requests = tuple(
            ToolConfirmationRequest(
                tool_name="bash_shell",
                operation="unittest",
                paths=(f"tests/case-{index}",),
                description="执行测试",
                request_id=f"{index:032x}",
            )
            for index in range(request_count)
        )
        with patch.object(broker._approvals, "put", side_effect=tracked_put):
            waiters = [Thread(target=wait_for_approval, args=(request,)) for request in requests]
            for waiter in waiters:
                waiter.start()
            try:
                self.assertTrue(all_enqueued.wait(2))

                pending = []
                for _ in range(request_count):
                    approval = broker.poll_approval()
                    self.assertIsNotNone(approval)
                    pending.append(approval)
                self.assertIsNone(broker.poll_approval())
                for approval in reversed(pending):
                    expected = int(approval.request.request_id, 16) % 2 == 0
                    broker.resolve_approval(approval, "yes" if expected else "no")
            finally:
                broker.close()
                for waiter in waiters:
                    waiter.join(1)

        self.assertTrue(all(not waiter.is_alive() for waiter in waiters))
        self.assertFalse(atomicity_violated.is_set())
        self.assertEqual(
            results,
            {
                request.request_id: int(request.request_id, 16) % 2 == 0
                for request in requests
            },
        )

    def test_multiple_user_turns_send_complete_history(self):
        agent, client = self.agent([
            reply(content="第一轮回答"),
            reply(content="第二轮回答"),
        ])
        self.assertEqual(agent.ask("第一问"), "第一轮回答")
        self.assertEqual(agent.ask("第二问"), "第二轮回答")
        self.assertEqual(
            [item["role"] for item in client.completions.requests[1]["messages"]],
            ["system", "user", "assistant", "user"],
        )

    def test_client_failure_is_sanitized(self):
        agent, _ = self.agent([RuntimeError("fake-key secret")])
        with self.assertRaisesRegex(AgentRequestError, "模型请求失败") as raised:
            agent.run("任务")
        self.assertNotIn("fake-key", str(raised.exception))

    def test_invalid_tool_result_or_correlation_is_rejected(self):
        agent, _ = self.agent([reply(calls=[("expected", "read_file", '{}')])])
        wrong = ToolMessage(
            tool_call_id="other", result=ToolResult(status="success", output="ok")
        ).model_dump_json()
        with patch("app.agent.call_tool", return_value=wrong):
            with self.assertRaises(AgentProtocolError):
                agent.run("任务")
        self.assertEqual(
            [item.role for item in agent.messages],
            ["system", "user", "assistant", "tool"],
        )
        self.assertEqual(agent.messages[-1].tool_call_id, "expected")

    def test_messages_property_returns_deep_copies(self):
        agent, _ = self.agent([reply(content="原始回答")])
        agent.run("原始问题")
        snapshot = agent.messages
        snapshot[1].content = "被外部修改"
        self.assertEqual(agent.messages[1].content, "原始问题")

    def test_main_entry_prints_final_text_and_closes_client(self):
        from app import main as main_module

        def build_test_agent(*args, **kwargs):
            kwargs["session_workflow"].close()
            kwargs["session_workflow"] = SessionEventWorkflow.in_memory()
            return CodingAgent(*args, **kwargs)

        client = FakeClient([reply(content="入口完成")])
        observer = Mock(spec=Observability)
        maintenance = Mock()
        lifecycle = Mock()
        maintenance.close.side_effect = lambda: lifecycle("close")
        observer.flush_pending.side_effect = lambda: lifecycle("flush")
        stdout = io.StringIO()
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as root:
            with (
                patch.object(main_module, "_SESSIONS_ROOT", Path(root) / "sessions"),
                patch.object(main_module, "load_settings", return_value=Settings(api_key="fake")),
                patch.object(main_module, "create_client", return_value=client),
                patch.object(main_module, "Observability", return_value=observer),
                patch.object(
                    main_module,
                    "CodingAgent",
                    side_effect=build_test_agent,
                ) as agent_factory,
                patch.object(main_module, "MaintenanceService", return_value=maintenance) as service_factory,
                patch("builtins.input", side_effect=["处理任务", "exit"]),
                patch("sys.stdout", stdout),
                patch("sys.stderr", stderr),
            ):
                self.assertEqual(main_module.main([]), 0)
        self.assertIn("入口完成", stdout.getvalue())
        self.assertEqual(stdout.getvalue().count("入口完成"), 1)
        self.assertIn("│ 入口完成", stdout.getvalue())
        self.assertEqual(stderr.getvalue(), "")
        self.assertTrue(client.closed)
        self.assertIs(agent_factory.call_args.kwargs["observability"], observer)
        service_factory.assert_called_once_with(observer)
        maintenance.start.assert_called_once_with()
        maintenance.close.assert_called_once_with()
        self.assertEqual(lifecycle.call_args_list, [call("close"), call("flush")])

    def test_main_does_not_close_session_on_background_read_ahead_eof(self):
        from app import main as main_module

        class StubAgent:
            def __init__(self):
                self.prompts: list[str] = []
                self.interrupts: list[str] = []
                self.closed = False
                self.workflow = None

            def pending_recovery(self):
                return ()

            def run(self, prompt, *, on_text_delta=None):
                self.prompts.append(prompt)
                if len(self.prompts) == 1:
                    # Ensure main enters the active-run input polling branch.
                    time.sleep(0.12)
                return f"完成：{prompt}"

            def interrupt(self, reason):
                self.interrupts.append(reason)

            def close(self):
                self.closed = True
                if self.workflow is not None:
                    self.workflow.close()

        built = StubAgent()
        client = Mock()
        observer = Mock(spec=Observability)
        maintenance = Mock()
        inputs: Queue[object] = Queue()
        for item in ("第一项任务", EOFError(), "第二项任务", "exit"):
            inputs.put(item)

        def read_input(*_args):
            item = inputs.get(timeout=2)
            if isinstance(item, BaseException):
                raise item
            return item

        def build_stub(*_args, **kwargs):
            kwargs["session_workflow"].close()
            return built

        with tempfile.TemporaryDirectory() as root:
            with (
                patch.object(main_module, "_SESSIONS_ROOT", Path(root) / "sessions"),
                patch.object(main_module, "load_settings", return_value=Settings(api_key="fake")),
                patch.object(main_module, "create_client", return_value=client),
                patch.object(main_module, "Observability", return_value=observer),
                patch.object(main_module, "CodingAgent", side_effect=build_stub),
                patch.object(main_module, "MaintenanceService", return_value=maintenance),
                patch("builtins.input", side_effect=read_input),
                patch("sys.stdout", io.StringIO()),
                patch("sys.stderr", io.StringIO()),
            ):
                self.assertEqual(main_module.main([]), 0)

        self.assertEqual(built.prompts, ["第一项任务", "第二项任务"])
        self.assertEqual(built.interrupts, [])
        self.assertTrue(built.closed)

    def test_main_initialization_failure_never_creates_maintenance_thread(self):
        from app import main as main_module

        client = FakeClient([])
        observer = Mock(spec=Observability)
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as root:
            with (
                patch.object(main_module, "_SESSIONS_ROOT", Path(root) / "sessions"),
                patch.object(main_module, "load_settings", return_value=Settings(api_key="fake")),
                patch.object(main_module, "create_client", return_value=client),
                patch.object(main_module, "Observability", return_value=observer),
                patch.object(main_module, "CodingAgent", side_effect=ValueError("SENSITIVE_FAILURE")),
                patch.object(main_module, "MaintenanceService") as service_factory,
                patch("builtins.input", return_value="触发初始化"),
                patch("sys.stdout", stdout),
                patch("sys.stderr", io.StringIO()),
            ):
                self.assertEqual(main_module.main([]), 2)

        service_factory.assert_not_called()
        observer.flush_pending.assert_called_once_with()
        self.assertTrue(client.closed)

    def test_main_maintenance_start_failure_does_not_block_agent(self):
        from app import main as main_module

        def build_test_agent(*args, **kwargs):
            kwargs["session_workflow"].close()
            kwargs["session_workflow"] = SessionEventWorkflow.in_memory()
            return CodingAgent(*args, **kwargs)

        client = FakeClient([reply(content="维护失败不影响回答")])
        observer = Mock(spec=Observability)
        maintenance = Mock()
        maintenance.start.side_effect = RuntimeError("SENSITIVE_MAINTENANCE_FAILURE")
        stdout = io.StringIO()
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as root:
            with (
                patch.object(main_module, "_SESSIONS_ROOT", Path(root) / "sessions"),
                patch.object(main_module, "load_settings", return_value=Settings(api_key="fake")),
                patch.object(main_module, "create_client", return_value=client),
                patch.object(main_module, "Observability", return_value=observer),
                patch.object(main_module, "CodingAgent", side_effect=build_test_agent),
                patch.object(main_module, "MaintenanceService", return_value=maintenance),
                patch("builtins.input", side_effect=["处理任务", "exit"]),
                patch("sys.stdout", stdout),
                patch("sys.stderr", stderr),
            ):
                self.assertEqual(main_module.main([]), 0)

        self.assertIn("Coding Agent", stdout.getvalue())
        self.assertNotIn("SENSITIVE_MAINTENANCE_FAILURE", stdout.getvalue())
        self.assertEqual(stderr.getvalue(), "")
        maintenance.close.assert_called_once_with()
        observer.flush_pending.assert_called_once_with()
        self.assertTrue(client.closed)

    def test_main_accepts_text_interrupt_while_streaming(self):
        from app import main as main_module

        old_started = Event()
        release_old = Event()
        new_started = Event()
        inputs: Queue[str] = Queue()
        inputs.put("处理任务")

        def old_generation():
            old_started.set()
            yield chunk(content="旧流")
            release_old.wait(2)
            yield chunk(content="迟到流")

        def new_generation():
            new_started.set()
            yield chunk(content="紧急重评估完成")

        client = FakeClient([old_generation(), new_generation()])
        observer = Mock(spec=Observability)
        maintenance = Mock()
        created: list[CodingAgent] = []

        def build_test_agent(*args, **kwargs):
            kwargs["session_workflow"].close()
            kwargs["session_workflow"] = SessionEventWorkflow.in_memory()
            built = CodingAgent(*args, **kwargs)
            created.append(built)
            return built

        def produce_commands():
            self.assertTrue(old_started.wait(2))
            inputs.put("/interrupt 停止旧流")
            time.sleep(0.02)
            release_old.set()
            self.assertTrue(new_started.wait(2))
            time.sleep(0.1)
            inputs.put("exit")

        producer = Thread(target=produce_commands)
        producer.start()
        stdout = io.StringIO()
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as root:
            with (
                patch.object(main_module, "_SESSIONS_ROOT", Path(root) / "sessions"),
                patch.object(main_module, "load_settings", return_value=Settings(api_key="fake")),
                patch.object(main_module, "create_client", return_value=client),
                patch.object(main_module, "Observability", return_value=observer),
                patch.object(main_module, "CodingAgent", side_effect=build_test_agent),
                patch.object(main_module, "MaintenanceService", return_value=maintenance),
                patch("builtins.input", side_effect=lambda *_args: inputs.get(timeout=3)),
                patch("sys.stdout", stdout),
                patch("sys.stderr", stderr),
            ):
                self.assertEqual(main_module.main([]), 0)
        producer.join(2)
        self.assertFalse(producer.is_alive())
        self.assertIn("紧急重评估完成", stdout.getvalue())
        self.assertIn("紧急事件已追加", stdout.getvalue())
        self.assertTrue(any(
            event.type == "host/urgent" for event in created[0].session_events
        ))

    def test_async_tool_has_one_placeholder_and_terminal_event_reenters_context(self):
        responses = [
            reply(calls=[("async_call", "code_interpreter", '{"code":"1+1"}')]),
            reply(content="后台计算已经启动。"),
            reply(content="后台计算已经完成。"),
        ]
        agent, client = self.agent(responses)
        def delayed_result(*_args, **_kwargs):
            time.sleep(0.15)
            return ToolResult(status="success", output="2")
        with patch(
            "tool.registry._execute",
            side_effect=delayed_result,
        ):
            result = agent.run("计算")
        self.assertEqual(result, "后台计算已经完成。")
        event_types = [item.type for item in agent.session_events]
        self.assertIn("async/task/accepted", event_types)
        self.assertIn("async/task/started", event_types)
        self.assertIn("async/task/progress", event_types)
        self.assertIn("async/task/completed", event_types)
        self.assertLess(event_types.index("tool/result"), event_types.index("async/task/started"))
        final_messages = client.completions.requests[-1]["messages"]
        tool_messages = [item for item in final_messages if item["role"] == "tool"]
        self.assertEqual(len(tool_messages), 1)
        placeholder = json.loads(tool_messages[0]["content"])
        self.assertEqual(placeholder["status"], "accepted")
        self.assertTrue(any(
            item["role"] == "user" and "async/task/completed" in item["content"]
            for item in final_messages
        ))
        self.assertEqual(
            [event.type for event in agent.session_events].count("turn/start"), 1
        )
        self.assertEqual(
            [event.type for event in agent.session_events].count("user/message"), 1
        )
        self.assertTrue(any(
            event.type == "step/end" and event.data.kind == "awaiting_async"
            for event in agent.session_events
        ))
        self.assertFalse(any(
            item["role"] == "user"
            and "请处理事件流中刚到达" in item["content"]
            for item in final_messages
        ))
        agent.close()

    def test_async_terminal_arriving_during_provisional_answer_gets_a_followup_step(self):
        provisional_started = Event()
        release_provisional = Event()

        def provisional_response():
            provisional_started.set()
            release_provisional.wait(2)
            yield chunk(content="后台计算正在进行。")

        responses = [
            reply(calls=[("async_call", "code_interpreter", '{"code":"1+1"}')]),
            provisional_response(),
            reply(content="后台计算已经完成。"),
        ]
        agent, client = self.agent(responses)

        def finish_while_model_is_streaming(*_args, **_kwargs):
            self.assertTrue(provisional_started.wait(2))
            release_provisional.set()
            return ToolResult(status="success", output="2")

        with patch("tool.registry._execute", side_effect=finish_while_model_is_streaming):
            result = agent.run("计算")

        self.assertEqual(result, "后台计算已经完成。")
        self.assertEqual(len(client.completions.requests), 3)
        self.assertEqual(
            [event.type for event in agent.session_events].count("turn/start"), 1
        )
        self.assertEqual(
            [event.type for event in agent.session_events].count("user/message"), 1
        )
        agent.close()

    def test_urgent_event_wakes_async_wait_without_waiting_for_task_completion(self):
        task_started = Event()
        release_task = Event()
        wait_entered = Event()
        urgent_reassessed = Event()

        def delayed_result(*_args, **_kwargs):
            task_started.set()
            release_task.wait(3)
            return ToolResult(status="success", output="2")

        def reassessment_response():
            urgent_reassessed.set()
            yield chunk(content="已立即响应中断；后台任务仍可继续。")

        agent, _client = self.agent([
            reply(calls=[("async_call", "code_interpreter", '{"code":"1+1"}')]),
            reply(content="后台计算正在进行。"),
            reassessment_response(),
            reply(content="后台计算最终完成。"),
        ])
        original_wait = agent._tasks.wait_for_change
        observed_timeouts: list[float | None] = []

        def observed_wait(session_id, previous, *, timeout=None):
            observed_timeouts.append(timeout)
            wait_entered.set()
            return original_wait(session_id, previous, timeout=timeout)

        outcome: list[object] = []

        def run_agent():
            try:
                outcome.append(agent.run("计算"))
            except BaseException as exc:  # pragma: no cover - assertion reports it
                outcome.append(exc)

        with (
            patch("tool.registry._execute", side_effect=delayed_result),
            patch.object(agent._tasks, "wait_for_change", side_effect=observed_wait),
        ):
            worker = Thread(target=run_agent)
            worker.start()
            self.assertTrue(task_started.wait(2))
            self.assertTrue(wait_entered.wait(2))
            agent.interrupt("先处理新的紧急指令")
            self.assertTrue(urgent_reassessed.wait(1))
            self.assertFalse(release_task.is_set())
            release_task.set()
            worker.join(3)

        self.assertFalse(worker.is_alive())
        self.assertEqual(outcome, ["后台计算最终完成。"])
        self.assertTrue(observed_timeouts)
        self.assertTrue(all(timeout == 0.1 for timeout in observed_timeouts))
        self.assertLess(len(observed_timeouts), 20)
        self.assertEqual(
            [event.type for event in agent.session_events].count("turn/start"), 1
        )
        agent.close()

    def test_urgent_event_fences_late_stream_chunks_and_reinvokes_model(self):
        started = Event()
        release = Event()

        def old_generation():
            started.set()
            yield chunk(content="旧回答")
            release.wait(2)
            yield chunk(content="迟到内容")

        agent, _client = self.agent([
            old_generation(),
            reply(content="已按紧急事件重新评估。"),
        ])
        outcome: list[object] = []

        def run_agent():
            try:
                outcome.append(agent.run("开始"))
            except BaseException as exc:  # pragma: no cover - assertion reports it
                outcome.append(exc)

        worker = Thread(target=run_agent)
        worker.start()
        self.assertTrue(started.wait(2))
        agent.interrupt("停止旧回答")
        release.set()
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(outcome, ["已按紧急事件重新评估。"])
        self.assertIn("host/urgent", [event.type for event in agent.session_events])
        committed_answers = [
            event.data.message.content
            for event in agent.session_events
            if event.type == "assistant/message"
        ]
        self.assertNotIn("旧回答", committed_answers)
        self.assertNotIn("迟到内容", committed_answers)
        agent.close()


if __name__ == "__main__":
    unittest.main()
