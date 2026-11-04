"""지식 검색 색인과 질의. 문서: docs/implementation/v2.1/plan.md 2.3, Phase 1. Build-log의 search.py·index.py 패턴.

- 색인(`knowledge_fts`)은 `work_items`에서 다시 만들 수 있는 파생 데이터다. 항목을 쓰는 곳(`core._create_item`,
  `core._write_item`)이 같은 트랜잭션에서 갱신하고, `rebuild`로 언제든 처음부터 다시 만든다.
- 검색 경로는 하나의 색인으로 둘이다.
  · 3글자 이상 검색어: FTS5 trigram `MATCH`(색인 사용)
  · 3글자 미만 검색어: trigram `MATCH`는 3글자 미만에 결과를 내지 않는다(실험 확인). 같은 표에서 `scope` 한정 부분 일치(LIKE)로
    후보를 찾는다. 새 색인·새 표는 필요 없다.
  두 경로의 후보를 합쳐 같은 점수 규칙으로 정렬한다.
- 결과는 `max_tokens` 안의 Markdown으로 조립한다. 넘치면 점수 낮은 기록부터 빼고, 첫 기록 하나가 예산보다 크면 잘라서 돌려준다.
- 대체(`superseded`)·해결(`resolved`)된 항목은 점수를 낮춰 아래에 둔다. 취소(`cancelled`)·되돌린(`reverted`) 항목은 기본으로 뺀다.
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable

from . import core, db
from .terms import _WORD_RE, _strip_particle

INDEXED_ATTRS = ("assignee", "due", "maturity", "artifact_type", "summary")  # 본문 말고 색인할 속성(extra 열)
PENALTY = {"superseded": 0.6, "resolved": 0.6}  # 검색에는 나오되 살아 있는 항목보다 아래
DELETED = ("cancelled", "reverted")  # 기본으로 뺀다(논리 삭제)
FIELD_WEIGHTS = {"title": 6.0, "body": 1.5, "extra": 1.0}
SHORT_WEIGHT = {1: 0.4, 2: 0.7}  # 짧은 검색어는 아무 데나 걸리므로 가중치를 낮춘다
MIN_RELATIVE_SCORE = 0.2  # 가장 높은 점수의 이 비율보다 낮은 후보는 고르지 않는다
DEFAULT_LIMIT = 8
DEFAULT_MAX_TOKENS = 3000
MAX_RELATED = 5
MAX_TERMS = 8
KIND_LABELS = {"decision": "결정", "open": "미정", "task": "할 일", "insight": "인사이트", "feedback": "피드백",
               "artifact": "산출물", "requirement": "요구사항", "hypothesis": "가설"}


# ── 색인 ─────────────────────────────────────────────────

def _attrs(row: Any) -> dict[str, Any]:
    try:
        return json.loads(row["attributes_json"] or "{}")
    except (TypeError, ValueError):
        return {}


def document(row: Any) -> tuple[str, str, str, str, str, str]:
    """work_items 한 행 → (item_id, scope, kind, title, body, extra). 색인 내용은 여기 한 곳에서 정한다."""
    attributes = _attrs(row)
    extra = " ".join(str(attributes[key]) for key in INDEXED_ATTRS if attributes.get(key))
    return (row["id"], row["scope"], row["kind"], row["title"], row["statement_md"], extra)


def index_items(c: sqlite3.Connection, item_ids: Iterable[str]) -> int:
    """항목 몇 개의 색인을 다시 쓴다(이미 열린 쓰기 트랜잭션 안에서). 없는 항목은 색인에서 지운다."""
    count = 0
    for item_id in item_ids:
        c.execute("DELETE FROM knowledge_fts WHERE item_id = ?", (item_id,))
        row = c.execute("SELECT * FROM work_items WHERE id = ?", (item_id,)).fetchone()
        if row is not None:
            c.execute("INSERT INTO knowledge_fts(item_id, scope, kind, title, body, extra) VALUES (?, ?, ?, ?, ?, ?)",
                      document(row))
            count += 1
    return count


def rebuild_connection(c: sqlite3.Connection, scope: str | None = None) -> int:
    """색인을 처음부터 다시 만든다(scope가 있으면 그 팀만). 마이그레이션과 `reindex-fts`가 쓴다."""
    if scope is None:
        c.execute("DELETE FROM knowledge_fts")
        rows = c.execute("SELECT * FROM work_items").fetchall()
    else:
        c.execute("DELETE FROM knowledge_fts WHERE scope = ?", (scope,))
        rows = c.execute("SELECT * FROM work_items WHERE scope = ?", (scope,)).fetchall()
    for row in rows:
        c.execute("INSERT INTO knowledge_fts(item_id, scope, kind, title, body, extra) VALUES (?, ?, ?, ?, ?, ?)",
                  document(row))
    return len(rows)


def rebuild(store: Path, scope: str | None = None) -> int:
    db.initialize(store)
    with db.write(store) as c:
        return rebuild_connection(c, scope)


# ── 검색어 ───────────────────────────────────────────────

def search_terms(text: str) -> list[str]:
    """질문 → 검색어. 조사를 떼고(Build-log 방식), 중복을 없앤다. 한 글자는 다른 검색어가 없을 때만 쓴다
    ("옷"만 검색하는 경우). 질문 속 한 글자("이", "그")는 거의 모든 항목에 걸려서 버린다."""
    words = [_strip_particle(w) for w in _WORD_RE.findall(unicodedata.normalize("NFKC", text or ""))]
    terms: list[str] = []
    seen: set[str] = set()
    for word in words:
        key = word.casefold()
        if word and key not in seen:
            seen.add(key)
            terms.append(word)
    longer = [t for t in terms if len(t) > 1]
    return (longer or terms)[:MAX_TERMS]


def _phrase(term: str) -> str:
    return '"' + term.replace('"', '""') + '"'


def _like(term: str) -> str:
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


# ── 후보와 점수 ──────────────────────────────────────────

def _candidates(c: sqlite3.Connection, scope: str, terms: list[str]) -> tuple[set[str], int, int]:
    """(후보 id, MATCH 경로 건수, 부분 일치 경로 건수)."""
    long_terms = [t for t in terms if len(t) >= 3]
    short_terms = [t for t in terms if len(t) < 3]
    matched: set[str] = set()
    liked: set[str] = set()
    if long_terms:
        expression = "{title body extra} : (" + " OR ".join(_phrase(t) for t in long_terms) + ")"
        for row in c.execute("SELECT item_id FROM knowledge_fts WHERE knowledge_fts MATCH ? AND scope = ?", (expression, scope)):
            matched.add(row[0])
    if short_terms:
        clause = " OR ".join("(title LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\' OR extra LIKE ? ESCAPE '\\')" for _ in short_terms)
        args: list[str] = [scope]
        for term in short_terms:
            args += [_like(term)] * 3
        for row in c.execute(f"SELECT item_id FROM knowledge_fts WHERE scope = ? AND ({clause})", args):
            liked.add(row[0])
    return matched | liked, len(matched), len(liked)


def _score(terms: list[str], title: str, body: str, extra: str, status: str) -> float:
    title, body, extra = title.casefold(), body.casefold(), extra.casefold()
    total, hits = 0.0, 0
    for term in terms:
        key = term.casefold()
        if key in title:
            weight = FIELD_WEIGHTS["title"]
        elif key in body:
            weight = FIELD_WEIGHTS["body"]
        elif key in extra:
            weight = FIELD_WEIGHTS["extra"]
        else:
            continue
        hits += 1
        total += weight * SHORT_WEIGHT.get(len(term), 1.0)
    if not hits:
        return 0.0
    total *= 0.5 + 0.5 * hits / len(terms)  # 질문의 단어를 더 많이 가진 항목이 위
    return total * PENALTY.get(status, 1.0)


def _chunks(items: list[str], size: int = 400) -> Iterable[list[str]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _item_sources(c: sqlite3.Connection, attributes: dict[str, Any]) -> list[dict[str, str]]:
    out = []
    for part_id in (attributes.get("part_ids") or [])[:2]:
        row = c.execute("SELECT path, last_edited FROM source_parts WHERE id = ?", (part_id,)).fetchone()
        if row is not None:
            out.append({"path": row["path"], "last_edited": row["last_edited"] or ""})
    return out


# ── 질의 ─────────────────────────────────────────────────

def _render(record: dict[str, Any], body_limit: int | None = None) -> str:
    tags = [KIND_LABELS.get(record["kind"], record["kind"])]
    if record["status"] not in ("active", "open", "in_progress"):
        tags.append(f"상태: {record['status']}")
    if record.get("maturity"):
        tags.append(f"성숙도: {record['maturity']}")
    body = record["body"].strip()
    where = " / ".join(f"{s['path']}" + (f" ({s['last_edited'][:10]})" if s["last_edited"] else "") for s in record["sources"])
    suffix = ""
    if body_limit is not None and len(body) > body_limit:
        body = body[:max(body_limit, 0)].rstrip() + " …"
        suffix = "\n(이하 생략" + (f", 원문 위치: {where}" if where else "") + ")"
    lines = [f"### {record['title']}  [{' · '.join(tags)}] (id: {record['id']})"]
    if body and body != record["title"]:
        lines.append(body)
    if where and not suffix:
        lines.append(f"출처: {where}")
    return "\n".join(lines) + suffix


def _fit(text: str, max_tokens: int) -> str:
    """예산을 넘으면 끝을 자른다(마지막 안전장치)."""
    while core.estimate_tokens(text) > max_tokens and len(text) > 1:
        text = text[: max(1, int(len(text) * 0.9))]
    return text


def query(store: Path, scope: str, text: str, kinds: Iterable[str] | None = None, limit: int = DEFAULT_LIMIT,
          max_tokens: int = DEFAULT_MAX_TOKENS, include_deleted: bool = False) -> dict[str, Any]:
    """질문으로 관련 항목을 찾는다. {records, markdown, related, conflicts, searched}. Notion·LLM을 부르지 않는다."""
    db.initialize(store)
    terms = search_terms(text)
    kind_filter = set(kinds) if kinds else None
    searched: dict[str, Any] = {"scope": scope, "query": text, "terms": terms, "kinds": sorted(kind_filter or []),
                                "paths": {"match": 0, "like": 0}, "candidates": 0, "returned": 0, "omitted": 0,
                                "truncated": False, "deleted_excluded": 0, "indexed": 0, "max_tokens": max_tokens,
                                "used_tokens": 0, "omitted_context": 0}
    empty = {"records": [], "markdown": "", "related": [], "conflicts": [], "searched": searched}
    with closing(db.connect(store)) as c:
        searched["indexed"] = c.execute("SELECT COUNT(*) FROM knowledge_fts WHERE scope = ?", (scope,)).fetchone()[0]
        if not terms:
            return empty
        ids, n_match, n_like = _candidates(c, scope, terms)
        searched["paths"] = {"match": n_match, "like": n_like}
        searched["candidates"] = len(ids)
        scored: list[tuple[float, str, dict[str, Any]]] = []
        for chunk in _chunks(sorted(ids)):
            marks = ",".join("?" * len(chunk))
            for row in c.execute(f"SELECT * FROM work_items WHERE id IN ({marks})", chunk):
                if kind_filter and row["kind"] not in kind_filter:
                    continue
                if row["status"] in DELETED and not include_deleted:
                    searched["deleted_excluded"] += 1
                    continue
                attributes = _attrs(row)
                extra = " ".join(str(attributes[k]) for k in INDEXED_ATTRS if attributes.get(k))
                score = _score(terms, row["title"], row["statement_md"], extra, row["status"])
                if score > 0:
                    scored.append((score, row["updated_at"] or "", dict(row)))
        if not scored:
            return empty
        scored.sort(key=lambda s: s[1], reverse=True)  # 같은 점수에서는 최근 수정이 앞(정렬은 안정적)
        scored.sort(key=lambda s: -s[0])
        best = scored[0][0]
        picked = [s for s in scored if s[0] >= best * MIN_RELATIVE_SCORE][:max(limit, 1)]
        records = []
        for score, _, row in picked:
            attributes = _attrs(row)
            records.append({"id": row["id"], "kind": row["kind"], "title": row["title"], "status": row["status"],
                            "maturity": attributes.get("maturity"), "score": round(score, 3), "body": row["statement_md"],
                            "assignee": attributes.get("assignee"), "sources": _item_sources(c, attributes)})
        related, conflicts = _neighbors(c, [r["id"] for r in records])

    # 예산 안에서 Markdown 조립: 점수 순으로 담고, 안 들어가면 건너뛴다. 첫 기록은 필요하면 잘라서라도 담는다.
    header = f"## 검색 결과 (질의: {text.strip()[:80]})"
    used = core.estimate_tokens(header)
    blocks: list[str] = []
    for record in records:
        block = _render(record)
        cost = core.estimate_tokens(block)
        if used + cost <= max_tokens:
            blocks.append(block)
            used += cost
        elif not blocks:  # 첫 기록이 예산보다 크면 본문을 잘라서라도 담는다(Build-log의 BudgetTooSmall 대신)
            limit = len(record["body"])
            block = _render(record, body_limit=limit)
            while limit > 0 and used + core.estimate_tokens(block) > max_tokens:
                limit = int(limit * 0.8)
                block = _render(record, body_limit=limit)
            blocks.append(block)
            searched["truncated"] = True
            used += core.estimate_tokens(block)
        else:
            searched["omitted"] += 1
    kept = len(blocks)
    tail: list[str] = []
    if conflicts:
        tail.append("### 대체·충돌\n" + "\n".join(f"- {x['new']['title']} → 앞선 결정 \"{x['old']['title']}\"을(를) 대체" for x in conflicts))
    if related:
        tail.append("### 관련 항목\n" + "\n".join(f"- [{x['relation']}] {x['title']} (id: {x['id']})" for x in related))
    for section in tail:
        cost = core.estimate_tokens(section)
        if used + cost <= max_tokens:
            blocks.append(section)
            used += cost
        else:
            searched["omitted_context"] += 1
    markdown = _fit("\n\n".join([header, *blocks]), max_tokens)
    if len(markdown) < len("\n\n".join([header, *blocks])):
        searched["truncated"] = True
    searched.update(returned=kept, used_tokens=core.estimate_tokens(markdown))
    return {"records": records[:kept], "markdown": markdown, "related": related, "conflicts": conflicts,
            "searched": searched}


def _neighbors(c: sqlite3.Connection, ids: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """결과 항목과 링크로 이어진 항목(related)과 대체 관계(conflicts)."""
    if not ids:
        return [], []
    marks = ",".join("?" * len(ids))
    links = c.execute(f"""SELECT l.type, l.source_id, l.target_id, s.title AS s_title, t.title AS t_title,
                                 s.kind AS s_kind, t.kind AS t_kind
                          FROM work_links l JOIN work_items s ON s.id = l.source_id JOIN work_items t ON t.id = l.target_id
                          WHERE l.source_id IN ({marks}) OR l.target_id IN ({marks})""", (*ids, *ids)).fetchall()
    inside = set(ids)
    related: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for link in links:
        if link["type"] == core.LINK_SUPERSEDES:
            conflicts.append({"new": {"id": link["source_id"], "title": link["s_title"]},
                              "old": {"id": link["target_id"], "title": link["t_title"]}})
        other, title = ((link["target_id"], link["t_title"]) if link["source_id"] in inside
                        else (link["source_id"], link["s_title"]))
        if other not in inside and other not in seen and len(related) < MAX_RELATED:
            seen.add(other)
            related.append({"id": other, "title": title, "relation": link["type"]})
    return related, conflicts
