"""변경 후보 검토 명령. server 폴더에서 실행:

python -m worklog candidates [--scope team:<팀ID>] [--status pending|approved|rejected|recorded|all]
python -m worklog approve <후보ID> --actor <승인자> [--note ...]
python -m worklog reject  <후보ID> --actor <승인자> --note <사유>
python -m worklog impact  <결정ID>
python -m worklog trace   <Task·결정·후보 ID>
python -m worklog map     --scope team:<팀ID> [--json]   (섹션별 역할 보기)
python -m worklog map-set --scope team:<팀ID> "<섹션 이름>" <진행|회의|보관|제외> --actor <이름>   (역할 바꾸기)

저장소 위치: --store > WORKLOG_STORE > server/../worklog.sqlite3
결과는 JSON으로 출력한다. 종료 코드: 0 성공, 2 입력 오류, 3 충돌(이미 처리됨·오래된 후보), 4 없음.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import core, sections
from .db import ConflictError, NotFoundError, WorklogError

DEFAULT_STORE = Path(__file__).resolve().parents[2] / "worklog.sqlite3"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m worklog")
    parser.add_argument("--store", help="SQLite 저장소 경로")
    sub = parser.add_subparsers(dest="command", required=True)
    listing = sub.add_parser("candidates", help="변경 후보와 근거 보기")
    listing.add_argument("--scope")
    listing.add_argument("--status", default="pending")
    approve = sub.add_parser("approve", help="후보 하나를 반영")
    approve.add_argument("candidate_id")
    approve.add_argument("--actor", required=True, help="승인자 (책임자로 기록됨)")
    approve.add_argument("--note", default="")
    reject = sub.add_parser("reject", help="아무것도 바꾸지 않고 후보 닫기")
    reject.add_argument("candidate_id")
    reject.add_argument("--actor", required=True)
    reject.add_argument("--note", required=True)
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
    return parser


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
        if args.command == "candidates":
            data = core.list_candidates(store, args.scope, None if args.status == "all" else args.status)
        elif args.command == "approve":
            data = core.approve(store, args.candidate_id, args.actor, args.note)
        elif args.command == "reject":
            data = core.reject(store, args.candidate_id, args.actor, args.note)
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
