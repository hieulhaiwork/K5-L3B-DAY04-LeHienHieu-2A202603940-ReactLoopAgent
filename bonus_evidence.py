
"""Stage 5: provenance ledger and bounded, non-LLM evidence checks."""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from bonus_retry import is_failure, error_code


URL_PATTERN = re.compile(r"https?://[^\s<>\]\)\}\"']+")

CONFIDENT_HEALTH = re.compile(
    r"(?i)((?:đang\s+)?hoạt\s+động\s+bình\s+thường|hoạt\s+động\s+ổn\s+định|"
    r"không\s+(có|gặp)\s+sự\s+cố|all\s+systems\s+operational|"
    r"\bis\s+operational\b|\bfully\s+operational\b)"
)

UNCERTAINTY = re.compile(
    r"(?i)(không\s+(thể|đủ|có)\s+(xác\s+nhận|kết\s+luận|dữ\s+liệu|bằng\s+chứng)|"
    r"chưa\s+(thể|xác\s+minh|đủ)|không\s+khẳng\s+định|"
    r"không\s+thể\s+biết|unable\s+to\s+verify|cannot\s+confirm|"
    r"not\s+verified|unknown|thiếu\s+dữ\s+liệu)"
)


def provider_of(
    tool_name: str,
    args: dict,
    result: Any,
) -> str | None:

    if isinstance(result, dict) and isinstance(
        result.get("provider"), str
    ):
        return result["provider"].casefold()

    if isinstance(args.get("provider"), str):
        return args["provider"].casefold()

    m = re.fullmatch(r"get_(.+)_status", tool_name)

    if m:
        return m.group(1).casefold()

    return None


