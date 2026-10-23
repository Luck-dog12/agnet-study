"""Code Interpreter 的隔离子进程入口；不得作为 Agent 工具直接注册。"""

from __future__ import annotations

import ast
from dataclasses import dataclass
import json
import math
import os
import statistics
import sys


MAX_AST_NODES = 512
MAX_OUTPUT_CHARACTERS = 16_384
MAX_RESULT_CHARACTERS = 16_384
MIN_OUTPUT_CHARACTERS = 1_024
MIN_MEMORY_MB = 64
MAX_MEMORY_MB = 512
MIN_PROCESSES = 1
MAX_PROCESSES = 2
MIN_TIMEOUT_SECONDS = 1
MAX_TIMEOUT_SECONDS = 10
MAX_REQUEST_BYTES = 16 * 1024
MAX_CODE_BYTES = 8 * 1024
MAX_FILE_BYTES = 64 * 1024
SAFE_CALLS = frozenset(
    {
        "abs", "all", "any", "bool", "dict", "divmod", "enumerate", "float",
        "int", "len", "list", "max", "min", "pow", "print", "range", "round",
        "set", "sorted", "str", "sum", "tuple", "zip",
    }
)
SAFE_CONSTANT_NAMES = frozenset({"True", "False", "None"})
SAFE_MODULE_CALLS = {
    "math": frozenset(
        {
            "acos", "asin", "atan", "atan2", "ceil", "comb", "cos", "degrees",
            "dist", "exp", "fabs", "factorial", "floor", "fmod", "fsum", "gcd",
            "hypot", "isclose", "isfinite", "isinf", "isnan", "lcm", "log",
            "log10", "log2", "perm", "prod", "radians", "remainder", "sin",
            "sqrt", "tan", "trunc",
        }
    ),
    "statistics": frozenset(
        {
            "fmean", "geometric_mean", "harmonic_mean", "mean", "median",
            "median_grouped", "median_high", "median_low", "mode", "multimode",
            "pstdev", "pvariance", "quantiles", "stdev", "variance",
        }
    ),
}
SAFE_MODULE_VALUES = {
    "math": frozenset({"e", "inf", "nan", "pi", "tau"}),
    "statistics": frozenset(),
}

ALLOWED_NODE_TYPES = (
    ast.Module, ast.Expr, ast.Assign, ast.AugAssign, ast.If, ast.For, ast.Break,
    ast.Continue, ast.Pass, ast.Name, ast.Load, ast.Store, ast.Constant, ast.List,
    ast.Tuple, ast.Dict, ast.Set, ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare,
    ast.IfExp, ast.Subscript, ast.Slice, ast.Call, ast.Attribute, ast.keyword,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.BitAnd, ast.BitOr, ast.BitXor, ast.LShift, ast.RShift, ast.MatMult,
    ast.UAdd, ast.USub, ast.Not, ast.Invert, ast.And, ast.Or, ast.Eq, ast.NotEq,
    ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn, ast.Is, ast.IsNot,
)


class RejectedCode(Exception):
    pass


class OutputLimitReached(Exception):
    pass


@dataclass(frozen=True, slots=True)
class RunnerRequest:
    code: str
    max_output_characters: int
    max_memory_mb: int
    max_processes: int
    timeout_seconds: int


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if not isinstance(key, str) or key in result:
            raise ValueError
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise ValueError


