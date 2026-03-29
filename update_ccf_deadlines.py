#!/usr/bin/env python3

import json
import re
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import requests


ROOT_DIR = Path(__file__).resolve().parent
OUTPUT_FILE = ROOT_DIR / "ccf_ai_deadlines.json"
LOCAL_TZ = ZoneInfo("Asia/Shanghai")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; VPSChatRelay/1.0; +https://example.local)"
}
MAIN_SOURCE_URL = "https://llv22.github.io/ai-deadlines/"
CCF_SOURCE_URL = "https://www.ccf.org.cn/Academic_Evaluation/AI/"
ARR_DATES_URL = "https://aclrollingreview.org/dates"
ACL_CFP_URL = "https://2026.aclweb.org/calls/main_conference_papers/"
EMNLP_CFP_URL = "https://2026.emnlp.org/calls/main_conference_papers/"
NAACL_REFERENCE_CFP_URL = "https://2025.naacl.org/calls/papers/"
SESSION = requests.Session()

TRACKED_CONFERENCES = [
    {
        "short_name": "AAAI",
        "full_name": "AAAI Conference on Artificial Intelligence",
        "pattern": r"^AAAI\s+\d{4}$",
        "tier": "CCF A / 人工智能",
    },
    {
        "short_name": "NeurIPS",
        "full_name": "Conference on Neural Information Processing Systems",
        "pattern": r"^NeurIPS\s+\d{4}$",
        "tier": "CCF A / 人工智能",
    },
    {
        "short_name": "ACL",
        "full_name": "Annual Meeting of the Association for Computational Linguistics",
        "pattern": r"^ACL\s+\d{4}$",
        "tier": "CCF A / 人工智能",
    },
    {
        "short_name": "CVPR",
        "full_name": "IEEE/CVF Computer Vision and Pattern Recognition Conference",
        "pattern": r"^CVPR\s+\d{4}$",
        "tier": "CCF A / 人工智能",
    },
    {
        "short_name": "ICCV",
        "full_name": "International Conference on Computer Vision",
        "pattern": r"^ICCV\s+\d{4}$",
        "tier": "CCF A / 人工智能",
    },
    {
        "short_name": "ICML",
        "full_name": "International Conference on Machine Learning",
        "pattern": r"^ICML\s+\d{4}$",
        "tier": "CCF A / 人工智能",
    },
    {
        "short_name": "IJCAI",
        "full_name": "International Joint Conference on Artificial Intelligence",
        "pattern": r"^IJCAI(?:-ECAI)?\s+\d{4}$",
        "tier": "CCF A / 人工智能",
    },
    {
        "short_name": "ICLR",
        "full_name": "International Conference on Learning Representations",
        "pattern": r"^ICLR\s+\d{4}$",
        "tier": "常用补充 / ML",
    },
    {
        "short_name": "EMNLP",
        "full_name": "Conference on Empirical Methods in Natural Language Processing",
        "pattern": r"^EMNLP\s+\d{4}$",
        "tier": "常用补充 / NLP",
    },
    {
        "short_name": "NAACL",
        "full_name": "Annual Conference of the North American Chapter of the Association for Computational Linguistics",
        "pattern": r"^NAACL\s+\d{4}$",
        "tier": "常用补充 / NLP",
    },
]

ARR_POLICY_SOURCES = {
    "ACL": {
        "url": ACL_CFP_URL,
        "mode": "current",
    },
    "EMNLP": {
        "url": EMNLP_CFP_URL,
        "mode": "current",
    },
    "NAACL": {
        "url": NAACL_REFERENCE_CFP_URL,
        "mode": "reference",
    },
}


def fetch_text(url: str) -> str:
    response = SESSION.get(url, headers=HEADERS, timeout=12)
    response.raise_for_status()
    return response.text


def strip_tags(value: str) -> str:
    text = re.sub(r"<[^>]+>", "", value)
    text = unescape(text)
    text = text.replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def clean_note_text(value: str) -> str:
    text = strip_tags(value)
    text = re.sub(r"\s*More info here\.?$", "", text, flags=re.I)
    text = re.sub(r"\s*Check here for updates\.?$", "Check official site for updates", text, flags=re.I)
    return text.strip()


def extract_first(pattern: str, text: str, default: str = "") -> str:
    match = re.search(pattern, text, re.S)
    return match.group(1).strip() if match else default


def parse_timezone(value: str):
    value = (value or "").strip()
    if not value:
        return None
    if value in {"UTC", "GMT"}:
        return timezone.utc

    fixed = re.match(r"^(?:UTC|GMT)([+-])(\d{1,2})(?::?(\d{2}))?$", value)
    if fixed:
        sign = 1 if fixed.group(1) == "+" else -1
        hours = int(fixed.group(2))
        minutes = int(fixed.group(3) or "0")
        return timezone(sign * timedelta(hours=hours, minutes=minutes))

    try:
        return ZoneInfo(value)
    except Exception:
        return None


