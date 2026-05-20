"""
BOS자비스 Phase 1a — 주간 자가 진단 봇

매주 일요일 19:30 KST 자동 실행:
- 노션 두 DB 데이터 분석
- 슬랙 채널 history 수집
- BOS 12주 프레임 기반 휴리스틱 진단
- BOS자비스 채널에 진단 리포트 발송

W4 (책임조직도): 담당자 분포 분석
W11 (AI 통합): 봇 작동 상태
W12 (영업이익률 신호): 진척 + 지연 패턴
W6 (프로세스): 업무 단계별 균형
"""

import os
import sys
import json
from datetime import datetime, timedelta
from collections import Counter
from zoneinfo import ZoneInfo

import requests
from slack_sdk import WebClient as SlackClient
from slack_sdk.errors import SlackApiError

KST = ZoneInfo("Asia/Seoul")
NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2025-09-03"


def get_env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"환경 변수 {name} 누락")
    return v


# --------------------------------------------------------------------------- #
# Notion fetch
# --------------------------------------------------------------------------- #
def query_data_source(token: str, ds_id: str) -> list[dict]:
    url = f"{NOTION_API}/data_sources/{ds_id}/query"
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }
    pages = []
    cursor = None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        r = requests.post(url, headers=headers, json=body, timeout=30)
        if r.status_code >= 400:
            raise RuntimeError(f"Notion API {r.status_code}: {r.text}")
        data = r.json()
        pages.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return pages


def parse_page(page: dict, date_prop: str, title_prop: str) -> dict | None:
    props = page.get("properties", {})

    title_field = props.get(title_prop, {})
    title_parts = title_field.get("title", []) or title_field.get("rich_text", [])
    title = "".join(p.get("plain_text", "") for p in title_parts).strip() or "(제목 없음)"

    date_field = props.get(date_prop, {}).get("date")
    if not date_field:
        return None
    start = date_field.get("start")
    end = date_field.get("end") or start
    if not start:
        return None

    status_value = ""
    for key in ("상태", "Status"):
        field = props.get(key, {})
        if field.get("type") == "select" and field.get("select"):
            status_value = field["select"]["name"]
            break

    item_value = ""
    field = props.get("아이템", {})
    if field.get("type") == "select" and field.get("select"):
        item_value = field["select"]["name"]

    owners = []
    field = props.get("담당자", {})
    if field.get("type") == "people":
        owners = [p.get("name", "") for p in field.get("people", [])]
    elif field.get("type") == "select" and field.get("select"):
        owners = [field["select"]["name"]]

    priority = ""
    field = props.get("우선순위", {})
    if field.get("type") == "select" and field.get("select"):
        priority = field["select"]["name"]

    return {
        "id": page["id"],
        "title": title,
        "start": start,
        "end": end,
        "status": status_value,
        "item": item_value,
        "owners": owners,
        "priority": priority,
        "last_edited": page.get("last_edited_time", ""),
    }


# --------------------------------------------------------------------------- #
# BOS 진단 (휴리스틱)
# --------------------------------------------------------------------------- #
def diagnose_w4_responsibility(milestone_rows, commerce_rows) -> dict:
    """W4 책임조직도 — 담당자 분포 + 부하 분석"""
    all_owners = []
    for r in milestone_rows + commerce_rows:
        all_owners.extend(r.get("owners", []))

    distribution = Counter(all_owners)
    total = sum(distribution.values())

    if total == 0:
        return {
            "status": "🟡",
            "message": "담당자 배정 없음. R&R 명확화 필요",
            "data": {},
        }

    # 50%+ 한 명에 몰리면 병목 시그널
    max_share = (max(distribution.values()) / total) if total else 0
    if max_share > 0.5:
        bottleneck = distribution.most_common(1)[0]
        return {
            "status": "🔴",
            "message": f"병목 — {bottleneck[0]}님에게 {bottleneck[1]}건 ({int(max_share*100)}%) 집중",
            "data": dict(distribution),
        }
    elif max_share > 0.4:
        return {
            "status": "🟡",
            "message": f"R&R 편중 주의 (한 명 {int(max_share*100)}%)",
            "data": dict(distribution),
        }
    else:
        return {
            "status": "🟢",
            "message": f"R&R 분산 양호 ({len(distribution)}명 담당)",
            "data": dict(distribution),
        }


def diagnose_w6_process(milestone_rows, commerce_rows) -> dict:
    """W6 프로세스 — Status 단계별 균형"""
    status_counts = Counter()
    for r in milestone_rows + commerce_rows:
        status = r.get("status", "")
        if status:
            status_counts[status] += 1

    in_progress = sum(c for s, c in status_counts.items() if "Progress" in s or "진행중" in s)
    backlog = sum(c for s, c in status_counts.items() if "Backlog" in s)
    this_week = sum(c for s, c in status_counts.items() if "Week" in s)
    waiting = sum(c for s, c in status_counts.items() if "Waiting" in s)

    total = sum(status_counts.values())
    if total == 0:
        return {"status": "🟡", "message": "진행 데이터 없음", "data": {}}

    if waiting > total * 0.3:
        return {
            "status": "🔴",
            "message": f"외부 대기 비율 높음 ({waiting}/{total}). 의존성 점검 필요",
            "data": dict(status_counts),
        }
    if backlog > in_progress * 3:
        return {
            "status": "🟡",
            "message": f"백로그 적체 ({backlog}건 vs 진행 {in_progress}건). 우선순위 재조정",
            "data": dict(status_counts),
        }
    return {
        "status": "🟢",
        "message": f"진행 흐름 양호 (진행 {in_progress}/이번주 {this_week}/대기 {waiting}/백로그 {backlog})",
        "data": dict(status_counts),
    }