def _strict_int(value: object, minimum: int, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise ValueError
    return value


def _parse_request(raw: str) -> RunnerRequest:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_REQUEST_BYTES:
        raise ValueError
    payload = json.loads(
        raw,
        object_pairs_hook=_strict_json_object,
        parse_constant=_reject_json_constant,
    )
    required = {
        "code",
        "max_output_characters",
        "max_memory_mb",
        "max_processes",
        "timeout_seconds",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise ValueError
    code = payload["code"]
    if (
        not isinstance(code, str)
        or not code.strip()
        or "\x00" in code
        or len(code.encode("utf-8")) > MAX_CODE_BYTES
    ):
        raise ValueError
    max_output_characters = _strict_int(
        payload["max_output_characters"],
        MIN_OUTPUT_CHARACTERS,
        MAX_OUTPUT_CHARACTERS,
    )
    max_memory_mb = _strict_int(
        payload["max_memory_mb"],
        MIN_MEMORY_MB,
        MAX_MEMORY_MB,
    )
    max_processes = _strict_int(
        payload["max_processes"],
        MIN_PROCESSES,
        MAX_PROCESSES,
    )
    timeout_seconds = _strict_int(
        payload["timeout_seconds"],
        MIN_TIMEOUT_SECONDS,
        MAX_TIMEOUT_SECONDS,
    )
    return RunnerRequest(
        code=code,
        max_output_characters=max_output_characters,
        max_memory_mb=max_memory_mb,
        max_processes=max_processes,
        timeout_seconds=timeout_seconds,
    )


def _tighter_limit(
    resource_module: object,
    resource_kind: int,
    target_soft: int,
    target_hard: int,
) -> None:
    getrlimit = getattr(resource_module, "getrlimit")
    setrlimit = getattr(resource_module, "setrlimit")
    infinity = getattr(resource_module, "RLIM_INFINITY")
    current_soft, current_hard = getrlimit(resource_kind)
    if (
        isinstance(current_soft, bool)
        or not isinstance(current_soft, int)
        or isinstance(current_hard, bool)
        or not isinstance(current_hard, int)
    ):
        raise RuntimeError
    new_soft = target_soft if current_soft == infinity else min(current_soft, target_soft)
    new_hard = target_hard if current_hard == infinity else min(current_hard, target_hard)
    new_soft = min(new_soft, new_hard)
    setrlimit(resource_kind, (new_soft, new_hard))


def _apply_resource_limits(request: RunnerRequest) -> None:
    if os.name == "nt":
        # Windows limits are established by the trusted parent Job Object before
        # the runner receives its payload.  Do not import the POSIX module here.
        return
    if os.name != "posix":
        raise RuntimeError
    import resource

    memory_bytes = request.max_memory_mb * 1024 * 1024
    limits = [
        (resource.RLIMIT_AS, memory_bytes, memory_bytes),
        (
            resource.RLIMIT_CPU,
            request.timeout_seconds,
            request.timeout_seconds + 1,
        ),
    ]
    if hasattr(resource, "RLIMIT_NPROC"):
        limits.append(
            (
                resource.RLIMIT_NPROC,
                request.max_processes,
                request.max_processes,
            )
        )
    limits.append((resource.RLIMIT_FSIZE, MAX_FILE_BYTES, MAX_FILE_BYTES))
    for resource_kind, target_soft, target_hard in limits:
        _tighter_limit(
            resource,
            resource_kind,
            target_soft,
            target_hard,
        )


def _target_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for item in ast.walk(node):
        if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store):
            names.add(item.id)
    return names


class Validator(ast.NodeVisitor):
    def __init__(self, tree: ast.AST) -> None:
        self.assigned_names = _target_names(tree)

    def generic_visit(self, node: ast.AST) -> None:
        if not isinstance(node, ALLOWED_NODE_TYPES):
            raise RejectedCode("语法能力不在白名单内")
        super().generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id.startswith("_") or "__" in node.id:
            raise RejectedCode("名称不在白名单内")
        if isinstance(node.ctx, ast.Store):
            if node.id in SAFE_CALLS or node.id in SAFE_MODULE_CALLS:
                raise RejectedCode("不能覆盖安全能力")
        elif node.id not in (
            self.assigned_names | SAFE_CALLS | SAFE_CONSTANT_NAMES | set(SAFE_MODULE_CALLS)
        ):
            raise RejectedCode("名称不在白名单内")
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if not isinstance(node.value, (type(None), bool, int, float, str)):
            raise RejectedCode("常量类型不在白名单内")
        if isinstance(node.value, str) and len(node.value) > 4096:
            raise RejectedCode("字符串常量过大")
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        if len(node.targets) != 1 or not isinstance(node.targets[0], (ast.Name, ast.Tuple)):
            raise RejectedCode("赋值目标不在白名单内")
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        if not isinstance(node.target, ast.Name):
            raise RejectedCode("增强赋值目标不在白名单内")
        self.generic_visit(node)

    def visit_For(self, node: ast.For) -> None:
        if not isinstance(node.target, (ast.Name, ast.Tuple)) or node.orelse:
            raise RejectedCode("循环形式不在白名单内")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if (
            node.attr.startswith("_")
            or "__" in node.attr
            or not isinstance(node.value, ast.Name)
            or node.value.id not in SAFE_MODULE_CALLS
            or node.attr not in SAFE_MODULE_CALLS[node.value.id] | SAFE_MODULE_VALUES[node.value.id]
        ):
            raise RejectedCode("属性访问不在白名单内")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        allowed = False
        if isinstance(node.func, ast.Name):
            allowed = node.func.id in SAFE_CALLS
        elif isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
            allowed = (
                node.func.value.id in SAFE_MODULE_CALLS
                and node.func.attr in SAFE_MODULE_CALLS[node.func.value.id]
            )
        if not allowed or any(keyword.arg is None for keyword in node.keywords):
            raise RejectedCode("函数调用不在白名单内")
        self.generic_visit(node)


def validate_and_transform(code: str) -> ast.Module:
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError:
        raise RejectedCode("代码语法无效") from None
    if sum(1 for _ in ast.walk(tree)) > MAX_AST_NODES:
        raise RejectedCode("代码结构过于复杂")
    Validator(tree).visit(tree)
    if tree.body and isinstance(tree.body[-1], ast.Expr):
        tree.body[-1] = ast.Assign(
            targets=[ast.Name(id="_sandbox_result", ctx=ast.Store())],
            value=tree.body[-1].value,
        )
        ast.fix_missing_locations(tree)
    return tree


class BoundedOutput:
    def __init__(self, limit: int) -> None:
        self.parts: list[str] = []
        self.length = 0
        self.truncated = False
        self.limit = limit

    def write(self, text: str) -> None:
        remaining = self.limit - self.length
        if remaining <= 0:
            self.truncated = True
            raise OutputLimitReached
        if len(text) > remaining:
            self.parts.append(text[:remaining])
            self.length += remaining
            self.truncated = True
            raise OutputLimitReached
        self.parts.append(text)
        self.length += len(text)

    def value(self) -> str:
        return "".join(self.parts)


def _run(code: str, max_output_characters: int = MAX_OUTPUT_CHARACTERS) -> dict[str, object]:
    if not 1024 <= max_output_characters <= MAX_OUTPUT_CHARACTERS:
        raise ValueError
    output = BoundedOutput(max_output_characters)

    def safe_print(*values: object, sep: str = " ", end: str = "\n") -> None:
        if not isinstance(sep, str) or not isinstance(end, str):
            raise TypeError
        output.write(sep.join(str(value) for value in values) + end)

    safe_builtins = {
        "abs": abs, "all": all, "any": any, "bool": bool, "dict": dict,
        "divmod": divmod, "enumerate": enumerate, "float": float, "int": int,
        "len": len, "list": list, "max": max, "min": min, "pow": pow,
        "print": safe_print, "range": range, "round": round, "set": set,
        "sorted": sorted, "str": str, "sum": sum, "tuple": tuple, "zip": zip,
    }
    namespace = {
        "__builtins__": safe_builtins,
        "math": math,
        "statistics": statistics,
    }
    try:
        tree = validate_and_transform(code)
        compiled = compile(tree, "<code-interpreter>", "exec", dont_inherit=True, optimize=2)
        exec(compiled, namespace, namespace)
        raw_result = repr(namespace.get("_sandbox_result"))
        result_limit = min(MAX_RESULT_CHARACTERS, max_output_characters)
        truncated = output.truncated or len(raw_result) > result_limit
        result = raw_result[:result_limit]
        return {
            "exit_code": 0,
            "stdout": output.value(),
            "result": result,
            "stderr": "",
            "truncated": truncated,
        }
    except RejectedCode:
        return {
            "exit_code": 1, "stdout": "", "result": "",
            "stderr": "代码不符合能力白名单。", "truncated": False,
        }
    except OutputLimitReached:
        return {
            "exit_code": 1, "stdout": output.value(), "result": "",
            "stderr": "代码输出超过安全上限。", "truncated": True,
        }
    except BaseException as exc:
        return {
            "exit_code": 1, "stdout": output.value(), "result": "",
            "stderr": f"代码运行失败：{type(exc).__name__}。", "truncated": output.truncated,
        }


def main() -> int:
    try:
        # Bound allocation before JSON parsing.  ``+ 1`` lets the strict parser
        # distinguish an exactly-sized request from an oversized text stream.
        request = _parse_request(sys.stdin.read(MAX_REQUEST_BYTES + 1))
        _apply_resource_limits(request)
        response = _run(request.code, request.max_output_characters)
    except BaseException:
        response = {
            "exit_code": 1, "stdout": "", "result": "",
            "stderr": "隔离执行请求无效。", "truncated": False,
        }
    sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
