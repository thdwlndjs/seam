"""서버 ↔ worklog(같은 폴더의 패키지) 연결.

- 원문 저장: Notion 회의록 행을 worklog 저장소에 넣는다. Teams 채팅 메시지는 v1.1부터 저장하지 않는다.
- 분석(`analyze_due`): 분석 정책에 걸린 묶음을 Groq로 분석해 변경 후보로 저장하는 예전 경로다. v1.1부터 서버 루프에서
  부르지 않고, Phase 3에서 미리보기 확인 반영으로 바뀐다(docs/v1.1/spec.md P1-0).

Notion·GitHub 데이터는 읽기만 하고 절대 바꾸지 않는다.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import httpx

import groq_queue
import metrics
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
# 서버가 켜진 뒤 첫 수집까지 기다리는 시간(켜자마자 들어오는 사용자 요청과 겹치지 않게).
START_DELAY_SECONDS = int(os.environ.get("WORKLOG_START_DELAY_SECONDS", "60"))
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
def _groq_call(system_prompt: str, payload: Any, priority: int = groq_queue.BACKGROUND,
               model: str | None = None) -> tuple[Any, dict]:
    """JSON 응답 한 번. 백그라운드(순위 2·3)는 먼저 대기열 한도를 확인한다(넘으면 groq_queue.Busy).
    (응답 JSON, usage)를 돌려준다."""
    body = json.dumps(payload, ensure_ascii=False)
    groq_queue.check(priority, len(body) * 3 // 4 + len(system_prompt) // 2, WORKLOG_STORE)
    with httpx.Client(timeout=60) as client:
        resp = client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            json={
                "model": model or GROQ_MODEL,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": body},
                ],
                "temperature": 0,
                "response_format": {"type": "json_object"},
            },
        )
        usage = usage_of(resp) or {}
        metrics.count_groq(usage)
        groq_queue.record(WORKLOG_STORE, priority, getattr(resp, "headers", None), getattr(resp, "status_code", 200),
                          int(usage.get("total_tokens") or 0))
        resp.raise_for_status()
        return json.loads(resp.json()["choices"][0]["message"]["content"]), usage


def _groq_json(system_prompt: str, payload: Any) -> Any:
    return _groq_call(system_prompt, payload)[0]


def usage_of(resp: httpx.Response) -> dict | None:
    """Groq 응답의 usage. 본문이 JSON이 아니면(오류 응답 등) None."""
    try:
        return resp.json().get("usage")
    except Exception:
        return None


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


PART_KIND_PROMPT = """너는 팀 Notion 회의록·문서 페이지를 나눈 '부분'이 어떤 글인지 고르는 분류기야.
입력: page_path(페이지 위치)와 parts 배열. 각 part에 part_id, path, title, chars(길이), head(앞부분),
children(그 부분 안의 접힌 토글: child_path, chars)이 있다.
kind:
- "meeting": 회의·멘토링에서 나온 메모, 피드백, 결정, 할 일, 질문과 답
- "document": 기획서·제안서·조사 자료처럼 따로 쓴 긴 문서, 양식, 소개 글
- "link": 링크·첨부·녹음 노트 공유'만' 있는 부분. 메모·피드백·결론 문장이 한 줄이라도 섞여 있으면 meeting이다
회의 메모 안에 기획서 같은 긴 문서가 토글로 붙어 있으면, 그 부분은 meeting으로 두고
그 토글을 split에 넣어 document로 떼어 내. 메모 토글(기능, 멘토링 등)은 떼지 마.
document·link에는 summary(40자 이내 한 줄 설명: 무엇에 대한 문서인지, 작성 기준일이 있으면 함께)를 붙여.
JSON 하나만 출력해: {"parts": [{"part_id": "...", "kind": "...", "confidence": 0.0~1.0, "summary": "..." 또는 null,
"reason": "한 줄", "split": [{"child_path": "...", "kind": "document", "summary": "..."}]}]}.
판단할 근거가 없으면 그 part_id는 빼."""


def groq_classify_parts(page: dict) -> dict:
    """한 페이지의 새로 생기거나 바뀐 부분만 종류를 판정한다(docs/v1.1/spec.md P1-3). Groq 키가 없으면 미룬다."""
    if not GROQ_API_KEY or not page.get("parts"):
        return {}
    data = _groq_json(PART_KIND_PROMPT, page)
    return data if isinstance(data, dict) else {}


# 추출 지시문의 판. 지시문이나 항목 구성을 바꾸면 올린다(같은 내용도 새 판으로 다시 추출된다).
EXTRACT_PROMPT_VERSION = "part-extract-1"
GROQ_BACKGROUND_MODEL = os.environ.get("GROQ_BACKGROUND_MODEL") or GROQ_MODEL

PART_EXTRACT_PROMPT = """너는 팀 회의·멘토링 메모에서 항목을 뽑는 분석기야. 입력: path(회의 위치)와 text(메모 원문).
뽑을 항목(kind):
- "decision": 정한 것, 확정된 방향
- "task": 누가 무엇을 해야 하는 일. owner(메모에 적힌 이름, 없으면 null), due(YYYY-MM-DD, 없으면 null),
  role_hint("plan" 기획 | "design" 디자인 | "dev" 개발 | null)
- "open": 아직 정하지 못했거나 논의가 필요한 것, 열린 질문
- "feedback": 멘토·외부 사람이 준 조언·지적·의견
- "insight": 고객·사용자 조사(VOC)에서 얻은 사실이나 발견
각 항목: {"kind", "title"(한 줄, 40자 안팎), "detail"(필요하면 한두 문장, 없으면 null), "owner", "due",
"role_hint", "quote"(근거가 된 원문을 text에서 그대로 복사한 짧은 구절), "confidence"(0.0~1.0)}.
quote는 반드시 text 안에 글자 그대로 있어야 한다. 근거 없는 항목은 만들지 마.
JSON 하나만 출력해: {"items": [...]}. 뽑을 것이 없으면 {"items": []}."""


def groq_extract_part(payload: dict) -> dict:
    """회의 메모 부분 하나에서 항목을 뽑는다(docs/v1.1/spec.md P1-4). {"items", "usage", "model"}.
    Groq 키가 없으면 빈 dict(추출을 미룬다). 백그라운드 순위라 한도에 걸리면 groq_queue.Busy."""
    if not GROQ_API_KEY:
        return {}
    data, usage = _groq_call(PART_EXTRACT_PROMPT, payload, groq_queue.BACKGROUND, GROQ_BACKGROUND_MODEL)
    items = data.get("items") if isinstance(data, dict) else None
    return {"items": items if isinstance(items, list) else [], "usage": usage, "model": GROQ_BACKGROUND_MODEL}


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


def team_snapshot(scope: str, root_id: str | None = None, max_chars: int = 12000) -> dict | None:
    """저장된 팀 정보 페이지 원문(1번 메뉴 요약용). 아직 수집 전이면 None."""
    from worklog import sections

    return sections.snapshot(WORKLOG_STORE, scope, root_id=root_id, max_chars=max_chars)


def meeting_notes(scope: str, notes_id: str | None, max_chars: int = 8000) -> dict | None:
    """저장된 회의 기록(2·3번 메뉴용). 아직 수집 전이면 None."""
    from worklog import sections

    return sections.meeting_notes(WORKLOG_STORE, scope, notes_id, max_chars=max_chars)
