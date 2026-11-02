"""서버 ↔ worklog(같은 폴더의 패키지) 연결.

- 원문 저장: Teams 메시지와 Notion 회의록 행을 받는 즉시 worklog 저장소에 넣는다.
- 분석: 주기적으로 분석 정책(Teams 60분 유휴 / Notion 수정 후 2시간 / 20개·3,000토큰)에 걸린 묶음을 Groq로 분석한다.
  결과는 변경 후보로만 저장되고, Task·Decision 반영은 사람이 `python -m worklog approve`로 승인해야 한다.

Notion·GitHub 데이터는 읽기만 하고 절대 바꾸지 않는다.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import httpx

import worklog

log = logging.getLogger("worklog")

WORKLOG_STORE = Path(
    os.environ.get("WORKLOG_STORE")
    or Path(__file__).resolve().parent.parent / "worklog.sqlite3"
)
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
# 분석할 묶음이 있는지 확인하는 주기. 확인 자체는 로컬 DB 조회뿐이라 비용이 없고, 분석 시점의 오차만 정한다.
ANALYZE_TICK_SECONDS = int(os.environ.get("WORKLOG_TICK_SECONDS", "300"))
# Notion 접근 주기. 접근할 때마다 페이지 수정 시각 → (바뀌었으면) 구조 지문을 먼저 확인한다.
NOTION_COLLECT_SECONDS = int(os.environ.get("WORKLOG_NOTION_SECONDS", "3600"))
POLICY = worklog.AnalysisPolicy(
    idle_seconds=int(os.environ.get("WORKLOG_IDLE_SECONDS", "3600")),
    max_messages=int(os.environ.get("WORKLOG_MAX_MESSAGES", "20")),
    max_tokens=int(os.environ.get("WORKLOG_MAX_TOKENS", "3000")),
    settle_seconds=int(os.environ.get("WORKLOG_SETTLE_SECONDS", "7200")),
)


def record(*, scope: str, kind: str, conversation_key: str, locator: str, text: str,
           author: str | None = None, sent_at: str | None = None) -> bool:
    """원문 한 건 저장. 새로 저장했으면 True. 실패해도 봇 응답을 막지 않도록 예외를 삼키고 False."""
    if not text.strip():
        return False
    try:
        return worklog.record(WORKLOG_STORE, scope=scope, kind=kind, conversation_key=conversation_key,
                              locator=locator, text=text, author=author, sent_at=sent_at)["created"]
    except Exception:
        log.exception("원문 저장 실패: %s", locator)
        return False


# ── Groq 호출 (동기 — 분석은 별도 스레드에서 돈다) ─────────────────
def _groq_json(system_prompt: str, payload: Any) -> Any:
    with httpx.Client(timeout=60) as client:
        resp = client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            json={
                "model": GROQ_MODEL,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                "temperature": 0,
                "response_format": {"type": "json_object"},
            },
        )
        resp.raise_for_status()
        return json.loads(resp.json()["choices"][0]["message"]["content"])


EXTRACT_PROMPT = """너는 팀 대화와 회의록에서 Task와 Decision 후보를 뽑는 분석기야.
입력은 records 배열이고 각 항목에 source_id, text가 있다.
items 배열 하나만 담은 JSON 객체를 출력해: {"items": [...]}.
각 item 필드:
- kind: "task"(새로 할 일) | "decision"(정해진 방침) | "progress"(기존 일의 진행 보고) | "opinion"(의견) | "question"(질문)
- title: 짧은 이름. 같은 대상이면 매번 같은 이름을 써.
- statement: 무엇을 하거나 정했는지 한 문장
- certainty: decision일 때만. "decided"(확정 표현이 원문에 있음) | "proposed"(제안·논의 중)
- areas: decision일 때 해당하는 것만: "goal","scope","architecture","budget","schedule"
- status: task/progress일 때 "open","in_progress","done","cancelled" 중 하나
- assignee, due(YYYY-MM-DD): 원문에 있을 때만
- source_ids: 근거가 된 record의 source_id 목록 (입력에 있는 값만)
- quote: 근거 원문을 그대로 짧게 인용 (원문에 있는 문자열 그대로)
원문에 없는 내용을 지어내지 마. 확정 표현이 없으면 decision의 certainty는 "proposed"다.
단순 진행 보고는 decision이 아니라 progress다. 해당 사항이 없으면 {"items": []}."""

JUDGE_PROMPT = """너는 새 기록에서 나온 항목(item)을 기존 Task·Decision 목록(related)과 비교하는 판정기야.
JSON 객체 하나만 출력해: {"change_type", "target_id", "affected_task_ids", "rationale"}.
change_type:
- "unchanged": 기존 항목과 같은 내용
- "supplementary": 기존 항목에 세부 사항·진행 상태를 더할 뿐 의미를 바꾸지 않음
- "proposed": 기존 어떤 항목과도 다른 새 항목 (target_id는 null)
- "conflicting": 기존 항목과 모순되지만 교체한다는 말은 없음
- "superseding": 기존 결정을 명시적으로 대체함 (decision끼리만)
target_id: 비교 대상 기존 항목의 id (related 안의 값만, 같은 kind만). proposed면 null.
affected_task_ids: item이 decision일 때 영향을 받는 기존 task id (related 안의 값만).
rationale: 판단 근거 한두 문장. 확신이 없으면 conflicting으로 두고 이유를 적어."""


CLASSIFY_PROMPT = """너는 팀 Notion 페이지의 섹션이 어떤 기록인지 고르는 분류기야.
입력 sections 배열의 각 항목에 id, path(페이지 안 위치), title, container(담는 방식), sample(내용 앞부분)이 있다.
제목 단어만 보지 말고 sample 내용으로 판단해. 제목이 사람 이름이나 날짜·주차여도 내용이 업무 기록이면 그 역할이다.
role:
- "progress": 팀원별·주차별 진행, 한 일/할 일, 데일리·스프린트 기록
- "meeting": 회의·회고(KPT, Retro 등)·결정 사항 기록
- "reference": 멘토링·피드백, WBS·일정·로드맵, 참고 자료 (보관만 함)
- "ignore": 소개, 규칙·원칙, 양식·템플릿, 안내, 연락처 등 작업 기록이 아닌 것
JSON 하나만 출력해: {"sections": {"<id>": {"role": "...", "confidence": "high|medium|low", "reason": "한 줄"}}}.
confidence가 low인 판정은 분석에 쓰이지 않고 원문 보관만 된다. 판단할 근거가 없으면 그 id는 빼."""


def groq_classify_sections(items: list[dict]) -> dict[str, dict]:
    """새로 생기거나 이름이 바뀐 섹션만 판정한다. 판정은 바로 쓰이고, 확신이 low면 원문만 보관한다."""
    if not GROQ_API_KEY or not items:
        return {}
    data = _groq_json(CLASSIFY_PROMPT, {"sections": items})
    found = data.get("sections") if isinstance(data, dict) else None
    return found if isinstance(found, dict) else {}


def groq_extract(records: list[dict]) -> list[dict]:
    payload = {"records": [{"source_id": r["source_id"], "text": r["text"], "author": r.get("author")} for r in records]}
    data = _groq_json(EXTRACT_PROMPT, payload)
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ValueError(f"extract 응답 형식이 아님: {str(data)[:200]}")
    return items


def groq_judge(item: dict, related: list[dict]) -> dict:
    return _groq_json(JUDGE_PROMPT, {"item": item, "related": related})


def analyze_due() -> list[dict]:
    """정책에 걸린 묶음을 분석한다. Groq 키가 없으면 원문만 쌓이고 분석은 건너뛴다."""
    if not GROQ_API_KEY:
        return []
    results = worklog.analyze_due(WORKLOG_STORE, groq_extract, groq_judge, policy=POLICY)
    for result in results:
        if "error" in result:
            log.warning("분석 실패(다음 주기에 재시도): %s", result["error"])
        else:
            log.info("분석 완료: 후보 %d건, 제외 %d건", len(result["candidates"]), len(result["ignored"]))
    return results
