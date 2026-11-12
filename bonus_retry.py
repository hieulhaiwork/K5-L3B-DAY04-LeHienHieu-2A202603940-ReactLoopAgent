
"""Stage 2: bounded retries and type-safe argument repair for status tools.

Only the original model-requested tool call gets a tool-role message.
Internal attempts are all recorded as trace['event'] == 'tool'.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any


TRANSIENT_ERRORS = {
    "RATE_LIMITED",
    "RATE_LIMIT_ERROR",
    "TOO_MANY_REQUESTS",
    "HTTP_429",
    "429",
}

INVALID_ERRORS = {
    "INVALID_ARGUMENTS",
    "ValueError",
    "TypeError",
}


def error_code(result: Any) -> str:
    if not isinstance(result, dict):
        return ""

    err = result.get("error", "")
    if isinstance(err, dict):
        err = err.get("code") or err.get("type") or ""

    return str(err).strip()


def is_failure(result: Any) -> bool:
    if not isinstance(result, dict):
        return False

    return result.get("ok") is False or bool(result.get("error"))


def _retry_after_seconds(result: Any) -> float | None:
    if not isinstance(result, dict):
        return None

    headers = result.get("headers") or {}
    if not isinstance(headers, dict):
        headers = {}

    value = (
        result.get("retry_after")
        or result.get("retry_after_seconds")
        or result.get("Retry-After")
        or headers.get("Retry-After")
        or headers.get("retry-after")
    )

    if value is None:
        return None

    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        try:
            date = parsedate_to_datetime(str(value))

            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)

            return max(
                0.0,
                (date - datetime.now(timezone.utc)).total_seconds(),
            )
        except (TypeError, ValueError, OverflowError):
            return None


def _schema_for(schemas: list, name: str) -> dict:
    for tool_schema in schemas:
        if not isinstance(tool_schema, dict):
            continue

        function = tool_schema.get("function", tool_schema)

        if isinstance(function, dict) and function.get("name") == name:
            return function.get("parameters", {}) or {}

    return {}


def repair_numeric_arguments(
    name: str,
    args: dict,
    schemas: list,
) -> dict | None:
    """Convert strict decimal strings to integer where schema requires it."""
    params = _schema_for(schemas, name)
    properties = (
        params.get("properties", {})
        if isinstance(params, dict)
        else {}
    )

    fixed = dict(args)
    changed = False

    for key, value in args.items():
        spec = (
            properties.get(key, {})
            if isinstance(properties, dict)
            else {}
        )

        expected_int = (
            isinstance(spec, dict)
            and spec.get("type") == "integer"
        )

        expected_int |= (
            name == "get_incident_details"
            and key in ("offset", "limit")
        )

        if expected_int and isinstance(value, str):
            s = value.strip()

            if s.isdecimal():
                fixed[key] = int(s)
                changed = True

    return fixed if changed else None


class RetryManager:
    """
    Trả lỗi về LLM để model quyết định retry.
    Tối đa 3 lần gọi thực tế cho mỗi tool/provider bị 429.
    """

    def __init__(self, max_rate_retries=2, max_wait_seconds=1.0):
        self.max_rate_retries = max(0, max_rate_retries)
        self.max_wait_seconds = max(0.0, max_wait_seconds)

        self.rate_attempts = {}
        self.exhausted_targets = set()
        self.next_allowed_at = {}

    @staticmethod
    def _target(name, args):
        provider = (
            args.get("provider")
            or name.removeprefix("get_").removesuffix("_status")
        )
        return name, str(provider)

    def execute(
        self, tools, name, args, *,
        schemas, step, trace,
        enable_retry=True,
        raw_arguments=None
    ):
        target = self._target(name, args)

        # Không gọi API nếu đã hết retry budget
        if enable_retry and target in self.exhausted_targets:
            trace.append({
                "event": "retry_suppressed",
                "step": step,
                "name": name
            })
            return {
                "ok": False,
                "error": "RATE_LIMITED",
                "retry_exhausted": True,
                "message": "Đã hết retry budget."
            }, args

        # Tôn trọng Retry-After của API thật
        if enable_retry and target in self.next_allowed_at:
            remaining = max(
                0.0,
                self.next_allowed_at[target] - time.monotonic()
            )

            if remaining > self.max_wait_seconds:
                trace.append({
                    "event": "retry_deferred",
                    "step": step,
                    "name": name,
                    "retry_after_seconds": remaining
                })
                return {
                    "ok": False,
                    "error": "RATE_LIMITED",
                    "message": "Retry-After chưa hết.",
                    "retry_after_seconds": remaining
                }, args

            if remaining > 0:
                time.sleep(remaining)

        # Mỗi lần model gọi tool chỉ thực thi một lần
        try:
            result = tools.execute(name, args)
        except Exception as exc:
            result = {
                "ok": False,
                "error": type(exc).__name__,
                "message": str(exc)[:250]
            }

        trace.append({
            "event": "tool",
            "step": step,
            "name": name,
            "arguments": (
                raw_arguments
                if raw_arguments is not None
                else json.dumps(args, ensure_ascii=False)
            ),
            "result": result
        })

        # Ghi nhận rate limit, KHÔNG tự retry trong cùng bước
        if (
            enable_retry
            and error_code(result).upper() in TRANSIENT_ERRORS
        ):
            count = self.rate_attempts.get(target, 0) + 1
            self.rate_attempts[target] = count

            wait = _retry_after_seconds(result)
            if wait is not None and wait > 0:
                self.next_allowed_at[target] = (
                    time.monotonic() + wait
                )

            if count >= self.max_rate_retries + 1:
                self.exhausted_targets.add(target)
                trace.append({
                    "event": "retry_exhausted",
                    "step": step,
                    "name": name,
                    "attempts": count
                })
            else:
                trace.append({
                    "event": "retry_available",
                    "step": step,
                    "name": name,
                    "remaining": (
                        self.max_rate_retries + 1 - count
                    )
                })

        elif not is_failure(result):
            self.next_allowed_at.pop(target, None)
            self.rate_attempts.pop(target, None)

        # Trả nguyên observation để LLM tự quyết định
        return result, args
