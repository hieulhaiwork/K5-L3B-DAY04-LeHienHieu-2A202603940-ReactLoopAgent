
"""Stage 4: query-aware context reduction and untrusted-text isolation.

Full unmodified results stay in trace; only tool observations sent to the LLM
are shortened, and only on bonus tools (not main-course StatusTools).
"""
from __future__ import annotations

import copy
import re
from typing import Any


INJECTION = re.compile(
    r"(?i)(system\s*override|ignore\s+(all\s+)?(previous|prior|above)\s+instructions|"
    r"disregard\s+previous|developer\s+message\s*:|"
    r"bỏ\s+qua\s+(mọi\s+)?(chỉ\s+dẫn|hướng\s+dẫn)\s+(trước|ở\s+trên)|"
    r"you\s+are\s+now\s+(an?\s+)?assistant|<\|im_start\|>system|"
    r"\[INST\]|\bSYSTEM\s*:\s*(you|ignore|hãy))"
)


STOPWORDS = set("""
a an and are at can could current does for from have how in is it its list of on or
please recent show status the their them these this those to was were what where when
which who why with without about by do did check compare service services provider
incident incidents outage operational affected issue issues result results related
hãy kiểm tra tình trạng trạng thái dịch vụ nhà cung cấp sự cố nào nào là có không
các những của cho với và một đã đang trên dưới gần đây thông tin nào hãy
so sánh đối chiếu liên quan hiện tại trong tìm xem cập nhật chi tiết nguồn
""".split())


LIST_KEYS = {
    "incidents",
    "recent_incidents",
    "history",
    "results",
}


def _remove_instructions(value: Any) -> tuple[Any, int]:
    if isinstance(value, str):
        lines = value.splitlines()
        clean = [
            line
            for line in lines
            if not INJECTION.search(line)
        ]

        removed = len(lines) - len(clean)

        if removed:
            return (
                "\n".join(clean) or "[untrusted instruction removed]",
                removed,
            )

        if INJECTION.search(value):
            return "[untrusted instruction removed]", 1

        return value, 0

    if isinstance(value, dict):
        output, total = {}, 0

        for key, child in value.items():
            output[key], n = _remove_instructions(child)
            total += n

        return output, total

    if isinstance(value, list):
        output, total = [], 0

        for child in value:
            clean, n = _remove_instructions(child)
            output.append(clean)
            total += n

        return output, total

    return value, 0


def _terms(question: str) -> list[str]:
    parts = re.findall(
        r"[^\W_]+",
        question.casefold(),
        re.UNICODE,
    )

    return [
        word
        for word in set(parts)
        if len(word) > 2 and word not in STOPWORDS
    ]


def _item_text(item: Any) -> str:
    if not isinstance(item, dict):
        return str(item).casefold()[:1500]

    fields = (
    "name", "title", "description", "body",
    "component", "service", "status",
    "summary", "incident_id", "id", "message"
)

    fragments = []

    for key in fields:
        value = item.get(key)
        if isinstance(value, str):
            fragments.append(value)

    for key in ("incident_updates", "updates", "components"):
        value = item.get(key)

        if isinstance(value, list):
            fragments.extend(
                str(x.get("body") or x.get("name") or "")
                for x in value[:20]
                if isinstance(x, dict)
            )

    return " ".join(fragments).casefold()[:4500]


def _reduce_lists(
    obj: Any,
    terms: list[str],
    *,
    max_items: int,
) -> tuple[Any, int]:

    if isinstance(obj, dict):
        new, dropped = {}, 0

        for key, child in obj.items():
            if key == "distractors" and isinstance(child, list):
                # Lọc riêng danh sách dữ liệu nhiễu
                relevant = [
                    item
                    for item in child
                    if any(
                        term in _item_text(item)
                        for term in terms
                    )
                ]

                # Chỉ giữ tối đa 3 bản ghi nếu có liên quan
                selected = relevant[:3]

                new[key] = selected

                new["distractors_total_before_filter"] = len(child)
                new["distractors_filtered_count"] = len(selected)

                dropped += len(child) - len(selected)

            elif (
                key in LIST_KEYS
                and isinstance(child, list)
                and len(child) > max_items
            ):
                scored = []

                for index, item in enumerate(child):
                    text = _item_text(item)
                    score = sum(
                        1 for t in terms
                        if t in text
                    )

                    if isinstance(item, dict):
                        status = str(
                            item.get("status", "")
                        ).casefold()

                        if status in (
                            "investigating",
                            "identified",
                            "monitoring",
                        ):
                            score += 1

                    scored.append((score, index))

                # Preserve a chronological sample and relevant records.
                keep = set(range(min(6, len(child))))

                for score, index in sorted(
                    scored,
                    key=lambda x: (-x[0], x[1]),
                ):
                    if len(keep) >= max_items:
                        break

                    if score > 0:
                        keep.add(index)

                # Fill remaining budget in original order.
                for i in range(len(child)):
                    if len(keep) >= max_items:
                        break
                    keep.add(i)

                new[key] = [
                    child[i]
                    for i in sorted(keep)
                ]

                dropped += len(child) - len(keep)

                new[f"{key}_total_before_filter"] = len(child)
                new[f"{key}_filtered_count"] = len(keep)

            else:
                new[key], n = _reduce_lists(
                    child,
                    terms,
                    max_items=max_items,
                )
                dropped += n

        return new, dropped

    if isinstance(obj, list):
        out, dropped = [], 0

        for value in obj:
            updated, n = _reduce_lists(
                value,
                terms,
                max_items=max_items,
            )
            out.append(updated)
            dropped += n

        return out, dropped

    return obj, 0


def prepare_observation(
    result: Any,
    *,
    question: str,
    enabled: bool,
    max_incidents: int = 25,
) -> Any:

    if not enabled or not isinstance(result, (dict, list)):
        return result

    prepared, removed_instructions = _remove_instructions(
        copy.deepcopy(result)
    )

    prepared, omitted = _reduce_lists(
        prepared,
        _terms(question),
        max_items=max_incidents,
    )

    if (
        isinstance(prepared, dict)
        and (omitted or removed_instructions)
    ):
        prepared["_observation_processing"] = {
            "incidents_omitted_from_model_context": omitted,
            "untrusted_instruction_lines_removed": removed_instructions,
            "full_raw_result_retained_in_trace": True,
            "warning": (
                "Dữ liệu đã được chọn lọc. Không kết luận "
                "danh sách này là toàn bộ incidents."
            ),
        }

    return prepared
