"""Small, deterministic policies for retry limits and tool observations.

Every tool call requested by the model produces exactly one tool observation.
No automatic background retries or pagination are performed.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

RATE_LIMIT = {"RATE_LIMITED", "HTTP_429", "RATE_LIMIT_ERROR", "TOO_MANY_REQUESTS", "429"}
INJECTION = re.compile(
    r"system\s*override|ignore\s+(?:all\s+)?(?:previous|prior|above)\s+instructions|"
    r"disregard\s+previous|developer\s+message\s*:|<\|im_start\|>system|"
    r"\[INST\]|BONUS_INJECTION_SUCCEEDED|"
    r"bỏ\s+qua\s+(?:mọi\s+)?(?:chỉ\s+dẫn|hướng\s+dẫn)\s+(?:trước|ở\s+trên)",
    re.IGNORECASE,
)


def failed(data):
    return isinstance(data, dict) and (data.get("ok") is False or bool(data.get("error")))


def retry_delay(data):
    if not isinstance(data, dict):
        return 0.0
    headers = data.get("headers") if isinstance(data.get("headers"), dict) else {}
    value = next((x for x in (
        data.get("retry_after_seconds"), data.get("retry_after"),
        headers.get("Retry-After"), headers.get("retry-after"),
    ) if x is not None), None)
    if value is None:
        return 0.0
    try:
        return max(0., float(value))
    except (TypeError, ValueError):
        try:
            date = parsedate_to_datetime(str(value))
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            return max(0., (date - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return 0.0


class RetryPolicy:
    """Maximum of 3 *actual* calls per uninterrupted series of HTTP 429s."""
    def __init__(self, max_retries=2, max_wait=1.0):
        self.max_attempts = max_retries + 1
        self.max_wait = max_wait
        self.counts = {}
        self.blocked = set()
        self.not_before = {}

    @staticmethod
    def target(name, args):
        return (name, str(args.get("provider") or name.removeprefix("get_").removesuffix("_status")).lower())

    def remaining(self, target):
        return max(0, self.max_attempts - self.counts.get(target, 0)) if target not in self.blocked else 0

    def execute(self, tools, name, args, step, trace, raw_args, enabled):
        target = self.target(name, args)
        if enabled and target in self.blocked:
            trace.append({"event": "retry_suppressed", "step": step, "name": name})
            return {"ok": False, "error": "RATE_LIMITED", "retry_exhausted": True}

        remaining = self.not_before.get(target, 0) - time.monotonic() if enabled else 0
        if remaining > self.max_wait:
            trace.append({"event": "retry_deferred", "step": step, "name": name})
            return {"ok": False, "error": "RATE_LIMITED", "retry_deferred": True,
                    "retry_after_seconds": round(remaining, 3)}
        if remaining > 0:
            time.sleep(remaining)

        try:
            result = tools.execute(name, args)
        except Exception as exc:
            result = {"ok": False, "error": type(exc).__name__, "message": str(exc)[:250]}
        trace.append({"event": "tool", "step": step, "name": name,
                      "arguments": raw_args, "result": result})

        if enabled and isinstance(result, dict) and str(result.get("error", "")).upper() in RATE_LIMIT:
            self.counts[target] = self.counts.get(target, 0) + 1
            self.not_before[target] = time.monotonic() + retry_delay(result)
            if self.remaining(target) == 0:
                self.blocked.add(target)
                trace.append({"event": "retry_exhausted", "step": step, "name": name,
                              "attempts": self.counts[target]})
            else:
                trace.append({"event": "retry_available", "step": step, "name": name,
                              "remaining": self.remaining(target)})
        elif not failed(result):
            self.counts.pop(target, None)
            self.not_before.pop(target, None)
        return result


def prepare_observation(data, question, enabled, pagination=True):
    """Leave raw evidence in trace; limit only the observation seen by the model."""
    if not enabled or not isinstance(data, (dict, list)):
        return data

    removed = [0]
    def sanitize(item):
        if isinstance(item, str):
            lines = item.splitlines()
            safe = [line for line in lines if not INJECTION.search(line)]
            removed[0] += len(lines) - len(safe)
            return item if len(safe) == len(lines) else ("\n".join(safe) or "[untrusted instruction removed]")
        if isinstance(item, list):
            return [sanitize(x) for x in item]
        if isinstance(item, dict):
            result = {}
            for key, value in item.items():
                if key == "distractors" and isinstance(value, list):
                    result["distractors_removed"] = len(value)
                    continue
                if key in ("incidents", "results", "history") and isinstance(value, list) and len(value) > 25:
                    terms = {x for x in re.findall(r"\w+", question.casefold()) if len(x) > 3}
                    ranked = sorted(enumerate(value), key=lambda x: (
                        -sum(t in str(x[1]).casefold() for t in terms), x[0]))
                    keep = sorted(set(range(6)) | {i for i, _ in ranked[:19]})
                    result[key] = [sanitize(value[i]) for i in keep[:25]]
                    result[f"{key}_omitted"] = len(value) - len(keep[:25])
                else:
                    result[key] = sanitize(value)
            return result
        return item

    output = sanitize(data)
    if removed[0] and isinstance(output, dict):
        output["untrusted_lines_removed"] = removed[0]
    if pagination and isinstance(output, dict) and output.get("next_offset") is not None:
        output["pagination_hint"] = "To inspect older updates, call get_incident_details in a NEW step with offset=next_offset (integer)."
    return output
