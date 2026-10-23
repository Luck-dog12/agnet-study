"""模型驱动 Plan 模式的严格协议、渲染和澄清答案编码。"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable
from typing import Literal, Self, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_MAX_MODEL_RESPONSE_CHARACTERS = 256 * 1024
_QUESTION_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}")


class _StrictPlanModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


def _safe_single_line(value: str) -> str:
    if not value.strip() or value != value.strip() or any(
        unicodedata.category(character) in {"Cc", "Cf"} for character in value
    ):
        raise ValueError("Plan 文本必须是无控制字符的非空单行文本")
    return value


class PlanChoiceOption(_StrictPlanModel):
    label: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=300)
    recommended: bool = False

    _validate_label = field_validator("label")(_safe_single_line)
    _validate_description = field_validator("description")(_safe_single_line)


class PlanClarificationQuestion(_StrictPlanModel):
    id: str = Field(min_length=1, max_length=64)
    question: str = Field(min_length=1, max_length=500)
    options: tuple[PlanChoiceOption, PlanChoiceOption, PlanChoiceOption]

    _validate_question = field_validator("question")(_safe_single_line)

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if _QUESTION_ID.fullmatch(value) is None:
            raise ValueError("澄清问题 id 必须是规范 ASCII 标识")
        return value

    @model_validator(mode="after")
    def validate_options(self) -> Self:
        labels = [item.label.casefold() for item in self.options]
        if len(set(labels)) != 3:
            raise ValueError("每个澄清问题必须提供 3 个不同选项")
        if sum(item.recommended for item in self.options) > 1:
            raise ValueError("每个澄清问题最多标记 1 个推荐项")
        return self


class PlanStep(_StrictPlanModel):
    number: int = Field(ge=1, le=20)
    title: str = Field(min_length=1, max_length=160)
    actions: list[str] = Field(min_length=1, max_length=12)
    verification: str = Field(min_length=1, max_length=500)

    _validate_title = field_validator("title")(_safe_single_line)
    _validate_verification = field_validator("verification")(_safe_single_line)

    @field_validator("actions")
    @classmethod
    def validate_actions(cls, values: list[str]) -> list[str]:
        return [_safe_single_line(value) for value in values]


class PlanRisk(_StrictPlanModel):
    risk: str = Field(min_length=1, max_length=300)
    mitigation: str = Field(min_length=1, max_length=500)

    _validate_risk = field_validator("risk")(_safe_single_line)
    _validate_mitigation = field_validator("mitigation")(_safe_single_line)


class PlanDocument(_StrictPlanModel):
    title: str = Field(min_length=1, max_length=160)
    objective: str = Field(min_length=1, max_length=1000)
    in_scope: list[str] = Field(min_length=1, max_length=20)
    out_of_scope: list[str] = Field(max_length=20)
    assumptions: list[str] = Field(max_length=20)
    steps: list[PlanStep] = Field(min_length=1, max_length=20)
    acceptance_criteria: list[str] = Field(min_length=1, max_length=20)
    risks: list[PlanRisk] = Field(max_length=20)
    # 保留字段以兼容既有供应商输出，但 final 不能携带未解决问题。
    # 信息不足必须走 kind=clarify 的结构化暂停/继续循环。
    open_questions: list[str] = Field(max_length=0)

    _validate_title = field_validator("title")(_safe_single_line)
    _validate_objective = field_validator("objective")(_safe_single_line)

    @field_validator(
        "in_scope", "out_of_scope", "assumptions", "acceptance_criteria", "open_questions"
    )
    @classmethod
    def validate_text_lists(cls, values: list[str]) -> list[str]:
        return [_safe_single_line(value) for value in values]

    @model_validator(mode="after")
    def validate_step_numbers(self) -> Self:
        expected = list(range(1, len(self.steps) + 1))
        if [step.number for step in self.steps] != expected:
            raise ValueError("Plan steps.number 必须从 1 连续递增")
        return self


class PlanModelResponse(_StrictPlanModel):
    kind: Literal["clarify", "final"]
    clarification: PlanClarificationQuestion | None = None
    plan: PlanDocument | None = None

    @model_validator(mode="after")
    def validate_variant(self) -> Self:
        if self.kind == "clarify":
            if self.clarification is None or self.plan is not None:
                raise ValueError("clarify 响应必须且只能包含 clarification")
        elif self.plan is None or self.clarification is not None:
            raise ValueError("final 响应必须且只能包含 plan")
        return self


PlanClarificationCallback: TypeAlias = Callable[[PlanClarificationQuestion], int | None]


def parse_plan_model_response(content: str) -> PlanModelResponse:
    if (
        not isinstance(content, str)
        or not content.strip()
        or len(content) > _MAX_MODEL_RESPONSE_CHARACTERS
    ):
        raise ValueError("Plan 模型响应为空或超过大小限制")
    try:
        return PlanModelResponse.model_validate_json(content)
    except Exception as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        raise ValueError("Plan 模型响应不符合严格 JSON schema") from None


def build_plan_request_prompt(request: str) -> str:
    if not isinstance(request, str) or not request.strip() or len(request) > 64 * 1024:
        raise ValueError("Plan 需求必须是非空且不超过 65536 字符的文本")
    schema = json.dumps(
        PlanModelResponse.model_json_schema(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return (
        "你正在 Plan 模式，只能分析和规划，严禁调用工具、执行命令、修改文件或声称已实施。\n"
        "先根据用户目标生成方案。仅当缺少会实质改变方案的关键信息时，返回 kind=clarify，"
        "一次只问一个最关键问题，并提供恰好 3 个相互排斥的 options；每个 option 必须包含"
        "简短 label、影响说明 description，可将至多一个选项标为 recommended。"
        "不得把选项写成命令、工具调用或要求用户粘贴密钥。信息足够时返回 kind=final 的完整规划书；"
        "final.plan.open_questions 必须是空数组。若仍有任何会影响方案的未解决问题，必须返回 clarify，"
        "不能把问题藏在 final、假设或 Markdown 中。\n"
        "必须只输出一个符合下方 JSON Schema 的 JSON object，不要 Markdown 围栏或额外文字。\n"
        f"JSON Schema: {schema}\n\n用户目标/需求：\n{request.strip()}"
    )


def build_plan_protocol_correction() -> str:
    """要求供应商在下一次独立模型请求中修正结构化 Plan 响应。"""
    return (
        "上一条响应不符合 Plan JSON 协议。请重新返回一个且仅一个 JSON object。"
        "若信息不足，返回 kind=clarify，一次只问一个问题并给出恰好 3 个互斥选项；"
        "若信息足够，返回 kind=final，且 plan.open_questions 必须为 []。"
        "不要输出 Markdown、解释文字、工具调用或未解决问题清单。"
    )


def build_clarification_answer(
    question: PlanClarificationQuestion, selected_index: int
) -> str:
    if type(selected_index) is not int or not 0 <= selected_index < 3:
        raise ValueError("澄清选择必须是 0..2 的整数")
    selected = question.options[selected_index]
    payload = {
        "type": "plan_clarification_answer",
        "question_id": question.id,
        "question": question.question,
        "selected_option": {
            "index": selected_index + 1,
            "label": selected.label,
            "description": selected.description,
        },
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def build_persisted_plan_request(
    request: str,
    answers: list[tuple[PlanClarificationQuestion, int]],
) -> str:
    if not answers:
        return request.strip()
    lines = [request.strip(), "", "Plan 澄清选择："]
    for question, selected_index in answers:
        selected = question.options[selected_index]
        lines.append(
            f"- {question.question} -> {selected.label}（{selected.description}）"
        )
    return "\n".join(lines)


def render_plan_document_chunks(plan: PlanDocument) -> tuple[str, ...]:
    """把已验证 Plan 渲染成可立即依次显示、拼接后完全等价的分块。"""

    def bullets(values: list[str], empty: str = "- 无") -> str:
        return "\n".join(f"- {value}" for value in values) if values else empty

    sections = [
        f"# {plan.title}",
        f"## 目标\n{plan.objective}",
        f"## 范围内\n{bullets(plan.in_scope)}",
        f"## 范围外\n{bullets(plan.out_of_scope)}",
        f"## 前提与假设\n{bullets(plan.assumptions)}",
    ]
    step_lines: list[str] = []
    for step in plan.steps:
        step_lines.append(f"{step.number}. {step.title}")
        step_lines.extend(f"   - {action}" for action in step.actions)
        step_lines.append(f"   - 验证：{step.verification}")
    sections.append("## 实施步骤\n" + "\n".join(step_lines))
    sections.append("## 验收标准\n" + bullets(plan.acceptance_criteria))
    risk_lines = [f"- {item.risk}；缓解：{item.mitigation}" for item in plan.risks]
    sections.append("## 风险与缓解\n" + ("\n".join(risk_lines) if risk_lines else "- 无"))
    return tuple(
        section if index == 0 else "\n\n" + section
        for index, section in enumerate(sections)
    )


def render_plan_document(plan: PlanDocument) -> str:
    return "".join(render_plan_document_chunks(plan))
