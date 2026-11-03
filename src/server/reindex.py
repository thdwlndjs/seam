"""팀 하나를 처음부터 다시 수집한다(md·부분으로). 문서: docs/v1.1/spec.md P1-10.

    cd src/server
    ../../.venv/Scripts/python.exe reindex.py --scope <team_key>

1. 저장소를 `worklog.sqlite3.bak-YYYYMMDD-HHMMSS`로 복사한다.
2. 그 팀의 페이지·행 수정 시각을 지워, 모든 페이지를 다시 스캔하고 모든 본문을 md로 다시 받게 한다.
   예전 원문(sources)은 지우지 않는다. 내용이 달라진 것만 새 판으로 쌓인다.
3. "판정 못 함"(LLM 없음·실패)으로 굳은 섹션을 다시 판정하고, 부분의 종류를 판정한다.

Notion은 읽기만 한다. 팀 설정은 src/team_configs.json에서 읽는다(토큰은 출력하지 않는다).
worklog 패키지는 Notion·LLM에 의존하지 않으므로 이 명령은 서버 폴더에 둔다.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv(os.environ.get("ENV_FILE"))  # GROQ_API_KEY를 worklog_bridge보다 먼저 읽는다

import metrics  # noqa: E402
import notion_map  # noqa: E402
import worklog_bridge  # noqa: E402
from notion_read import notion_client_for  # noqa: E402
from worklog import db as wdb  # noqa: E402

TEAM_CONFIG_PATH = Path(__file__).resolve().parent.parent / "team_configs.json"


def backup(store: Path) -> Path | None:
    if not store.exists():
        return None
    target = store.with_name(f"{store.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    with closing(wdb.connect(store)) as source, closing(wdb.connect(target)) as copy:
        source.backup(copy)  # WAL 중이어도 일관된 복사본
    return target


def forget_edit_times(store: Path, scope: str) -> None:
    """수정 시각·구조 지문을 지워 다음 동기화가 전부 다시 읽게 한다. 원문·역할·부분은 그대로 둔다."""
    wdb.initialize(store)
    with wdb.write(store) as c:
        c.execute("UPDATE source_pages SET last_edited = NULL, fingerprint = NULL WHERE scope = ?", (scope,))
        c.execute("""UPDATE source_units SET last_edited = '' WHERE section_id IN
                     (SELECT id FROM source_sections WHERE scope = ?)""", (scope,))


def reindex(store: Path, scope: str, roots: list[str], client: Any, classify: Any = None,
            classify_parts: Any = None) -> dict[str, Any]:
    made = backup(store)
    forget_edit_times(store, scope)
    stats = notion_map.sync_roots(store, scope, roots, client, classify=classify, classify_parts=classify_parts,
                                  rejudge_unknown=True)
    stats["backup"] = str(made) if made else None
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="팀 하나를 md·부분으로 다시 수집한다")
    parser.add_argument("--scope", required=True, help="team_configs.json의 팀 키")
    args = parser.parse_args(argv)
    configs = json.loads(TEAM_CONFIG_PATH.read_text(encoding="utf-8"))
    config = configs.get(args.scope)
    if config is None:
        print(f"팀 {args.scope} 설정이 없어요", file=sys.stderr)
        return 2
    roots = [r for r in (config.get("notion_page_id"), config.get("meeting_notes_page_id")) if r]
    with metrics.measure("reindex", args.scope) as meter:
        stats = reindex(worklog_bridge.WORKLOG_STORE, args.scope, roots, notion_client_for(config["notion_token"]),
                        classify=worklog_bridge.groq_classify_sections, classify_parts=worklog_bridge.groq_classify_parts)
    summary = {k: stats[k] for k in ("backup", "pages_checked", "judged", "records_new", "parts_judged", "roots")}
    summary.update(meter.counts)
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
