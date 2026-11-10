"""GitHub-originated work records for Phase 7. No GitHub API calls live here."""
from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path
from typing import Any

from . import board, core, db, leading, search
from .db import ContractError, NotFoundError, ValidationError

MAX_ISSUES = 8
MAX_TITLE = 160
MAX_BODY = 6000
MAX_CRITERIA = 8
MAX_CRITERION = 500


def validate_issue_proposals(raw: Any, allowed_assignees: dict[str, str]) -> list[dict[str, Any]]:
    """Validate model output and restrict assignments to registered team members."""
    if not isinstance(raw, dict) or not isinstance(raw.get("issues"), list):
        raise ContractError("issue breakdown must contain an issues list")
    entries = raw["issues"]
    if not entries or len(entries) > MAX_ISSUES:
        raise ContractError(f"issue breakdown must contain 1 to {MAX_ISSUES} issues")
    proposals: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ContractError("each issue proposal must be an object")
        title = entry.get("title")
        body = entry.get("body")
        criteria = entry.get("acceptance_criteria")
        assignee = entry.get("assignee")
        if not isinstance(title, str) or not title.strip() or len(title.strip()) > MAX_TITLE:
            raise ContractError("issue title must contain 1 to 160 characters")
        if not isinstance(body, str) or not body.strip() or len(body.strip()) > MAX_BODY:
            raise ContractError("issue body must contain 1 to 6000 characters")
        if (not isinstance(criteria, list) or not criteria or len(criteria) > MAX_CRITERIA
                or any(not isinstance(c, str) or not c.strip() or len(c.strip()) > MAX_CRITERION for c in criteria)):
            raise ContractError("acceptance criteria must contain 1 to 8 non-empty strings")
        title = title.strip()
        key = db.normalize_text(title)
        if key in seen:
            raise ContractError("issue proposal titles must be unique")
        seen.add(key)
        if assignee is not None and (not isinstance(assignee, str) or assignee not in allowed_assignees):
            raise ContractError("issue proposal contains an unknown assignee")
        proposals.append({"title": title, "body": body.strip(),
                          "acceptance_criteria": [c.strip() for c in criteria],
                          "assignee": assignee,
                          "assignee_name": allowed_assignees.get(assignee) if assignee else None,
                          "include": True})
    return proposals


def issue_source(store: Path, scope: str, item_id: str) -> dict[str, Any]:
    """Return a saved decision/requirement for an explicitly requested issue proposal."""
    db.initialize(store)
    with closing(db.connect(store)) as c:
        row = c.execute("SELECT * FROM work_items WHERE id=? AND scope=?", (item_id, scope)).fetchone()
        if row is None or row["status"] in board.HIDDEN:
            raise NotFoundError("저장된 항목을 찾지 못했어요")
        if row["kind"] not in ("decision", "requirement"):
            raise ValidationError("결정 또는 요구사항에서만 이슈를 제안할 수 있어요")
        attributes = json.loads(row["attributes_json"])
        linked_tasks = []
        for link in board._links(c, scope).get(item_id, []):
            if link["type"] == core.LINK_AFFECTS and link["other"]["kind"] == "task":
                task = c.execute("SELECT title, statement_md, status FROM work_items WHERE id=?", (link["other"]["id"],)).fetchone()
                if task is not None:
                    linked_tasks.append({"title": task["title"], "statement": task["statement_md"], "status": task["status"]})
        return {"id": row["id"], "kind": row["kind"], "title": row["title"],
                "statement": row["statement_md"], "maturity": attributes.get("maturity"),
                "quotes": board._quotes(c, item_id), "related_tasks": linked_tasks[:8]}


def share_context(store: Path, scope: str, text: str, limit: int = 6) -> list[dict[str, Any]]:
    """Find saved decisions, requirements, and tasks related to the selected GitHub item."""
    result = search.query(store, scope, text[:500], kinds=("decision", "requirement", "task", "artifact"),
                          limit=limit, max_tokens=1800)
    return [{"kind": item["kind"], "title": item["title"], "status": item["status"],
             "maturity": item.get("maturity"), "statement": item["body"][:500],
             "sources": [{"path": source["path"], "date": source.get("last_edited")} for source in item["sources"][:2]]}
            for item in result["records"]]


def validate_work_share(raw: Any) -> dict[str, str]:
    """Validate a short share draft; it cannot claim verification absent from the supplied facts."""
    if not isinstance(raw, dict) or set(raw) != {"title", "summary"}:
        raise ContractError("work share must contain title and summary only")
    title, summary = raw.get("title"), raw.get("summary")
    if not isinstance(title, str) or not title.strip() or len(title.strip()) > 120:
        raise ContractError("work share title must contain 1 to 120 characters")
    if not isinstance(summary, str) or not summary.strip() or len(summary.strip()) > 2000:
        raise ContractError("work share summary must contain 1 to 2000 characters")
    if leading.FORBIDDEN.search(summary):
        raise ContractError("work share cannot claim that implementation was verified")
    return {"title": title.strip(), "summary": summary.strip()}


