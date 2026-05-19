"""
기린 슬랙 봇 — 노션 두 DB 매일 슬랙 다이제스트

09:00 KST: 풀 다이제스트 (이미지 2장 + 요약)
12/15/18:00 KST: diff 체크 → 변화 있을 때만 발송

Notion API 2025-09-03 (data_sources endpoint) 사용 — multi-source DB 지원
"""

import os
import sys
import json
import argparse
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib import font_manager, rc

import requests
from slack_sdk import WebClient as SlackClient
from slack_sdk.errors import SlackApiError

KST = ZoneInfo("Asia/Seoul")
STATE_FILE = Path(__file__).parent / "state" / "previous_state.json"
NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2025-09-03"

ITEM_COLORS = {
    "솔잎": "#9C7CB5",
    "하임리히": "#F5A6B6",
    "미백치약": "#B5A88B",
    "솔직한알": "#C9A8E0",
    "공통": "#A0A0A0",
}


def setup_korean_font():
    candidates = ["Malgun Gothic", "AppleGothic", "NanumGothic", "Noto Sans CJK KR", "DejaVu Sans"]
    available = {f.name for f in font_manager.fontManager.ttflist}
    for c in candidates:
        if c in available:
            rc("font", family=c)
            break
    rc("axes", unicode_minus=False)


def get_env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"환경 변수 {name} 누락")
    return v


# --------------------------------------------------------------------------- #
# Notion fetch (data_sources endpoint, supports multi-source DBs)
# --------------------------------------------------------------------------- #
def notion_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def query_data_source(token: str, ds_id: str) -> list[dict]:
    """data_sources.query 엔드포인트로 전체 페이지 fetch (페이지네이션 자동)."""
    url = f"{NOTION_API}/data_sources/{ds_id}/query"
    headers = notion_headers(token)
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
    for key in ("상태", "Status", "진행상태"):
        field = props.get(key, {})
        if field.get("type") == "select" and field.get("select"):
            status_value = field["select"]["name"]
            break
        if field.get("type") == "status" and field.get("status"):
            status_value = field["status"]["name"]
            break

    item_value = ""
    item_field = props.get("아이템", {})
    if item_field.get("type") == "select" and item_field.get("select"):
        item_value = item_field["select"]["name"]

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
        "url": page.get("url", ""),
        "title": title,
        "start": start,
        "end": end,
        "status": status_value,
        "item": item_value,
        "owners": owners,
        "priority": priority,
        "last_edited": page.get("last_edited_time", ""),
    }


def parse_pages(pages: list[dict], date_prop: str, title_prop: str) -> list[dict]:
    rows = []
    for p in pages:
        r = parse_page(p, date_prop=date_prop, title_prop=title_prop)
        if r:
            rows.append(r)
    return rows


# --------------------------------------------------------------------------- #
# Gantt rendering
# --------------------------------------------------------------------------- #
def render_gantt(rows: list[dict], title: str) -> str:
    if not rows:
        fig, ax = plt.subplots(figsize=(10, 3))
        ax.text(0.5, 0.5, "데이터 없음", ha="center", va="center", fontsize=14)
        ax.set_title(title)
        ax.axis("off")
        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        plt.savefig(tmp.name, dpi=130, bbox_inches="tight")
        plt.close(fig)
        return tmp.name

    rows_sorted = sorted(rows, key=lambda r: r["start"])
    n = len(rows_sorted)
    height = max(3.5, 0.42 * n + 1.5)
    fig, ax = plt.subplots(figsize=(13, height))

    today = datetime.now(KST).date()
    y_labels = []
    used_colors = set()

    for i, r in enumerate(rows_sorted):
        start_d = datetime.fromisoformat(r["start"][:10]).date()
        end_d = datetime.fromisoformat(r["end"][:10]).date()
        duration = max((end_d - start_d).days + 1, 1)

        color = ITEM_COLORS.get(r.get("item", ""), "#A0A0A0")
        used_colors.add((r.get("item", "") or "공통", color))

        ax.barh(i, duration, left=start_d, color=color, alpha=0.85, edgecolor="#444", linewidth=0.5)
        y_labels.append(r["title"])

    ax.set_yticks(range(n))
    ax.set_yticklabels(y_labels, fontsize=9)
    ax.invert_yaxis()
    ax.set_title(title, fontsize=14, weight="bold", pad=10)
    ax.grid(axis="x", linestyle="--", alpha=0.4)
    ax.axvline(today, color="red", linestyle="-", linewidth=1.5, alpha=0.7)
    ax.text(today, -0.5, "오늘", color="red", fontsize=10, ha="center")
    fig.autofmt_xdate()

    legend_patches = [mpatches.Patch(color=c, label=k) for k, c in sorted(used_colors)]
    if legend_patches:
        ax.legend(handles=legend_patches, loc="upper right", fontsize=9, framealpha=0.9)

    plt.tight_layout()
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    plt.savefig(tmp.name, dpi=130, bbox_inches="tight")
    plt.close(fig)
    return tmp.name


