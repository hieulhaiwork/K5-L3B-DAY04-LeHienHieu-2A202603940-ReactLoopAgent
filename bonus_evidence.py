"""One source of truth for evidence, completeness checks, and final-answer validation."""
from __future__ import annotations

import re
from collections import defaultdict

from bonus_policy import failed

URL = re.compile(r"https?://[^\s<>\]\)\}\"']+")
HEALTHY = re.compile(r"hoạt\s+động\s+(?:bình\s+thường|ổn\s+định)|all\s+systems\s+operational|fully\s+operational|\bis\s+operational\b", re.I)
UNKNOWN = re.compile(r"không\s+(?:thể\s+)?(?:xác\s+minh|xác\s+nhận|khẳng\s+định)|chưa\s+(?:thể\s+)?xác\s+minh|unknown|unable\s+to\s+verify|cannot\s+confirm|rate_limited", re.I)
ALL_HEALTHY = re.compile(r"all\s+(?:providers|services)\s+(?:are\s+)?(?:fully\s+)?operational|tất\s+cả\s+(?:các\s+)?(?:nhà\s+cung\s+cấp|dịch\s+vụ)\s+(?:đều\s+)?(?:đang\s+)?hoạt\s+động\s+bình\s+thường", re.I)
NEEDS_SCOPE = re.compile(r"phạm\s+vi|ảnh\s+hưởng|tác\s+động|\bscope\b|\bimpact\b|đối\s+chiếu|so\s+sánh", re.I)
MENTIONS_INCIDENT = re.compile(r"\bincidents?\b|sự\s+cố", re.I)
FULL_TIMELINE = re.compile(r"đã\s+xem\s+(?:hết|toàn\s+bộ)|entire\s+timeline|all\s+updates\s+reviewed", re.I)


def provider_for(name, args, data):
    if isinstance(data, dict) and isinstance(data.get("provider"), str):
        return data["provider"].casefold()
    if isinstance(args.get("provider"), str):
        return args["provider"].casefold()
    match = re.fullmatch(r"get_(.+)_status", name)
    return match.group(1).casefold() if match else None


class Evidence:
    def __init__(self):
        self.status = {}                # provider -> successful status snapshot
        self.failures = defaultdict(set)  # provider -> error codes; cleared on success
        self.incident_urls = defaultdict(set)
        self.details = set()            # (provider, incident_id) fetched successfully
        self.incomplete = set()         # (provider, incident_id) with a next page
        self.rate_limited_once = set()  # for possible answer audit

    def record(self, name, args, result):
        provider = provider_for(name, args, result)
        if not provider or not isinstance(result, dict):
            return
        is_status = name == "get_status" or (name.startswith("get_") and name.endswith("_status"))
        if is_status:
            if failed(result):
                code = str(result.get("error") or "UNKNOWN_ERROR")
                self.failures[provider].add(code)
                if code.upper() == "RATE_LIMITED":
                    self.rate_limited_once.add(provider)
            else:
                self.status[provider] = result
                self.failures.pop(provider, None)
        elif name == "get_incident_details" and not failed(result):
            key = (provider, str(result.get("id") or args.get("incident_id") or ""))
            self.details.add(key)
            if result.get("url"):
                self.incident_urls[provider].add(str(result["url"]))
            if result.get("next_offset") is None:
                self.incomplete.discard(key)
            else:
                self.incomplete.add(key)

    @staticmethod
    def requires_scope(question):
        return bool(MENTIONS_INCIDENT.search(question) and NEEDS_SCOPE.search(question))

    def missing_details(self, question):
        """Only enforce details for explicitly requested incident scope comparisons."""
        if not self.requires_scope(question):
            return []
        q = question.casefold()
        missing = []
        for provider, result in self.status.items():
            if provider not in q:
                continue
            incidents = [x for x in result.get("incidents", []) if isinstance(x, dict) and x.get("id")]
            if not incidents:
                continue
            incident = next((x for x in incidents if str(x["id"]) in q), incidents[0])
            key = (provider, str(incident["id"]))
            if key not in self.details:
                missing.append((provider, *key[1:], incident.get("name", "")))
        return missing

    def validate(self, answer, question="", advanced=True):
        errors = []
        lower_answer = answer.casefold()
        # For the original lab contract, one confirmed incident URL per provider is sufficient.
        for provider, urls in self.incident_urls.items():
            if urls and not any(url in answer for url in urls):
                errors.append(f"Thiếu URL incident {provider}: {', '.join(sorted(urls))}")
        if not advanced:
            return errors

        source_requested = any(x in question.casefold() for x in ("nguồn", "dẫn link", "source", "cite", "url"))
        if source_requested:
            for provider, data in self.status.items():
                urls = set(self.incident_urls.get(provider, set()))
                if isinstance(data.get("source"), str):
                    urls.add(data["source"])
                if urls and not any(u in answer for u in urls):
                    errors.append(f"Thiếu nguồn {provider}: {', '.join(sorted(urls))}")

        known_links = {u for links in self.incident_urls.values() for u in links}
        known_links |= {d.get("source") for d in self.status.values() if isinstance(d.get("source"), str)}
        for url in {x.group(0).rstrip(".,;:") for x in URL.finditer(answer)}:
            if ("/incidents/" in url or "stspg.io/" in url) and url not in known_links:
                errors.append(f"URL incident chưa được xác minh: {url}")

        for provider in sorted(self.failures):
            if provider not in lower_answer:
                errors.append(f"Chưa báo trạng thái {provider} chưa xác minh do tool lỗi")
                continue
            for line in answer.splitlines():
                # Check only a local provider claim; don't mistake GitHub's
                # 'All Systems Operational' for a global claim about Vercel.
                if provider in line.casefold() and HEALTHY.search(line) and not UNKNOWN.search(line):
                    errors.append(f"Không có bằng chứng {provider} operational")
                    break

        if self.failures and any(ALL_HEALTHY.search(line) and not UNKNOWN.search(line)
                                 for line in answer.splitlines()):
            errors.append("Không đủ bằng chứng rằng tất cả nhà cung cấp đều operational")
        if self.incomplete and FULL_TIMELINE.search(answer):
            errors.append("Không khẳng định đã xem hết timeline khi còn next_offset")
        return errors

    def summary(self):
        """Conservative fallback for basic status requests, not incident investigations."""
        lines = ["Theo các snapshot đã thu thập (không phải trạng thái live):"]
        for name, data in self.status.items():
            overall = data.get("overall") or {}
            lines.append(f"- {name}: {overall.get('description') or overall.get('indicator') or 'unknown'}; "
                         f"captured_at={data.get('captured_at', 'unknown')}; "
                         f"nguồn={data.get('source', 'không có nguồn')}.")
        for name, problems in sorted(self.failures.items()):
            lines.append(f"- {name}: chưa thể xác minh ({', '.join(sorted(problems))}); "
                         "lỗi tool không chứng minh outage.")
        lines.append("Không đủ căn cứ suy ra nguyên nhân chung giữa các nhà cung cấp.")
        return "\n".join(lines)
