"""해석 대상 탐색(explore). 문서: docs/implementation/v2.1/plan.md 2.2, Phase 2.

Notion은 처음부터 정답 DB가 아니다. 회의 메모는 항상 해석하고(최근 회의가 출발점), 그 밖의 문서 부분은 LLM이 목록(메타구조)과
최근 회의 항목을 보고 "읽을 영역"을 고른다. 고르지 않은 영역은 원문만 보관하고 해석하지 않는다(질문 때 다시 탐색하지 않는다).

- 목록(카탈로그)에는 본문을 넣지 않는다: 페이지 제목·날짜·부분(영역) 제목·길이·한 줄 설명만.
- 고른 결과는 runs(stage = 'explore')에 소스(페이지)별로 남는다. 키에 구조 지문이 들어 있어서, 구조가 바뀐 소스만 다시 부른다.
  내용만 바뀐 부분은 다시 탐색하지 않는다(부분 ID가 같으면 계속 해석 대상이다).
- LLM 호출은 호출부가 넘긴다(worklog 패키지는 외부 서비스에 의존하지 않는다).
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

from . import db, parts, runs, sections
from .db import ContractError

EXPLORE_BUDGET_TOKENS = 4000  # [가정] 호출 하나의 입력(추정) 상한. 무료 한도(분당 8,000토큰)에 응답 몫을 남긴다. 넘으면 호출을 나눈다
MAX_READ = 40  # 호출 하나에서 고르는 영역 수의 상한
MAX_CALLS = 4  # 한 주기에 부르는 탐색 호출 수의 상한(나머지는 다음 주기)
AREA_STEPS = (40, 25, 15, 10, 6, 4, 2)  # 소스 하나가 예산을 넘으면 큰 영역부터 이만큼만 보인다
HUMAN = "human"

Explorer = Callable[[dict[str, Any]], dict[str, Any]]


def _done_keys(store: Path, scope: str) -> set[str]:
    db.initialize(store)
    with closing(db.connect(store)) as c:
        return {r["idempotency_key"] for r in c.execute(
            "SELECT idempotency_key FROM runs WHERE stage = 'explore' AND status = 'succeeded' AND idempotency_key LIKE ?",
            (f"explore:{scope}:%",))}


def pending_sources(store: Path, scope: str) -> list[dict[str, Any]]:
    """아직 탐색하지 않은(또는 구조가 바뀐) 소스. 선택할 문서 영역이 없는 페이지는 뺀다."""
    done = _done_keys(store, scope)
    return [s for s in sections.catalog(store, scope)
            if s["areas"] and parts.explore_key(scope, s["source_id"], s["fingerprint"]) not in done]


def _view(sources: list[dict[str, Any]], recent: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, str]]:
    """LLM에 보낼 입력. 영역 ID는 짧은 별칭(a1, a2…)으로 바꿔 토큰을 줄이고, 별칭 → 실제 영역 ID 표를 함께 돌려준다."""
    alias: dict[str, str] = {}
    shown = []
    for source in sources:
        areas = []
        for area in source["areas"]:
            name = f"a{len(alias) + 1}"
            alias[name] = area["area_id"]
            areas.append(dict(area, area_id=name))
        shown.append(dict(source, areas=areas))
    return {"sources": shown, "recent_meetings": recent, "max_read": MAX_READ}, alias


def _trim(source: dict[str, Any], recent: list[dict[str, Any]]) -> tuple[dict[str, Any], int]:
    """소스 하나가 혼자서 예산을 넘으면 큰 영역부터 남기고 줄인다. 뺀 영역 수."""
    areas = source["areas"]
    for limit in (len(areas), *AREA_STEPS):
        if limit > len(areas):
            continue
        keep = sorted(sorted(areas, key=lambda a: -a["chars"])[:limit], key=areas.index)
        trimmed = dict(source, areas=keep)
        if parts.estimate_tokens(_view([trimmed], recent)[0]) <= EXPLORE_BUDGET_TOKENS or limit == AREA_STEPS[-1]:
            return trimmed, len(areas) - len(keep)
    return source, 0


def batches(sources: list[dict[str, Any]], recent: list[dict[str, Any]]) -> list[tuple[list[dict[str, Any]], dict[str, int]]]:
    """소스를 예산에 맞게 호출 단위로 묶는다(차례대로). 각 묶음 = (소스들, 소스별로 뺀 영역 수)."""
    out: list[tuple[list[dict[str, Any]], dict[str, int]]] = []
    current: list[dict[str, Any]] = []
    omitted: dict[str, int] = {}
    for source in sources:
        fitted, cut = _trim(source, recent)
        if current and parts.estimate_tokens(_view([*current, fitted], recent)[0]) > EXPLORE_BUDGET_TOKENS:
            out.append((current, omitted))
            current, omitted = [], {}
        current.append(fitted)
        if cut:
            omitted[source["source_id"]] = cut
    if current:
        out.append((current, omitted))
    return out


def save_selection(store: Path, scope: str, source: dict[str, Any], area_ids: list[str], reasons: dict[str, str] | None = None,
                   by: str = "llm", model: str | None = None, omitted: int = 0) -> None:
    """한 소스(페이지)의 고른 영역을 남긴다(해석 대상이 된다)."""
    runs.put(store, parts.explore_key(scope, source["source_id"], source["fingerprint"]), "explore", "succeeded",
             result={"area_ids": area_ids, "reasons": reasons or {}, "by": by, "model": model, "omitted": omitted,
                     "title": source.get("title")})


def select_all(store: Path, scope: str, page_id: str, by: str) -> int:
    """사람이 직접 올린 자료(4번 ④): 이 페이지의 문서 부분을 모두 해석 대상으로 둔다. 고른 영역 수."""
    source = next((s for s in sections.catalog(store, scope, include_all=True) if s["source_id"] == page_id), None)
    if source is None:
        return 0
    ids = [a["area_id"] for a in source["areas"]]
    save_selection(store, scope, source, ids, by=by)
    return len(ids)


def run(store: Path, scope: str, explore: Explorer | None, prompt_version: str) -> int:
    """탐색이 필요한 소스를 탐색한다(호출 하나에 예산만큼, 한 주기에 MAX_CALLS번까지). 탐색한 소스 수.
    회의 메모가 아직 해석 중이면 기다린다(최근 회의 항목을 보고 고르기 때문). LLM이 없거나 한도에 걸리면 멈춘다."""
    if explore is None:
        return 0
    pending = pending_sources(store, scope)
    if not pending:
        return 0
    if [p for p in parts.pending_extracts(store, scope, prompt_version) if p["kind"] == "meeting"]:
        return 0
    recent = sections.recent_meetings(store, scope, prompt_version)
    explored = 0
    for sources, omitted in batches(pending, recent)[:MAX_CALLS]:
        payload, alias = _view(sources, recent)
        keys = {s["source_id"]: parts.explore_key(scope, s["source_id"], s["fingerprint"]) for s in sources}
        try:
            answer = explore(payload) or {}
            if "read" not in answer:
                return explored  # LLM 없음
            if not isinstance(answer["read"], list):
                raise ContractError("read must be a list")
        except ContractError as error:
            for key in keys.values():
                runs.report(store, key, "explore", "contract", str(error))
            return explored
        except Exception as error:
            if type(error).__name__ == "Busy" or "429" in str(error):
                return explored
            for key in keys.values():
                runs.report(store, key, "explore", "llm_failed", str(error), count=False)
            return explored
        owner = {alias[a["area_id"]]: s["source_id"] for s in payload["sources"] for a in s["areas"]}
        picked: dict[str, dict[str, str]] = {s["source_id"]: {} for s in sources}
        total = 0
        for entry in answer["read"]:
            name = entry.get("area_id") if isinstance(entry, dict) else None
            real = alias.get(name) if isinstance(name, str) else None
            if real is None or total >= MAX_READ:
                continue  # 목록에 없는 id·한도를 넘는 선택은 버린다
            if real not in picked[owner[real]]:
                picked[owner[real]][real] = str(entry.get("reason") or "")[:200]
                total += 1
        for source in sources:
            found = picked[source["source_id"]]
            save_selection(store, scope, source, list(found), reasons=found, model=answer.get("model"),
                           omitted=omitted.get(source["source_id"], 0))
        explored += len(sources)
    return explored
