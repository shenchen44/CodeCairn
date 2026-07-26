from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _tuple_of_str(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list | tuple):
        return tuple(str(item) for item in value)
    return (str(value),)


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class SWEInstance:
    """A SWE-bench-like code-fixing task.

    The class accepts both real SWE-bench records and the small local benchmark
    fixtures used by micro-swe-agent.
    """

    instance_id: str
    problem_statement: str
    repo: str = ""
    base_commit: str = ""
    test_command: str = "pytest -q"
    gold_patch: str = ""
    fail_to_pass: tuple[str, ...] = ()
    pass_to_pass: tuple[str, ...] = ()
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, row: dict[str, Any]) -> "SWEInstance":
        issue = row.get("issue") if isinstance(row.get("issue"), dict) else {}
        title = issue.get("title") or row.get("title") or row.get("name") or row.get("instance_id") or "code-fixing task"
        body = issue.get("body") or row.get("problem_statement") or row.get("body") or ""
        problem_statement = row.get("problem_statement") or "\n\n".join(part for part in [str(title), str(body)] if part)

        instance_id = (
            row.get("instance_id")
            or row.get("name")
            or row.get("id")
            or str(abs(hash((row.get("repo", ""), problem_statement))) % 10**12)
        )

        test_command = (
            row.get("test_command")
            or row.get("test_cmd")
            or row.get("pytest_command")
            or "pytest -q"
        )

        return cls(
            instance_id=str(instance_id),
            repo=str(row.get("repo") or row.get("repository") or row.get("repo_fixture") or ""),
            base_commit=str(row.get("base_commit") or row.get("commit") or ""),
            problem_statement=str(problem_statement),
            test_command=str(test_command),
            gold_patch=str(row.get("patch") or row.get("gold_patch") or row.get("expected_patch") or ""),
            fail_to_pass=_tuple_of_str(row.get("FAIL_TO_PASS") or row.get("fail_to_pass")),
            pass_to_pass=_tuple_of_str(row.get("PASS_TO_PASS") or row.get("pass_to_pass")),
            meta={key: value for key, value in row.items() if key not in {"patch", "gold_patch", "expected_patch"}},
        )


@dataclass(frozen=True, slots=True)
class PatchEvaluation:
    """Execution result for one candidate patch on one SWE-style task."""

    instance_id: str
    patch: str = ""
    patch_apply_ok: bool = True
    tests_pass: bool = False
    fail_to_pass_passed: bool | None = None
    pass_to_pass_passed: bool | None = None
    blocked_path_violation: bool = False
    unsafe_command_violation: bool = False
    files_changed_count: int | None = None
    diff_line_count: int | None = None
    model_call_count: int | None = None
    tool_call_count: int | None = None
    duration_ms: int | None = None
    error: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, row: dict[str, Any]) -> "PatchEvaluation":
        status = str(row.get("status") or "").lower()
        test_exit_code = row.get("test_exit_code")
        tests_pass = bool(row.get("tests_pass")) or status == "success" or test_exit_code == 0
        patch_apply_ok = bool(row.get("patch_apply_ok", True))
        if status in {"patch_apply_failed", "apply_failed"}:
            patch_apply_ok = False

        return cls(
            instance_id=str(row.get("instance_id") or row.get("name") or row.get("id") or "unknown"),
            patch=str(row.get("patch") or row.get("diff") or row.get("patch_text") or ""),
            patch_apply_ok=patch_apply_ok,
            tests_pass=tests_pass,
            fail_to_pass_passed=row.get("fail_to_pass_passed"),
            pass_to_pass_passed=row.get("pass_to_pass_passed"),
            blocked_path_violation=bool(row.get("blocked_path_violation", False)),
            unsafe_command_violation=bool(row.get("unsafe_command_violation", False)),
            files_changed_count=_int_or_none(row.get("files_changed_count")),
            diff_line_count=_int_or_none(row.get("diff_line_count")),
            model_call_count=_int_or_none(row.get("model_call_count")),
            tool_call_count=_int_or_none(row.get("tool_call_count")),
            duration_ms=_int_or_none(row.get("duration_ms")),
            error=str(row.get("error") or row.get("failure_reason") or ""),
            meta=dict(row),
        )
