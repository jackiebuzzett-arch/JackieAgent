"""Structured, actionable tool errors with explicit LLM recovery instructions."""

from __future__ import annotations

from typing import Any

from .observability import PIIRedactor


class ActionableToolError(RuntimeError):
    """Structured error carrying explicit recovery steps and suggested follow-up tools for LLM callers."""

    def __init__(
        self,
        error_code: str,
        message: str,
        recovery_instructions: list[str],
        retryable: bool = False,
        suggested_tool: str | None = None,
        valid_parameters: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.user_message = PIIRedactor.redact_text(message)
        self.recovery_instructions = [PIIRedactor.redact_text(step) for step in recovery_instructions]
        self.retryable = retryable
        self.suggested_tool = suggested_tool
        self.valid_parameters = PIIRedactor.redact(valid_parameters or {})

    def to_payload(self) -> dict[str, Any]:
        return {
            "status": "error",
            "error_code": self.error_code,
            "message": self.user_message,
            "retryable": self.retryable,
            "suggested_tool": self.suggested_tool,
            "valid_parameters": self.valid_parameters,
            "recovery_instructions": self.recovery_instructions,
        }


def classify_exception_to_actionable_error(exc: BaseException, tool_name: str = "") -> ActionableToolError:
    """Map arbitrary runtime exceptions into explicit, self-healing LLM recovery instructions."""
    if isinstance(exc, ActionableToolError):
        return exc

    msg = str(exc)
    lower = msg.lower()

    if "outside the 17-week" in lower or "must be within the 17-week" in lower:
        return ActionableToolError(
            error_code="PLAN_DATE_OUT_OF_BOUNDS",
            message=msg,
            retryable=True,
            suggested_tool="coach_get_training_plan",
            valid_parameters={"plan_start_date": "2026-11-09", "plan_end_date": "2027-03-07", "week_num_range": [1, 17]},
            recovery_instructions=[
                "Call `coach_get_training_plan` with `week_num` between 1 and 17 to inspect valid YYYY-MM-DD dates.",
                "Retry your tool call using a date inside `2026-11-09`..`2027-03-07`.",
            ],
        )

    if "race day" in lower and "cannot be moved" in lower:
        return ActionableToolError(
            error_code="IMMUTABLE_RACE_DAY",
            message=msg,
            retryable=False,
            suggested_tool="coach_get_training_plan",
            valid_parameters={"immutable_date": "2027-03-06"},
            recovery_instructions=[
                "Do not attempt to move `2027-03-06` (Publix Atlanta Half Marathon race day).",
                "Instead, swap surrounding shakeout or taper workouts on `2027-03-03`..`2027-03-05`.",
            ],
        )

    if "already undone" in lower or ("adaptation" in lower and "not found" in lower):
        return ActionableToolError(
            error_code="INVALID_ADAPTATION_ID",
            message=msg,
            retryable=True,
            suggested_tool="coach_get_training_plan",
            recovery_instructions=[
                "Call `coach_get_training_plan` and inspect `recent_adaptations` for entries where `reverted_at` is `null`.",
                "Pass a valid active `adaptation_id` to `coach_undo_adaptation`, or omit `adaptation_id` to revert the most recent active change.",
            ],
        )

    if "oauth" in lower or "not authenticated" in lower or "no oura oauth token" in lower:
        return ActionableToolError(
            error_code="OURA_AUTHENTICATION_REQUIRED",
            message=msg,
            retryable=True,
            suggested_tool="coach_morning_briefing",
            recovery_instructions=[
                "Call `coach_morning_briefing` with `sync_live=false` to operate against cached SQLite biometrics immediately.",
                "Instruct the user to run `python3 -m oura_connect auth` or authorize via `http://localhost:8080` to refresh Oura OAuth tokens.",
            ],
        )

    if isinstance(exc, ValueError) and ("isoformat" in lower or "month must be" in lower or "day is out of range" in lower):
        return ActionableToolError(
            error_code="INVALID_DATE_FORMAT",
            message=f"Invalid ISO date supplied to `{tool_name}`: {msg}",
            retryable=True,
            valid_parameters={"expected_date_format": "YYYY-MM-DD", "example": "2026-11-12"},
            recovery_instructions=[
                "Format all `date`, `start_date`, `end_date`, and `swap_with_date` arguments strictly as `YYYY-MM-DD`.",
                f"Retry `{tool_name}` with a valid calendar date.",
            ],
        )

    return ActionableToolError(
        error_code="TOOL_EXECUTION_ERROR",
        message=f"Tool `{tool_name}` encountered an error: {msg}",
        retryable=True,
        suggested_tool="coach_morning_briefing",
        recovery_instructions=[
            f"Verify all argument types passed to `{tool_name}` match its JSON inputSchema.",
            "Call `coach_morning_briefing` with `sync_live=false` to inspect current system state before retrying.",
        ],
    )