class EvidenceLedger:
    def __init__(self):
        self.incident_urls: dict[str, set[str]] = defaultdict(set)
        self.status_urls: dict[str, set[str]] = defaultdict(set)
        self.successful_status: set[str] = set()
        self.failed_status: dict[str, set[str]] = defaultdict(set)
        self.captured_at: dict[str, set[str]] = defaultdict(set)
        self.incomplete_timelines: set[tuple[str, str]] = set()
        self.tool_failures: list[tuple[str, str]] = []
        self.other_observed_urls: set[str] = set()

    def record(
        self,
        tool_name: str,
        args: dict,
        result: Any,
    ) -> None:

        provider = provider_of(tool_name, args, result)

        if not provider or not isinstance(result, dict):
            return

        if result.get("captured_at"):
            self.captured_at[provider].add(
                str(result["captured_at"])
            )

        # Also remember links mentioned in incident update bodies.
        if not is_failure(result):
            for update in result.get("updates") or []:
                if (
                    isinstance(update, dict)
                    and isinstance(update.get("body"), str)
                ):
                    self.other_observed_urls.update(
                        m.group(0).rstrip(".,;:")
                        for m in URL_PATTERN.finditer(update["body"])
                    )

        if tool_name == "get_incident_details":
            if not is_failure(result) and result.get("url"):
                self.incident_urls[provider].add(str(result["url"]))

            if not is_failure(result):
                incident_id = str(
                    result.get("id")
                    or args.get("incident_id")
                    or ""
                )

                key = (provider, incident_id)

                if result.get("next_offset") is not None:
                    self.incomplete_timelines.add(key)
                else:
                    self.incomplete_timelines.discard(key)

            return

        if (
            tool_name == "get_status"
            or (
                tool_name.startswith("get_")
                and tool_name.endswith("_status")
            )
        ):
            if is_failure(result):
                code = error_code(result) or "UNKNOWN_ERROR"

                self.failed_status[provider].add(code)
                self.tool_failures.append((provider, code))

            else:
                self.successful_status.add(provider)
                self.failed_status.pop(provider, None)

                if isinstance(result.get("source"), str):
                    self.status_urls[provider].add(result["source"])

    def missing_providers(self, answer: str) -> list[str]:
        return sorted(
            provider
            for provider, urls in self.incident_urls.items()
            if urls and not any(url in answer for url in urls)
        )

    def _unsupported_url(self, answer: str) -> list[str]:
        known = {
            u
            for values in self.incident_urls.values()
            for u in values
        }

        known.update(
            u
            for values in self.status_urls.values()
            for u in values
        )

        found = {
            match.group(0).rstrip(".,;:")
            for match in URL_PATTERN.finditer(answer)
        }

        incident_links = [
            url
            for url in found
            if (
                "/incidents/" in url
                or "stspg.io/" in url
            )
            and url not in known
        ]

        return sorted(incident_links)

    def unsupported_health_claim(self, answer: str) -> list[str]:
        """Detect unsupported operational claims for providers with failed status checks."""

        # Chỉ xét provider chưa có lần kiểm tra thành công.
        failed = {
            provider.casefold()
            for provider in self.failed_status
            if provider not in self.successful_status
        }

        if not failed:
            return []

        errors = set()

        # 1. Phát hiện kết luận về TẤT CẢ providers.
        global_pattern = re.compile(
            r"(?i)("
            r"all\s+(?:services|providers)\s+"
            r"(?:are\s+)?(?:fully\s+)?operational|"
            r"tất\s+cả\s+(?:các\s+)?"
            r"(?:dịch\s+vụ|nhà\s+cung\s+cấp)\s+"
            r"(?:đều\s+)?(?:đang\s+)?"
            r"hoạt\s+động\s+bình\s+thường"
            r")"
        )

        # 2. Tách nội dung theo provider
        providers = sorted(
            set(self.failed_status) | self.successful_status,
            key=len,
            reverse=True,
        )

        provider_pattern = re.compile(
            r"(?<!\w)(?:"
            + "|".join(re.escape(p) for p in providers)
            + r")(?!\w)",
            re.IGNORECASE,
        )

        for line in answer.splitlines():

            # Kiểm tra khẳng định tất cả providers đều khỏe.
            if (
                global_pattern.search(line)
                and not UNCERTAINTY.search(line)
                and not re.search(
                    r"(?i)không\s+thể\s+khẳng\s+định",
                    line,
                )
            ):
                errors.add("global_status_without_full_evidence")

            # Kiểm tra khẳng định trạng thái của từng provider.
            matches = list(provider_pattern.finditer(line))

            for i, match in enumerate(matches):
                provider = match.group().casefold()

                if provider not in failed:
                    continue

                # Chỉ lấy nội dung thuộc về provider này.
                end = (
                    matches[i + 1].start()
                    if i + 1 < len(matches)
                    else len(line)
                )
                text = line[match.end():end]

                # Kiểm tra khẳng định operational.
                claims_healthy = (
                    CONFIDENT_HEALTH.search(text)
                    or re.search(r"(?i)\b(?:operational|healthy)\b", text)
                )

                expresses_uncertainty = (
                    UNCERTAINTY.search(text)
                    or re.search(
                        r"(?i)(not\s+(?:fully\s+)?operational|"
                        r"không\s+(?:thể\s+)?(?:xác\s+minh|"
                        r"xác\s+nhận|khẳng\s+định|hoạt\s+động))",
                        text,
                    )
                )

                if claims_healthy and not expresses_uncertainty:
                    errors.add(provider)

        return sorted(errors)

    def validate(
        self,
        answer: str,
        *,
        advanced: bool = True,
        question: str = "",
    ) -> list[str]:
        """
        Kiểm tra final answer dựa trên evidence đã thu thập.

        Returns:
            []: Câu trả lời vượt qua các kiểm tra.
            list[str]: Những lỗi cần yêu cầu LLM sửa.
        """
        errors = []
        answer_lower = answer.casefold()
        question_lower = question.casefold()

        # 1. Kiểm tra URL của incident đã điều tra
        # Ít nhất một URL hợp lệ cho mỗi provider có incident details.
        missing = self.missing_providers(answer)

        if missing:
            sources = "; ".join(
                f"{provider}: "
                + ", ".join(sorted(self.incident_urls[provider]))
                for provider in missing
            )
            errors.append(
                f"Thiếu URL incident đã xác minh cho provider: {sources}"
            )

        # Chỉ kiểm tra nguồn cơ bản nếu không bật advanced.
        if not advanced:
            return errors

        # 2. Kiểm tra provider bị lỗi nhưng không được đề cập
        for provider in sorted(self.failed_status):
            if provider in self.successful_status:
                continue

            if provider.casefold() not in answer_lower:
                errors.append(
                    f"Chưa báo trạng thái chưa xác minh được "
                    f"của {provider} do tool lỗi."
                )

        # 3. Phát hiện kết luận operational thiếu bằng chứng
        for provider in self.unsupported_health_claim(answer):
            if provider == "global_status_without_full_evidence":
                errors.append(
                    "Không thể khẳng định tất cả dịch vụ hoạt động "
                    "bình thường khi có provider bị lỗi."
                )
            else:
                errors.append(
                    f"Provider {provider} bị lỗi tool; "
                    "không được khẳng định operational."
                )

        # 4. Kiểm tra URL nguồn
        # 4a. Phát hiện URL incident không được xác minh.
        unverified_links = self._unsupported_url(answer)

        if unverified_links:
            errors.append(
                "URL incident chưa được tools xác nhận: "
                + ", ".join(unverified_links)
            )

        # 4b. Nếu người dùng yêu cầu nguồn, kiểm tra từng provider.
        source_requested = any(
            keyword in question_lower
            for keyword in ("nguồn", "dẫn link", "source", "cite", "url")
        )

        if source_requested:
            for provider, urls in sorted(self.status_urls.items()):
                # Chấp nhận cả URL status hoặc incident hợp lệ
                # thuộc cùng provider.
                allowed_urls = (
                    urls | self.incident_urls.get(provider, set())
                )

                if allowed_urls and not any(
                    url in answer for url in allowed_urls
                ):
                    errors.append(
                        f"Thiếu URL nguồn cho {provider}: "
                        + ", ".join(sorted(allowed_urls))
                    )

        # 5. Kiểm tra tuyên bố đã đọc toàn bộ timeline
        claims_full_timeline = re.search(
            r"(?i)(đã\s+xem\s+(hết|toàn\s+bộ)|"
            r"toàn\s+bộ\s+timeline\s+đã|"
            r"all\s+updates\s+reviewed|"
            r"entire\s+timeline)",
            answer,
        )

        if self.incomplete_timelines and claims_full_timeline:
            errors.append(
                "Timeline chưa đọc hết; "
                "không được khẳng định đã xem mọi update."
            )

        return errors

    def partial_failure_note(self) -> str:
        parts = []

        for provider, errors in sorted(self.failed_status.items()):
            if provider not in self.successful_status:
                parts.append(
                    f"{provider}: chưa xác minh được trạng thái "
                    f"({', '.join(sorted(errors))})"
                )

        for provider, incident_id in sorted(self.incomplete_timelines):
            parts.append(
                f"{provider}/{incident_id}: timeline chưa được đọc hết"
            )

        return "; ".join(parts)
