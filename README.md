# 기린 슬랙 봇

노션 두 DB를 매일 슬랙에 다이제스트로 발송하는 봇.

## 동작

- **매일 09:00 (KST)**: 풀 다이제스트 (이미지 2장 + 요약 텍스트) 무조건 발송
- **매일 12:00 / 15:00 / 18:00 (KST)**: 노션 diff 체크. 변화 있을 때만 발송.

## 환경 변수 (GitHub Secrets)

| Name | 설명 |
|------|------|
| `SLACK_BOT_TOKEN` | `xoxb-...` |
| `SLACK_CHANNEL_ID` | `C...` |
| `NOTION_TOKEN` | `ntn_...` |
| `NOTION_MILESTONE_DS_ID` | 신제품 마일스톤 Data Source ID (Notion API 2025-09-03) |
| `NOTION_COMMERCE_DS_ID` | 커머스팀 전체 업무 Data Source ID (Notion API 2025-09-03) |

## 로컬 실행

```bash
pip install -r requirements.txt
export SLACK_BOT_TOKEN=xoxb-...
# ... (다른 env 변수)
python main.py
```

`main.py --force`로 강제 풀 발송 (diff 무시).

## 자동 실행

`.github/workflows/digest.yml` cron이 매일 4회 실행. state/`previous_state.json`에 직전 상태 저장 후 diff 비교.
