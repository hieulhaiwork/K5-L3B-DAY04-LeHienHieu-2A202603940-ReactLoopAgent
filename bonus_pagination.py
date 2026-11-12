
"""Stage 3: bounded timeline pagination, with full page-level trace."""
from __future__ import annotations

import json
from typing import Any

from bonus_retry import is_failure


TIMELINE_HINTS = (
    "timeline",
    "update",
    "updates",
    "cập nhật",
    "nhắc",
    "mention",
    "lịch sử",
    "toàn bộ",
    "chi tiết",
    "nguyên nhân",
    "root cause",
    "diễn biến",
    "trang tiếp",
    "phân tích",
    "giải thích",
)


def supports_pagination(schemas: list) -> bool:
    for schema in schemas:
        function = (
            schema.get("function", schema)
            if isinstance(schema, dict)
            else {}
        )

        if (
            isinstance(function, dict)
            and function.get("name") == "get_incident_details"
        ):
            props = (
                (function.get("parameters") or {})
                .get("properties") or {}
            )
            return "offset" in props and "limit" in props

    return False


def should_auto_page(question: str) -> bool:
    lower = question.casefold()
    return any(token in lower for token in TIMELINE_HINTS)


def collect_pages(
    *,
    tools,
    name,
    args,
    first_result,
    question,
    schemas,
    step,
    trace,
    max_pages=12,
    enabled=True
):
    """
    Không tự đọc các trang tiếp theo.

    Chỉ bổ sung thông tin pagination để model
    chủ động gọi get_incident_details ở bước mới.
    """

    if (
        not enabled
        or name != "get_incident_details"
        or not isinstance(first_result, dict)
        or is_failure(first_result)
    ):
        return first_result

    annotated = dict(first_result)
    next_offset = first_result.get("next_offset")

    annotated["pagination"] = {
        "complete": next_offset is None,
        "next_offset": next_offset,
        "instruction": (
            "If more historical updates are required, "
            "call get_incident_details again in the next "
            "assistant turn using offset=next_offset "
            "and integer limit between 1 and 3."
            if next_offset is not None
            else "No further page."
        )
    }

    return annotated