# --------------------------------------------------------------------------- #
# Summary
# --------------------------------------------------------------------------- #
def build_summary(milestone_rows, commerce_rows) -> str:
    today = datetime.now(KST).date()

    def categorize(rows):
        starting, ending, delayed = [], [], []
        for r in rows:
            try:
                start_d = datetime.fromisoformat(r["start"][:10]).date()
                end_d = datetime.fromisoformat(r["end"][:10]).date()
            except Exception:
                continue
            status = r.get("status", "")
            if status == "Done":
                continue
            if start_d == today:
                starting.append(r)
            if end_d == today:
                ending.append(r)
            if end_d < today:
                delayed.append(r)
        return starting, ending, delayed

    m_start, m_end, m_delay = categorize(milestone_rows)
    c_start, c_end, c_delay = categorize(commerce_rows)

    lines = [f"📊 *기린 데일리 다이제스트 — {today.strftime('%Y-%m-%d (%a)')}*", ""]

    if m_end or c_end:
        lines.append("🎯 *오늘 마감*")
        for r in m_end:
            lines.append(f"  • [신제품] {r['title']}")
        for r in c_end:
            lines.append(f"  • [커머스] {r['title']}")
        lines.append("")

    if m_start or c_start:
        lines.append("🚀 *오늘 시작*")
        for r in m_start:
            lines.append(f"  • [신제품] {r['title']}")
        for r in c_start:
            lines.append(f"  • [커머스] {r['title']}")
        lines.append("")

    if m_delay or c_delay:
        lines.append("⚠️ *지연*")
        for r in m_delay:
            lines.append(f"  • [신제품] {r['title']}")
        for r in c_delay:
            lines.append(f"  • [커머스] {r['title']}")
        lines.append("")

    if not (m_end or c_end or m_start or c_start or m_delay or c_delay):
        lines.append("✅ 오늘 특이사항 없음")
        lines.append("")

    lines.append(f"📈 신제품 {len(milestone_rows)}건 / 커머스 {len(commerce_rows)}건 추적 중")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Diff
# --------------------------------------------------------------------------- #
def make_snapshot(rows: list[dict]) -> dict:
    return {
        r["id"]: {
            "title": r["title"],
            "start": r["start"],
            "end": r["end"],
            "status": r["status"],
            "last_edited": r["last_edited"],
        }
        for r in rows
    }


