"""작업 기록 조회 명령. server 폴더에서 실행 (회의록 반영·되돌리기는 봇 4번 메뉴에서 한다):

python -m worklog impact  <결정ID>
python -m worklog trace   <Task·결정·후보 ID>
python -m worklog map     --scope team:<팀ID> [--json]   (섹션별 역할 보기)
python -m worklog map-set --scope team:<팀ID> "<섹션 이름>" <진행|회의|보관|제외> --actor <이름>   (역할 바꾸기)
python -m worklog extract-eval --scope team:<팀ID> [--prompt-version ...]   (정답지와 추출 비교)
python -m worklog reindex-fts [--scope team:<팀ID>]   (지식 검색 색인을 항목에서 다시 만든다. 색인은 파생 데이터)
python -m worklog search --scope team:<팀ID> "<질문>" [--kind decision ...] [--limit 8] [--max-tokens 3000]   (색인 검색)

저장소 위치: --store > WORKLOG_STORE > server/../worklog.sqlite3
결과는 JSON으로 출력한다. 종료 코드: 0 성공, 2 입력 오류, 3 충돌(이미 처리됨·오래된 후보), 4 없음.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from contextlib import closing

from . import apply, core, db, search, sections
from .db import ConflictError, NotFoundError, WorklogError

DEFAULT_STORE = Path(__file__).resolve().parents[2] / "worklog.sqlite3"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m worklog")
    parser.add_argument("--store", help="SQLite 저장소 경로")
    sub = parser.add_subparsers(dest="command", required=True)
    impact = sub.add_parser("impact", help="결정이 영향을 주는 Task")
    impact.add_argument("decision_id")
    trace = sub.add_parser("trace", help="이력과 원문 근거")
    trace.add_argument("object_id")
    map_show = sub.add_parser("map", help="섹션별 역할 보기")
    map_show.add_argument("--scope", required=True)
    map_show.add_argument("--json", action="store_true", help="표 대신 JSON (판정 근거 포함)")
    map_set = sub.add_parser("map-set", help="섹션 역할 바꾸기")
    map_set.add_argument("section", help="섹션 이름, 경로 끝부분(예: '진행상황 > 김팀원'), 또는 ID")
    map_set.add_argument("role", help="진행 | 회의 | 보관 | 제외 (progress|meeting|reference|ignore)")
    map_set.add_argument("--scope", required=True)
    map_set.add_argument("--actor", required=True)
    evaluate = sub.add_parser("extract-eval", help="정답지(봇에서 고친 목록)와 지금 추출 비교")
    evaluate.add_argument("--scope", required=True)
    evaluate.add_argument("--prompt-version", help="비교할 추출 버전 (기본: 가장 최근에 쓴 버전)")
    reindex = sub.add_parser("reindex-fts", help="지식 검색 색인 다시 만들기")
    reindex.add_argument("--scope", help="이 팀만(기본: 전체)")
    find = sub.add_parser("search", help="지식 검색(색인)")
    find.add_argument("text", help="질문 또는 검색어")
    find.add_argument("--scope", required=True)
    find.add_argument("--kind", action="append", help="종류 제한(여러 번 가능)")
    find.add_argument("--limit", type=int, default=search.DEFAULT_LIMIT)
    find.add_argument("--max-tokens", type=int, default=search.DEFAULT_MAX_TOKENS)
    find.add_argument("--json", action="store_true", help="Markdown 대신 JSON")
    return parser


def _latest_prompt_version(store: Path) -> str | None:
    db.initialize(store)
    with closing(db.connect(store)) as c:
        row = c.execute("SELECT prompt_version FROM part_extracts ORDER BY created_at DESC LIMIT 1").fetchone()
    return row["prompt_version"] if row else None


def _render_map(scope: str, rows: list[dict]) -> str:
    if not rows:
        return f"{scope}: 아직 스캔한 섹션이 없다 (Notion 수집이 한 번 돌아야 생긴다)"
    lines = [f"{scope} 섹션 역할 ({len(rows)}개)", ""]
    for r in rows:
        lines.append(f"  [{r['role_name']}] {r['section']}  · {r['decided_by']}")
    lines += ["", f'바꾸기: python -m worklog map-set --scope {scope} "<섹션 이름>" <진행|회의|보관|제외> --actor <이름>']
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = Path(args.store or os.environ.get("WORKLOG_STORE") or DEFAULT_STORE)
    try:
        if args.command == "extract-eval":
            version = args.prompt_version or _latest_prompt_version(store)
            data = {"prompt_version": version, **apply.extract_eval(store, args.scope, version or "")}
        elif args.command == "reindex-fts":
            data = {"scope": args.scope, "indexed": search.rebuild(store, args.scope)}
        elif args.command == "search":
            result = search.query(store, args.scope, args.text, kinds=args.kind, limit=args.limit, max_tokens=args.max_tokens)
            if not args.json:
                print(result["markdown"] or f"(결과 없음) 조회 범위: {json.dumps(result['searched'], ensure_ascii=False)}")
                return 0
            data = result
        elif args.command == "map":
            rows = sections.role_table(store, args.scope)
            if not args.json:
                print(_render_map(args.scope, rows))
                return 0
            data = {"scope": args.scope, "sections": rows}
        elif args.command == "map-set":
            section = sections.resolve(store, args.scope, args.section)
            role = sections.role_of(args.role)
            before = section["role"]
            result = sections.set_role(store, section["id"], role, args.actor)
            names = sections.ROLE_NAMES
            print(f"{section['path']}: {names[before]} → {names[role]}")
            if result["inherited_by"]:
                print(f"  하위 섹션 {len(result['inherited_by'])}개도 같이 바뀜")
            if result["released_records"]:
                print(f"  보관해 둔 원문 {result['released_records']}건을 분석 대기로 돌림")
            return 0
        elif args.command == "impact":
            data = core.impacted_tasks(store, args.decision_id)
        else:
            data = core.trace(store, args.object_id)
    except ConflictError as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 3
    except NotFoundError as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 4
    except WorklogError as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