def diagnose_delays(milestone_rows, commerce_rows) -> dict:
    """지연 항목 식별"""
    today = datetime.now(KST).date()
    delayed = []
    for r in milestone_rows + commerce_rows:
        try:
            end_d = datetime.fromisoformat(r["end"][:10]).date()
        except Exception:
            continue
        if end_d < today and r.get("status") not in ("Done",):
            delayed.append({
                "title": r["title"],
                "days_late": (today - end_d).days,
                "item": r.get("item", ""),
                "owners": r.get("owners", []),
            })

    if not delayed:
        return {"status": "🟢", "message": "지연 항목 없음", "data": []}

    delayed_sorted = sorted(delayed, key=lambda x: -x["days_late"])
    return {
        "status": "🔴" if len(delayed) > 3 else "🟡",
        "message": f"지연 {len(delayed)}건 (최대 {delayed_sorted[0]['days_late']}일)",
        "data": delayed_sorted[:5],  # top 5
    }


def diagnose_item_balance(milestone_rows, commerce_rows) -> dict:
    """아이템별 (제품별) 진척 균형"""
    item_counts = Counter()
    for r in milestone_rows + commerce_rows:
        item = r.get("item", "") or "미분류"
        item_counts[item] += 1

    total = sum(item_counts.values())
    if total == 0:
        return {"status": "🟡", "message": "아이템 분류 없음", "data": {}}

    return {
        "status": "🟢",
        "message": f"제품별 분포 — {', '.join(f'{k}:{v}' for k, v in item_counts.most_common())}",
        "data": dict(item_counts),
    }


# --------------------------------------------------------------------------- #
# 리포트 생성
# --------------------------------------------------------------------------- #
def build_diagnostic_report(milestone_rows, commerce_rows) -> str:
    today = datetime.now(KST).date()
    week_num = today.isocalendar()[1]

    w4 = diagnose_w4_responsibility(milestone_rows, commerce_rows)
    w6 = diagnose_w6_process(milestone_rows, commerce_rows)
    delays = diagnose_delays(milestone_rows, commerce_rows)
    items = diagnose_item_balance(milestone_rows, commerce_rows)

    lines = [
        f"🤖 *BOS자비스 주간 자가 진단 — W{week_num} ({today.strftime('%Y-%m-%d')})*",
        "",
        f"📊 *전체 현황*: 신제품 {len(milestone_rows)}건 / 커머스 {len(commerce_rows)}건 추적",
        "",
        "*🔥 BOS 차원별 진단*",
        f"{w4['status']} *W4 책임조직도* — {w4['message']}",
        f"{w6['status']} *W6 프로세스* — {w6['message']}",
        f"{delays['status']} *지연 신호* — {delays['message']}",
        f"{items['status']} *제품 균형* — {items['message']}",
    ]

    if delays["data"]:
        lines.append("")
        lines.append("*⚠️ 지연 항목 (Top 5)*")
        for d in delays["data"]:
            owners = ", ".join(d["owners"]) if d["owners"] else "미배정"
            lines.append(f"  • [{d['item'] or '미분류'}] {d['title']} (지연 {d['days_late']}일, 담당: {owners})")

    if w4["data"]:
        lines.append("")
        lines.append("*👥 담당자별 분포*")
        for name, count in Counter(w4["data"]).most_common():
            lines.append(f"  • {name}: {count}건")

    lines.append("")
    lines.append("---")
    lines.append("_Phase 1a 휴리스틱 진단. Phase 1b (Claude API 기반 진짜 BOS 컨설팅) 다음 단계._")

    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Slack
# --------------------------------------------------------------------------- #
def post_diagnostic(slack: SlackClient, channel: str, report: str):
    slack.chat_postMessage(channel=channel, text=report)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    slack_token = get_env("BOS_SLACK_BOT_TOKEN")
    slack_channel = get_env("BOS_SLACK_CHANNEL_ID")
    notion_token = get_env("NOTION_TOKEN")
    milestone_ds_id = get_env("NOTION_MILESTONE_DS_ID")
    commerce_ds_id = get_env("NOTION_COMMERCE_DS_ID")

    slack = SlackClient(token=slack_token)
    print(f"[{datetime.now(KST).isoformat()}] BOS자비스 주간 진단 시작")

    try:
        milestone_pages = query_data_source(notion_token, milestone_ds_id)
        commerce_pages = query_data_source(notion_token, commerce_ds_id)
    except Exception as e:
        print(f"❌ 노션 fetch 실패: {e}")
        sys.exit(1)

    milestone_rows = [
        r for r in (parse_page(p, "목표일", "이름") for p in milestone_pages) if r
    ]
    commerce_rows = [
        r for r in (parse_page(p, "마감일", "Name") for p in commerce_pages) if r
    ]
    print(f"✅ 신제품 {len(milestone_rows)}건 / 커머스 {len(commerce_rows)}건")

    report = build_diagnostic_report(milestone_rows, commerce_rows)
    print("--- 진단 리포트 ---")
    print(report)
    print("--- 끝 ---")

    try:
        post_diagnostic(slack, slack_channel, report)
        print("✅ BOS자비스 진단 발송 완료")
    except SlackApiError as e:
        print(f"❌ Slack 발송 실패: {e.response['error']}")
        sys.exit(1)


if __name__ == "__main__":
    main()