def compute_diff(previous: dict, current: dict) -> list[str]:
    changes = []
    prev_ids, curr_ids = set(previous.keys()), set(current.keys())

    for new_id in curr_ids - prev_ids:
        c = current[new_id]
        changes.append(f"🆕 신규: {c['title']}")

    for removed_id in prev_ids - curr_ids:
        p = previous[removed_id]
        changes.append(f"🗑️ 삭제: {p['title']}")

    for shared_id in prev_ids & curr_ids:
        p, c = previous[shared_id], current[shared_id]
        if p.get("status") != c.get("status"):
            changes.append(f"🔄 상태: {c['title']} ({p.get('status', '?')} → {c.get('status', '?')})")
        if (p.get("start"), p.get("end")) != (c.get("start"), c.get("end")):
            changes.append(f"📅 일정 변경: {c['title']}")
        if p.get("title") != c.get("title"):
            changes.append(f"✏️ 이름: {p['title']} → {c['title']}")

    return changes


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(milestone_snap: dict, commerce_snap: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps({"milestone": milestone_snap, "commerce": commerce_snap}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# --------------------------------------------------------------------------- #
# Slack
# --------------------------------------------------------------------------- #
def post_full_digest(slack: SlackClient, channel: str, summary: str, m_img: str, c_img: str):
    slack.files_upload_v2(
        channel=channel,
        file=m_img,
        filename="milestone.png",
        title="신제품 마일스톤",
        initial_comment=summary,
    )
    slack.files_upload_v2(
        channel=channel,
        file=c_img,
        filename="commerce.png",
        title="커머스팀 전체 업무",
    )


def post_diff(slack: SlackClient, channel: str, m_changes: list[str], c_changes: list[str]) -> bool:
    if not (m_changes or c_changes):
        return False
    now = datetime.now(KST).strftime("%H:%M")
    lines = [f"📊 *업데이트 — {now}*", ""]
    if m_changes:
        lines.append("*신제품 마일스톤*")
        for c in m_changes:
            lines.append(f"  • {c}")
        lines.append("")
    if c_changes:
        lines.append("*커머스팀 업무*")
        for c in c_changes:
            lines.append(f"  • {c}")
    slack.chat_postMessage(channel=channel, text="\n".join(lines))
    return True


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--force-full", action="store_true", help="diff 무시 풀 다이제스트")
    args = parser.parse_args()

    setup_korean_font()

    slack_token = get_env("SLACK_BOT_TOKEN")
    slack_channel = get_env("SLACK_CHANNEL_ID")
    notion_token = get_env("NOTION_TOKEN")
    milestone_ds_id = get_env("NOTION_MILESTONE_DS_ID")
    commerce_ds_id = get_env("NOTION_COMMERCE_DS_ID")

    slack = SlackClient(token=slack_token)
    print(f"[{datetime.now(KST).isoformat()}] 시작 — force_full={args.force_full}")

    try:
        milestone_pages = query_data_source(notion_token, milestone_ds_id)
        commerce_pages = query_data_source(notion_token, commerce_ds_id)
    except Exception as e:
        print(f"❌ 노션 fetch 실패: {e}")
        sys.exit(1)

    milestone_rows = parse_pages(milestone_pages, date_prop="목표일", title_prop="이름")
    commerce_rows = parse_pages(commerce_pages, date_prop="마감일", title_prop="Name")
    print(f"✅ 신제품 {len(milestone_rows)}건 / 커머스 {len(commerce_rows)}건")

    m_snap = make_snapshot(milestone_rows)
    c_snap = make_snapshot(commerce_rows)

    if args.force_full:
        summary = build_summary(milestone_rows, commerce_rows)
        m_img = render_gantt(milestone_rows, "신제품 마일스톤")
        c_img = render_gantt(commerce_rows, "커머스팀 전체 업무")
        try:
            post_full_digest(slack, slack_channel, summary, m_img, c_img)
            print("✅ 풀 다이제스트 발송")
        except SlackApiError as e:
            print(f"❌ Slack 발송 실패: {e.response['error']}")
            sys.exit(1)
        finally:
            for p in (m_img, c_img):
                try:
                    os.unlink(p)
                except OSError:
                    pass
        save_state(m_snap, c_snap)
    else:
        prev = load_state()
        m_changes = compute_diff(prev.get("milestone", {}), m_snap)
        c_changes = compute_diff(prev.get("commerce", {}), c_snap)
        if post_diff(slack, slack_channel, m_changes, c_changes):
            print(f"✅ Diff 알림 — 신제품 {len(m_changes)}건 / 커머스 {len(c_changes)}건")
            save_state(m_snap, c_snap)
        else:
            print("⏸️ 변화 없음 — skip")


if __name__ == "__main__":
    main()