def record_created_task(store: Path, scope: str, proposal: dict[str, Any], issue: dict[str, Any], actor: str,
                        source_item_id: str | None = None) -> str:
    """Persist only an issue that GitHub has confirmed as created, with an explicit link."""
    number = issue.get("number")
    url = issue.get("url")
    if not isinstance(number, int) or number < 1 or not isinstance(url, str) or not url.startswith("https://github.com/"):
        raise ValidationError("GitHub did not return a valid issue number and URL")
    criteria = "\n".join(f"- {line}" for line in proposal["acceptance_criteria"])
    statement = f"{proposal['body'].strip()}\n\n완료 기준:\n{criteria}\n\nGitHub 이슈: {url}"
    db.initialize(store)
    with db.write(store) as c:
        if source_item_id:
            source = board._live(c, scope, source_item_id)
            if source["kind"] not in ("decision", "requirement"):
                raise ValidationError("GitHub task source must be a decision or requirement")
        ts = db.now()
        item_id = core._create_item(c, scope, "task", {
            "title": proposal["title"], "statement": statement, "status": "open",
            "assignee": proposal.get("assignee_name"),
            "github_refs": [{"type": "issue", "number": number, "basis": "explicit"}],
        }, "승인된 GitHub 이슈 등록", actor, ts)
        if source_item_id:
            core._link(c, source_item_id, item_id, core.LINK_AFFECTS, None, ts)
        return item_id


def complete_linked_tasks(store: Path, scope: str, issue_numbers: set[int], repo: str, actor: str) -> list[dict[str, Any]]:
    """Mark tasks done only for explicitly linked closed issues, preserving a revision."""
    if not issue_numbers:
        return []
    if not isinstance(repo, str) or not repo.strip():
        raise ValidationError("GitHub repository is required")
    db.initialize(store)
    updated: list[dict[str, Any]] = []
    with db.write(store) as c:
        rows = c.execute("SELECT * FROM work_items WHERE scope=? AND kind='task' AND status != 'cancelled'", (scope,)).fetchall()
        for row in rows:
            attributes = json.loads(row["attributes_json"])
            matches = [r for r in core.github_refs(attributes)
                       if r.get("type") == "issue" and r.get("basis", "explicit") == "explicit"
                       and int(r["number"]) in issue_numbers]
            if not matches:
                continue
            additions = []
            for ref in matches:
                number = int(ref["number"])
                marker = f"https://github.com/{repo}/issues/{number}"
                note = f"GitHub 완료: {marker} (동작 검증 여부는 확인되지 않음)"
                if note not in row["statement_md"]:
                    additions.append(note)
            changes: dict[str, Any] = {}
            if row["status"] != "done":
                changes["status"] = "done"
            if additions:
                changes["statement_md"] = row["statement_md"].rstrip() + "\n\n" + "\n".join(additions)
            if not changes:
                continue
            core._write_item(c, row["id"], changes, "GitHub 완료 상태 동기화(검증 아님)", actor, db.now())
            updated.append({"id": row["id"], "title": row["title"],
                            "numbers": [int(r["number"]) for r in matches]})
    return updated


def completed_tasks(store: Path, scope: str, limit: int = 20) -> list[dict[str, Any]]:
    """Locally completed tasks with explicit GitHub issue links for menu 2/3 display."""
    db.initialize(store)
    with closing(db.connect(store)) as c:
        rows = c.execute("""SELECT title, attributes_json FROM work_items
                            WHERE scope=? AND kind='task' AND status='done'
                            ORDER BY updated_at DESC, rowid DESC LIMIT ?""", (scope, limit)).fetchall()
    out = []
    for row in rows:
        attributes = json.loads(row["attributes_json"])
        refs = [ref for ref in core.github_refs(attributes)
                if ref.get("type") == "issue" and ref.get("basis", "explicit") == "explicit"]
        if refs:
            out.append({"title": row["title"], "assignee": attributes.get("assignee"),
                        "numbers": sorted({int(ref["number"]) for ref in refs})})
    return out


def record_shared_artifact(store: Path, scope: str, title: str, summary: str, url: str,
                           source_type: str, actor: str) -> str:
    """Create an artifact and an evidence-backed GitHub source in one local transaction."""
    if source_type not in ("pr", "commit"):
        raise ValidationError("source must be a pull request or commit")
    if not url.startswith("https://github.com/") or not title.strip() or not summary.strip():
        raise ValidationError("title, summary, and a GitHub URL are required")
    db.initialize(store)
    ts = db.now()
    source_id, evidence_id = core.new_id(), core.new_id()
    metadata = {"scope": scope, "origin": "github", "github_type": source_type, "url": url, "title": title.strip()}
    with db.write(store) as c:
        item_id = core._create_item(c, scope, "artifact", {
            "title": title.strip(), "statement": f"{summary.strip()}\n\nGitHub 출처: {url}",
            "artifact_type": "개발 산출물", "summary": summary.strip(),
        }, "사람이 승인한 GitHub 작업물 공유", actor, ts)
        c.execute("""INSERT INTO sources(id,kind,locator,captured_at,accessible_excerpt,content,content_hash,metadata_json,parse_status)
                     VALUES (?, 'github', ?, ?, ?, ?, ?, ?, 'complete')""",
                  (source_id, url, ts, title.strip(), summary.strip(), db.sha256(summary.strip()), db.to_json(metadata)))
        c.execute("INSERT INTO evidence(id,source_id,section_locator,excerpt,evidence_kind) VALUES (?,?, 'github_artifact', ?, 'observed')",
                  (evidence_id, source_id, summary.strip()))
        c.execute("INSERT INTO object_evidence(object_type,object_id,evidence_id) VALUES ('work_item',?,?)",
                  (item_id, evidence_id))
        return item_id
