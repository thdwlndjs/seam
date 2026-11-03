"""회의 메모 부분을 지금 추출하고, 사람이 고칠 추출 초안(md)을 만든다. 문서: docs/v1.1/phases/phase-2-extracts.md.

    cd src/server
    ../../.venv/Scripts/python.exe extract_draft.py --scope <team_key> [--match "차 멘토링"] [--out <파일>]

1. 종류 판정 전인 부분을 판정하고, 추출 전인 회의 메모 부분을 추출한다(Groq 대기열 한도를 지키며,
   분당 한도에 걸리면 기다렸다 이어 한다. 하루 몫을 넘으면 멈춘다).
2. --match에 맞는 경로의 부분만 골라, 부분마다 뽑힌 항목을 md로 적는다. 이 파일을 고쳐 정답지로 쓴다.

초안에는 실제 회의 내용이 들어간다. 저장소는 공개이므로 기본 위치는 커밋하지 않는 devTools/ 이다.
Notion은 부르지 않는다.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(os.environ.get("ENV_FILE"))

import groq_queue  # noqa: E402
import metrics  # noqa: E402
import worklog_bridge  # noqa: E402
from worklog import parts  # noqa: E402

KIND_LABELS = {"decision": "결정", "task": "할 일", "open": "미정", "feedback": "피드백", "insight": "인사이트"}
DEFAULT_OUT = Path(__file__).resolve().parents[2] / "devTools"


def organize(store: Path, scope: str, max_rounds: int = 30, sleep=time.sleep) -> dict[str, int]:
    """판정·추출을 끝까지(또는 하루 몫까지). 분당 한도면 기다렸다 이어 한다."""
    judged = extracted = 0
    version = worklog_bridge.EXTRACT_PROMPT_VERSION
    for _ in range(max_rounds):
        judged += parts.judge_pending(store, scope, worklog_bridge.groq_classify_parts, budget_tokens=None)
        extracted += parts.extract_pending(store, scope, worklog_bridge.groq_extract_part, version, budget_tokens=None)
        if parts.pending_count(store, scope, version) == 0:
            break
        try:
            groq_queue.check(groq_queue.BACKGROUND, 1000, store)
            sleep(5)
        except groq_queue.Busy as busy:
            if busy.reason == "daily":
                break
            sleep(min(65, busy.wait_seconds + 1))
    return {"judged": judged, "extracted": extracted, "pending": parts.pending_count(store, scope, version)}


def draft(store: Path, scope: str, match: str) -> str:
    version = worklog_bridge.EXTRACT_PROMPT_VERSION
    extracts = {}
    with parts.closing(parts.db.connect(store)) as c:
        for row in c.execute("SELECT content_hash, items_json, dropped FROM part_extracts WHERE prompt_version = ?", (version,)):
            extracts[row["content_hash"]] = (json.loads(row["items_json"]), row["dropped"])
    lines = [f"# 추출 초안 ({scope}, {version})", "",
             "항목을 고치거나 지우거나 더해서 정답지로 쓴다. 빠진 항목은 `- [추가] 종류: 제목`으로 적는다.", ""]
    rows = [p for p in parts.list_parts(store, scope) if match in p["path"]]
    for part in sorted(rows, key=lambda p: (p["path"].split(" > ")[:-1], p["position"])):
        lines.append(f"## {part['path']}")
        lines.append(f"종류: {part['kind']}" + (f" · {part['summary']}" if part.get("summary") else ""))
        if part["kind"] != "meeting":
            lines.append("")
            continue
        items, dropped = extracts.get(parts.extract_hash(part), (None, 0))
        if items is None:
            lines += ["(아직 추출 전)", ""]
            continue
        for item in items:
            extra = " · ".join(filter(None, [item.get("owner"), item.get("due")]))
            lines.append(f"- {KIND_LABELS[item['kind']]}: {item['title']}" + (f" ({extra})" if extra else ""))
            lines.append(f"  - 근거: \"{item['quote']}\"")
        if not items:
            lines.append("(뽑힌 항목 없음)")
        if dropped:
            lines.append(f"(원문에 없는 인용이라 버린 항목 {dropped}개)")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="회의 메모를 추출하고 사람이 고칠 초안을 만든다")
    parser.add_argument("--scope", required=True)
    parser.add_argument("--match", default="차 멘토링", help="이 글자가 경로에 있는 부분만 초안에 넣는다")
    parser.add_argument("--out", default=None, help="초안 파일(기본: devTools/extract-draft-<날짜>.md)")
    args = parser.parse_args(argv)
    store = worklog_bridge.WORKLOG_STORE
    with metrics.measure("extract-draft", args.scope) as meter:
        stats = organize(store, args.scope)
    out = Path(args.out) if args.out else DEFAULT_OUT / f"extract-draft-{time.strftime('%Y%m%d-%H%M%S')}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(draft(store, args.scope, args.match), encoding="utf-8")
    print(json.dumps(dict(stats, out=str(out), **meter.counts), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
