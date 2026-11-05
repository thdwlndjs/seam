"""Phase 5 실연동 확인. 서버 설정 로더와 복사본 DB를 사용하며 비밀과 원문을 출력하지 않는다."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sqlite3
import sys
from collections import Counter
from contextlib import closing
from pathlib import Path

SERVER = Path(__file__).resolve().parents[1] / "src" / "server"
sys.path.insert(0, str(SERVER))
# 서버를 띄우지 않으며 이 값은 현재 검증 프로세스 안에서만 쓴다.
os.environ.setdefault("BOT_SERVER_SECRET", "read-only-probe")
os.environ["WORKLOG_LOOP"] = "0"

import main  # noqa: E402


def snapshot_sources(snapshot: Path, scope: str | None = None) -> tuple[str, list[str]]:
    """실제 저장소를 거부하고 읽기 전용 복사본에서 단일 범위와 활성 Notion 페이지를 얻는다."""
    snapshot = snapshot.resolve(strict=True)
    if snapshot == (SERVER.parent / "worklog.sqlite3").resolve():
        raise ValueError("검증에는 실제 DB가 아닌 복사본을 지정해야 합니다")
    with closing(sqlite3.connect(snapshot.as_uri() + "?mode=ro&immutable=1", uri=True)) as c:
        scopes = [r[0] for r in c.execute("SELECT DISTINCT scope FROM source_parts WHERE origin='notion' AND status='active'")]
        if scope is None:
            if len(scopes) != 1:
                raise ValueError("범위가 하나가 아닙니다. --scope를 명시하세요")
            scope = scopes[0]
        if scope not in scopes:
            raise ValueError("복사본에 해당 범위의 활성 Notion 출처가 없습니다")
        rows = c.execute("SELECT DISTINCT page_id FROM source_parts WHERE scope=? AND origin='notion' "
                         "AND status='active' AND page_id IS NOT NULL", (scope,)).fetchall()
    return scope, [r[0] for r in rows]


def _status(error: Exception) -> str:
    """예외 본문에는 인증정보가 포함될 수 있어 HTTP 상태만 남긴다."""
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None) or getattr(error, "status", None)
    return str(status) if status is not None else type(error).__name__


async def verify(snapshot: Path, scope: str | None = None) -> dict:
    scope, pages = snapshot_sources(snapshot, scope)
    # 서버의 팀/사용자 범위 선택과 완전히 같은 로더. 다른 경로의 설정으로 대체하지 않는다.
    config = main._get_config(scope)
    if not config:
        raise ValueError("서버 설정에 복사본의 범위가 없습니다")
    if not all(config.get(k) for k in ("github_repo", "github_token", "notion_token", "notion_page_id")):
        raise ValueError("서버 설정에 필수 연결 항목이 없습니다")

    github: dict[str, str] = {}
    try:
        for name, path in (("issues", "/issues"), ("pulls", "/pulls"), ("commits", "/commits")):
            try:
                params = {"per_page": 1}
                if name != "commits":
                    params["state"] = "all"
                await main._github_get(config["github_repo"], path, config["github_token"], params)
                github[name] = "200"
            except Exception as error:
                github[name] = _status(error)
    finally:
        await main._close_github_http_client()

    client = main._notion_client_for(config)
    notion: Counter[str] = Counter()
    for page in pages:
        try:
            await asyncio.to_thread(client.pages.retrieve, page_id=page)
            notion["200"] += 1
        except Exception as error:
            notion[_status(error)] += 1
    try:
        await asyncio.to_thread(client.pages.retrieve, page_id=config["notion_page_id"])
        root = "200"
    except Exception as error:
        root = _status(error)
    return {"config_loader": "main._get_config", "github": github,
            "notion": {"root": root, "active_pages": len(pages), "page_statuses": dict(notion)}}


def main_cli() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True, help="읽기 전용으로 열 복사본 SQLite 경로")
    parser.add_argument("--scope", help="복사본에 여러 팀이 있는 경우의 정확한 범위")
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    try:
        result = asyncio.run(verify(args.snapshot, args.scope))
    except Exception as error:
        # 경로·SQL·인증정보가 예외에 들어갈 수 있으므로 예외 내용은 출력하지 않는다.
        print(json.dumps({"error_type": type(error).__name__}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    good = all(s == "200" for s in result["github"].values()) and result["notion"]["root"] == "200"
    good = good and result["notion"]["active_pages"] > 0 and result["notion"]["page_statuses"] == {"200": result["notion"]["active_pages"]}
    return 0 if good else 1


if __name__ == "__main__":
    raise SystemExit(main_cli())
