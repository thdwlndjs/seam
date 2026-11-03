"""Notion 페이지를 테스트용으로 녹화한다(읽기만). 결과는 커밋하지 않는 로컬 폴더에 저장한다.

    cd src/server
    ../../.venv/Scripts/python.exe ../../scripts/record_notion_fixtures.py --team <team_key> <page_id> [<page_id> ...]

페이지마다 두 파일을 만든다(기본 위치: src/server/tests/fixtures/notion_local/, .gitignore 대상).
- <id>.md: `GET /v1/pages/{id}/markdown` 응답의 markdown (Notion-Version 2026-03-11)
- <id>.blocks.json: 최상위 블록 목록(id, type, has_children, 첫 글자들만)

저장소가 공개이므로 실제 녹화본은 커밋하지 않는다. 커밋하는 fixture는 같은 구조의 가짜 내용으로 만든다
(tests/fixtures/notion/). 혹시 모를 유출을 줄이려고 토큰·키·비밀번호처럼 보이는 값은 녹화할 때도 지운다.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import httpx

SERVER = Path(__file__).resolve().parents[1] / "src" / "server"
sys.path.insert(0, str(SERVER))

MD_VERSION = "2026-03-11"
SECRET = re.compile(r"\b(secret_|ntn_|ghp_|github_pat_|gsk_|sk-|sk_|proj_|xox[abp]-)[A-Za-z0-9_\-]{6,}")
PASSWORD = re.compile(r"(비밀번호|password|pw)\s*[:：]\s*\S+", re.IGNORECASE)


def redact(text: str) -> str:
    return PASSWORD.sub(lambda m: f"{m.group(1)}: [REDACTED]", SECRET.sub("[REDACTED]", text))


def _plain(block: dict) -> str:
    body = block.get(block["type"], {}) or {}
    if block["type"] == "child_page":
        return body.get("title", "")
    return "".join(item.get("plain_text", "") for item in body.get("rich_text", []) or [])


def fetch_markdown(token: str, page_id: str) -> dict:
    resp = httpx.get(f"https://api.notion.com/v1/pages/{page_id}/markdown",
                     headers={"Authorization": f"Bearer {token}", "Notion-Version": MD_VERSION}, timeout=60)
    resp.raise_for_status()
    return resp.json()


def top_blocks(token: str, page_id: str) -> list[dict]:
    from notion_read import notion_client_for

    client = notion_client_for(token)
    blocks, cursor = [], None
    while True:
        page = client.blocks.children.list(page_id, start_cursor=cursor)
        blocks += page["results"]
        if not page.get("has_more"):
            break
        cursor = page["next_cursor"]
    return [{"id": b["id"], "type": b["type"], "has_children": b.get("has_children", False),
             "text": redact(_plain(b))[:80]} for b in blocks]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--team", required=True, help="team_configs.json의 팀 키")
    parser.add_argument("--out", default=str(SERVER / "tests" / "fixtures" / "notion_local"))
    parser.add_argument("pages", nargs="+")
    args = parser.parse_args()

    token = json.loads((SERVER.parent / "team_configs.json").read_text(encoding="utf-8"))[args.team]["notion_token"]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for page_id in args.pages:
        md = fetch_markdown(token, page_id)
        blocks = top_blocks(token, page_id)
        (out / f"{page_id}.md").write_text(redact(md.get("markdown", "")), encoding="utf-8")
        (out / f"{page_id}.blocks.json").write_text(json.dumps(blocks, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"{page_id}: md {len(md.get('markdown', ''))}자, truncated={md.get('truncated')}, "
              f"unknown={len(md.get('unknown_block_ids') or [])}, 최상위 블록 {len(blocks)}개")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