def normalize_deadline(deadline_raw: str, timezone_name: str) -> dict:
    deadline_raw = (deadline_raw or "").strip()
    timezone_name = (timezone_name or "").strip()

    result = {
        "deadline_raw": deadline_raw,
        "deadline_timezone": timezone_name,
        "deadline_local": "",
        "deadline_iso": "",
        "status": "unknown",
        "days_until": None,
    }

    if not deadline_raw:
        result["status"] = "missing"
        return result

    if "TBA" in deadline_raw.upper():
        result["status"] = "tba"
        return result

    try:
        deadline_dt = datetime.strptime(deadline_raw, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        try:
            deadline_dt = datetime.strptime(deadline_raw, "%Y-%m-%d %H:%M")
        except ValueError:
            result["status"] = "unparsed"
            return result

    tzinfo = parse_timezone(timezone_name)
    if tzinfo is None:
        result["status"] = "unparsed"
        return result

    deadline_dt = deadline_dt.replace(tzinfo=tzinfo)
    local_deadline = deadline_dt.astimezone(LOCAL_TZ)
    now = datetime.now(LOCAL_TZ)
    delta = local_deadline - now

    result["deadline_local"] = local_deadline.strftime("%Y-%m-%d %H:%M:%S %Z")
    result["deadline_iso"] = local_deadline.isoformat()
    result["days_until"] = int(delta.total_seconds() // 86400)
    result["status"] = "upcoming" if delta.total_seconds() >= 0 else "passed"
    return result


def parse_main_cards(html: str) -> list[dict]:
    cards = []
    for match in re.finditer(r'<div id="(?P<id>[^"]+)" class="ConfItem.*?<hr>', html, re.S):
        block = match.group(0)
        title = extract_first(
            r'<span class="conf-title">\s*<a[^>]+href="[^"]+">([^<]+)</a>',
            block,
        )
        detail_path = extract_first(
            r'<span class="conf-title">\s*<a[^>]+href="([^"]+)">',
            block,
        )
        official_url = extract_first(
            r'<a title="Conference Website" href="([^"]+)"',
            block,
        )
        date_text = strip_tags(extract_first(r'<span class="conf-date">(.*?)</span>', block))
        place_text = strip_tags(extract_first(r'<span class="conf-place">(.*?)</span>', block))
        note_text = clean_note_text(extract_first(r'<div class="note">\s*<b>Note:\s*</b>(.*?)</div>', block))

        if not title:
            continue

        cards.append(
            {
                "id": match.group("id"),
                "title": strip_tags(title),
                "detail_url": f"https://llv22.github.io{detail_path}" if detail_path else "",
                "official_url": official_url,
                "conference_date": date_text.rstrip("."),
                "conference_place": place_text.rstrip("."),
                "note": note_text,
            }
        )
    return cards


def select_matching_cards(cards: list[dict], pattern: str) -> list[dict]:
    matched = []
    for card in cards:
        title = card["title"]
        if re.match(pattern, title):
            year_match = re.search(r"(\d{4})$", title)
            year = int(year_match.group(1)) if year_match else 0
            matched.append((year, card["title"], card))
    matched.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [item[2] for item in matched]


def parse_detail_fields(html: str, conf_id: str) -> dict:
    marker = f'if (conf == "{conf_id}")'
    start = html.find(marker)
    if start == -1:
        return {}

    next_block = html.find('\n            if (conf == "', start + len(marker))
    block = html[start: next_block if next_block != -1 else len(html)]

    timezone_name = extract_first(r'var timezone = "([^"]+)"', block)
    deadline_raw = extract_first(r'var confDeadline = moment\.tz\("([^"]+)"', block)
    conf_date = extract_first(r"\$\('#conf-date'\)\.text\(\"([^\"]+)\"\)", block)
    conf_place = extract_first(r"\$\('#conf-place'\)\.text\(\"([^\"]+)\"\)", block)
    conf_website = extract_first(r"\$\('#conf-website'\)\.text\(\"([^\"]+)\"\)", block)

    return {
        "detail_date": conf_date,
        "detail_place": conf_place,
        "detail_official_url": conf_website,
        "deadline_raw": deadline_raw,
        "deadline_timezone": timezone_name,
    }


def parse_arr_cycle_date(cycle_label: str, date_label: str) -> tuple[str, str]:
    cycle_match = re.search(r"(\d{4})", cycle_label)
    year = int(cycle_match.group(1)) if cycle_match else datetime.now(LOCAL_TZ).year
    cleaned = date_label.strip()
    if cleaned.upper() == "TBA":
        return "", "tba"
    try:
        parsed = datetime.strptime(f"{cleaned} {year}", "%B %d %Y").replace(
            tzinfo=timezone(timedelta(hours=-12))
        )
    except ValueError:
        return "", "unknown"

    now = datetime.now(LOCAL_TZ)
    local_dt = parsed.astimezone(LOCAL_TZ)
    return local_dt.strftime("%Y-%m-%d %H:%M:%S %Z"), "upcoming" if local_dt >= now else "passed"


def build_arr_policy_guidance(arr_cycles: dict) -> dict[str, dict]:
    guidance = {}

    accepted_acl = arr_cycles.get("accepted_cycles_for_acl", [])
    if accepted_acl:
        guidance["ACL"] = {
            "kind": "official",
            "summary": f"官方可投 ARR 轮次：{'、'.join(accepted_acl)}。",
            "source_url": ACL_CFP_URL,
        }

    try:
        emnlp_html = fetch_text(EMNLP_CFP_URL)
        emnlp_text = extract_first(
            r"Papers must be submitted, at latest, by the .*?(ARR\s+2026\s+May\s+cycle)",
            emnlp_html,
        )
        emnlp_commit = extract_first(
            r"Papers that have received reviews and a meta-review from ARR \(.*?(ARR\s+2026\s+May\s+cycle or an earlier ARR cycle).*?\) may be committed to EMNLP",
            emnlp_html,
        )
        parts = []
        if emnlp_text:
            parts.append(f"官方最晚提交 ARR 轮次：{strip_tags(emnlp_text)}")
        if emnlp_commit:
            parts.append(f"官方允许 commit 到 EMNLP 的范围：{strip_tags(emnlp_commit)}")
        guidance["EMNLP"] = {
            "kind": "official",
            "summary": "；".join(parts) + ("。" if parts else ""),
            "source_url": EMNLP_CFP_URL,
        }
    except Exception:
        pass

    try:
        naacl_html = fetch_text(NAACL_REFERENCE_CFP_URL)
        naacl_text = extract_first(
            r"Papers may be submitted to the .*?(ARR\s+2024\s+October\s+cycle)",
            naacl_html,
        )
        naacl_commit = extract_first(
            r"Papers that have received reviews and a meta-review from ARR \(.*?(ARR\s+2024\s+October\s+cycle or an earlier ARR cycle).*?\) may be committed to NAACL",
            naacl_html,
        )
        parts = ["NAACL 2027 官方 CFP 暂未发布，以下为上一届 NAACL 2025 官方规则参考"]
        if naacl_text:
            parts.append(f"上一届最晚提交 ARR 轮次：{strip_tags(naacl_text)}")
        if naacl_commit:
            parts.append(f"上一届允许 commit 的范围：{strip_tags(naacl_commit)}")
        guidance["NAACL"] = {
            "kind": "reference",
            "summary": "；".join(parts) + "。",
            "source_url": NAACL_REFERENCE_CFP_URL,
        }
    except Exception:
        pass

    return guidance


def build_acl_arr_cycles() -> dict:
    html = fetch_text(ARR_DATES_URL)
    table_match = re.search(r"<table>\s*<thead>.*?</thead>\s*<tbody>(.*?)</tbody>\s*</table>", html, re.S)
    if not table_match:
        raise RuntimeError("未找到 ARR 时间表")

    rows = []
    for row_html in re.findall(r"<tr>(.*?)</tr>", table_match.group(1), re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row_html, re.S)
        if len(cells) != 7:
            continue
        cycle = strip_tags(cells[0])
        submission = strip_tags(cells[1])
        reviewer_registration = strip_tags(cells[2])
        reviews_due = strip_tags(cells[3])
        author_response = strip_tags(cells[4])
        meta_reviews = strip_tags(cells[5])
        cycle_end = strip_tags(cells[6])
        submission_local, status = parse_arr_cycle_date(cycle, submission)

        rows.append(
            {
                "cycle": cycle,
                "submission": submission,
                "submission_local": submission_local,
                "reviewer_registration": reviewer_registration,
                "reviews_due": reviews_due,
                "author_response": author_response,
                "meta_reviews_release": meta_reviews,
                "cycle_end": cycle_end,
                "status": status,
                "source_url": ARR_DATES_URL,
            }
        )

    acl_html = fetch_text(ACL_CFP_URL)
    accepted_cycles = []
    cycle_line = extract_first(
        r"Papers may be submitted to (.*?)\.",
        acl_html,
    )
    if cycle_line:
        accepted_cycles = re.findall(r"ARR\s+\d{4}\s+[A-Za-z]+(?:\s+cycle)?", cycle_line)

    return {
        "title": "ACL / ARR 轮次",
        "source_url": ARR_DATES_URL,
        "acl_call_url": ACL_CFP_URL,
        "accepted_cycles_for_acl": accepted_cycles,
        "cycles": rows,
    }


def build_payload() -> dict:
    main_html = fetch_text(MAIN_SOURCE_URL)
    cards = parse_main_cards(main_html)
    items = []
    errors = []
    arr_cycles = {}
    arr_policy_guidance = {}

    try:
        arr_cycles = build_acl_arr_cycles()
    except Exception as exc:
        errors.append(f'ACL ARR 轮次抓取失败: {exc}')

    try:
        arr_policy_guidance = build_arr_policy_guidance(arr_cycles)
    except Exception as exc:
        errors.append(f'ARR 会议说明抓取失败: {exc}')

    for conf in TRACKED_CONFERENCES:
        matched_cards = select_matching_cards(cards, conf["pattern"])
        if not matched_cards:
            errors.append(f'{conf["short_name"]}: 未在抓取源中找到匹配条目')
            continue

        enriched_cards = []
        for card in matched_cards[:2]:
            detail_fields = {}
            if card["detail_url"]:
                try:
                    detail_html = fetch_text(card["detail_url"])
                    detail_fields = parse_detail_fields(detail_html, card["id"])
                except Exception as exc:
                    errors.append(f'{conf["short_name"]}: 读取详情页失败 - {exc}')

            deadline_info = normalize_deadline(
                detail_fields.get("deadline_raw", ""),
                detail_fields.get("deadline_timezone", ""),
            )
            cycle_match = re.search(r"(\d{4})$", card["title"])

            enriched_cards.append(
                {
                    "short_name": conf["short_name"],
                    "ccf_full_name": conf["full_name"],
                    "ccf_level": conf["tier"],
                    "cycle_year": int(cycle_match.group(1)) if cycle_match else None,
                    "source_title": card["title"],
                    "conference_date": detail_fields.get("detail_date") or card["conference_date"],
                    "conference_place": detail_fields.get("detail_place") or card["conference_place"],
                    "official_url": detail_fields.get("detail_official_url") or card["official_url"],
                    "detail_url": card["detail_url"],
                    "note": card["note"],
                    "ccf_source_url": CCF_SOURCE_URL,
                    **deadline_info,
                }
            )

        latest_card = enriched_cards[0]
        previous_concrete = None
        for candidate in enriched_cards[1:]:
            if candidate["status"] in {"upcoming", "passed"}:
                previous_concrete = candidate
                break

        item = dict(latest_card)
        if previous_concrete:
            item["previous_concrete_cycle_title"] = previous_concrete["source_title"]
            item["previous_concrete_deadline_local"] = previous_concrete["deadline_local"]
            item["previous_concrete_deadline_raw"] = previous_concrete["deadline_raw"]
            item["previous_concrete_status"] = previous_concrete["status"]
        else:
            item["previous_concrete_cycle_title"] = ""
            item["previous_concrete_deadline_local"] = ""
            item["previous_concrete_deadline_raw"] = ""
            item["previous_concrete_status"] = ""

        arr_guidance = arr_policy_guidance.get(conf["short_name"])
        if arr_guidance:
            item["arr_policy_kind"] = arr_guidance["kind"]
            item["arr_policy_summary"] = arr_guidance["summary"]
            item["arr_policy_source_url"] = arr_guidance["source_url"]
        else:
            item["arr_policy_kind"] = ""
            item["arr_policy_summary"] = ""
            item["arr_policy_source_url"] = ""

        items.append(item)

    items.sort(key=lambda item: item["short_name"])

    payload = {
        "meta": {
            "title": "AI 顶会论文 DDL（CCF A + 常用补充）",
            "updated_at": datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S %Z"),
            "server_timezone": "Asia/Shanghai",
            "conference_count": len(items),
            "sources": [
                {
                    "name": "CCF 人工智能 A 类会议名单",
                    "url": CCF_SOURCE_URL,
                },
                {
                    "name": "AI Deadlines 抓取源",
                    "url": MAIN_SOURCE_URL,
                },
                {
                    "name": "ACL ARR 官方时间表",
                    "url": ARR_DATES_URL,
                },
            ],
            "errors": errors,
        },
        "conferences": items,
        "acl_arr_cycles": arr_cycles,
    }

    if not items:
        raise RuntimeError("未抓取到任何会议 ddl 数据")

    return payload


def main():
    payload = build_payload()
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"updated {OUTPUT_FILE}")
    print(f"conference_count={payload['meta']['conference_count']}")
    if payload["meta"]["errors"]:
        print("errors:")
        for err in payload["meta"]["errors"]:
            print(f"  - {err}")


if __name__ == "__main__":
    main()
