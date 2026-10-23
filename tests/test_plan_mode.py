from __future__ import annotations

import json
import unittest

from pydantic import ValidationError

from app.plan_mode import (
    PlanChoiceOption,
    PlanClarificationQuestion,
    PlanModelResponse,
    build_clarification_answer,
    build_persisted_plan_request,
    build_plan_request_prompt,
    parse_plan_model_response,
    render_plan_document,
    render_plan_document_chunks,
)


def final_plan_payload() -> dict[str, object]:
    return {
        "kind": "final",
        "plan": {
            "title": "会话恢复改进计划",
            "objective": "让中断后的会话可以安全恢复。",
            "in_scope": ["恢复可靠事件"],
            "out_of_scope": ["重放未完成工具"],
            "assumptions": ["事件存储可用"],
            "steps": [
                {
                    "number": 1,
                    "title": "重建上下文",
                    "actions": ["读取已可靠提交的事件", "忽略半截模型流"],
                    "verification": "恢复测试通过",
                }
            ],
            "acceptance_criteria": ["历史消息顺序一致"],
            "risks": [
                {"risk": "工具结果未知", "mitigation": "要求外部核验"}
            ],
            "open_questions": [],
        },
    }


def clarification_payload(question_id: str = "runtime") -> dict[str, object]:
    return {
        "kind": "clarify",
        "clarification": {
            "id": question_id,
            "question": "优先支持哪个运行环境？",
            "options": [
                {"label": "Windows", "description": "优先当前环境", "recommended": True},
                {"label": "Ubuntu", "description": "优先部署环境", "recommended": False},
                {"label": "双平台", "description": "增加兼容测试成本", "recommended": False},
            ],
        },
    }


class PlanModeTests(unittest.TestCase):
    def test_final_response_parses_and_renders_readable_plan(self):
        response = parse_plan_model_response(json.dumps(final_plan_payload(), ensure_ascii=False))
        self.assertIsInstance(response, PlanModelResponse)
        rendered = render_plan_document(response.plan)
        chunks = render_plan_document_chunks(response.plan)
        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(chunks), rendered)
        self.assertTrue(chunks[0].startswith("# 会话恢复改进计划"))
        self.assertIn("# 会话恢复改进计划", rendered)
        self.assertIn("## 实施步骤", rendered)
        self.assertIn("1. 重建上下文", rendered)
        self.assertIn("验证：恢复测试通过", rendered)
        self.assertNotIn("尚待确认", rendered)

    def test_clarification_requires_exactly_three_distinct_options_and_at_most_one_recommended(self):
        valid = parse_plan_model_response(json.dumps(clarification_payload(), ensure_ascii=False))
        self.assertEqual(len(valid.clarification.options), 3)
        for mutate in ("two", "duplicate", "recommended"):
            payload = clarification_payload()
            options = payload["clarification"]["options"]
            if mutate == "two":
                options.pop()
            elif mutate == "duplicate":
                options[1]["label"] = options[0]["label"]
            else:
                options[1]["recommended"] = True
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                parse_plan_model_response(json.dumps(payload, ensure_ascii=False))

    def test_answer_is_data_not_command_and_persisted_request_records_selection(self):
        response = parse_plan_model_response(json.dumps(clarification_payload(), ensure_ascii=False))
        question = response.clarification
        encoded = build_clarification_answer(question, 2)
        decoded = json.loads(encoded)
        self.assertEqual(decoded["type"], "plan_clarification_answer")
        self.assertEqual(decoded["selected_option"]["index"], 3)
        self.assertNotIn("command", decoded)
        persisted = build_persisted_plan_request("改进恢复", [(question, 2)])
        self.assertIn("双平台", persisted)
        for invalid in (-1, 3, True, "1"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                build_clarification_answer(question, invalid)

    def test_protocol_prompt_contains_schema_and_strict_parser_rejects_unsafe_shapes(self):
        prompt = build_plan_request_prompt("设计一个会话系统")
        self.assertIn("JSON Schema", prompt)
        self.assertIn("恰好 3 个", prompt)
        self.assertIn("严禁调用工具", prompt)
        self.assertIn("open_questions 必须是空数组", prompt)
        unresolved = final_plan_payload()
        unresolved["plan"]["open_questions"] = ["还要选择运行环境"]
        invalid_contents = (
            "```json\n{}\n```",
            json.dumps({"kind": "final", "plan": final_plan_payload()["plan"], "extra": 1}),
            json.dumps({"kind": "clarify", "clarification": None}),
            json.dumps(unresolved, ensure_ascii=False),
        )
        for content in invalid_contents:
            with self.subTest(content=content[:20]), self.assertRaises(ValueError):
                parse_plan_model_response(content)

        question = clarification_payload()["clarification"]
        question["question"] = "bad\x1b[31m"
        with self.assertRaises(ValueError):
            parse_plan_model_response(json.dumps({"kind": "clarify", "clarification": question}))


if __name__ == "__main__":
    unittest.main()
