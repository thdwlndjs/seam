"""커밋마다: 직전 커밋과 이번 커밋의 .py diff를 보고, 그 diff가 anatomy.json의 어느 부품
설명에 영향을 주는지 가려낸 뒤 Claude에게만 판단을 맡긴다.

구조가 하나도 안 바뀌었으면(= 영향받은 부품이 없으면) Claude를 아예 호출하지 않는다 — 비용은
diff가 실제로 기존 설명을 건드릴 때만 든다.

쓰는 데이터:
- 입력: anatomy/anatomy.json (현재 상태 설명), git diff(이전 커밋 vs 이번 커밋)
- 출력: anatomy/anatomy.json (영향받은 노드만 패치), anatomy/history.jsonl (이번 커밋 요약 한 줄 추가)

Claude 호출은 Anthropic API가 아니라 Claude Code CLI(`claude -p ...`)를 헤드리스로 돌려서 한다.
인증은 `claude setup-token`으로 만든 CLAUDE_CODE_OAUTH_TOKEN(구독 기반) — API 키/크레딧 불필요.

깃에 커밋/푸시하는 건 이 스크립트가 아니라 워크플로우가 한다. 이 스크립트는 파일만 고치고,
뭔가 바뀌었으면 표준출력 마지막 줄에 "CHANGED"를 찍는다(워크플로우가 그걸 보고 커밋 여부를 정함).

사용법:
    python anatomy/claude_sync.py --before <parent-sha> --after <head-sha>
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from build import symbols_in_text  # noqa: E402

ZERO_SHA = "0" * 40
MODEL = os.environ.get("ANATOMY_CLAUDE_MODEL", "claude-sonnet-5")
SNIPPET_LINES = 30


def git(*args: str, root: Path) -> str:
    # -c safe.directory=*: 이 호출 한정으로만 신뢰. 영구 설정은 절대 안 건드린다.
    # encoding 명시: Windows 로캘(cp949)로 떨어지면 한글 커밋 메시지/코드에서 깨진다.
    result = subprocess.run(["git", "-c", "safe.directory=*", "-C", str(root), *args],
                             capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 실패: {result.stderr.strip()}")
    return result.stdout


def file_at_rev(rev: str, repo_path: str, repo_top: Path) -> str | None:
    """git show <rev>:<repo_path>. repo_path는 저장소 루트 기준. 그 시점에 파일이 없었으면 None."""
    result = subprocess.run(["git", "-c", "safe.directory=*", "-C", str(repo_top), "show", f"{rev}:{repo_path}"],
                             capture_output=True, text=True, encoding="utf-8", errors="replace")
    return result.stdout if result.returncode == 0 else None


def repo_prefix(root: Path, repo_top: Path) -> str:
    """--src로 준 폴더가 저장소 루트의 하위 폴더면 그 상대 경로("src" 같은 것), 루트 자체면 ""."""
    rel = root.resolve().relative_to(repo_top.resolve()).as_posix()
    return "" if rel == "." else rel


def changed_py_files(before: str, after: str, repo_top: Path, prefix: str) -> list[tuple[str, str]]:
    """(저장소 루트 기준 경로, --src 기준 경로) 쌍 목록. prefix 밖의 .py 변경은 뺀다."""
    full_paths = [p for p in git("diff", "--name-only", before, after, "--", "*.py", root=repo_top).splitlines() if p.strip()]
    out = []
    for full in full_paths:
        if prefix:
            if not (full == prefix or full.startswith(prefix + "/")):
                continue
            out.append((full, full[len(prefix) + 1:]))
        else:
            out.append((full, full))
    return out


def snippet(text: str, sym: dict) -> str:
    lines = text.splitlines()[sym["start"] - 1: sym["end"]]
    return "\n".join(lines[:SNIPPET_LINES]) + ("\n    …" if len(lines) > SNIPPET_LINES else "")


def refs_in_file(anatomy: dict, changed_path: str):
    """anatomy.json에서 이 파일을 가리키는 (node, ref, name) 들."""
    for node in anatomy["nodes"]:
        for ref in node.get("refs", []):
            ref_path, _, name = ref.partition("::")
            if changed_path == ref_path or changed_path.endswith("/" + ref_path):
                yield node, ref, name


def collect_diff(anatomy: dict, changed: list[tuple[str, str]], root: Path, repo_top: Path,
                  before: str) -> tuple[list[dict], dict[str, list[str]]]:
    """영향받은 (node, ref 상태) 목록과, refs에 안 걸린 새 함수 후보(파일별)를 모은다."""
    affected: list[dict] = []
    candidates: dict[str, list[str]] = {}
    referenced_names_by_file: dict[str, set[str]] = {}

    for full_path, path in changed:
        old_text = file_at_rev(before, full_path, repo_top) if before != ZERO_SHA else None
        new_file = root / path
        if not new_file.exists():
            continue  # 지워진 파일: 거기 걸린 refs는 build.py가 "missing"으로 알아서 잡아준다
        new_text = new_file.read_text(encoding="utf-8", errors="replace")
        old_syms = {s["name"]: s for s in symbols_in_text(old_text, path)} if old_text is not None else {}
        new_syms = {s["name"]: s for s in symbols_in_text(new_text, path)}

        file_refs = list(refs_in_file(anatomy, path))
        referenced_names_by_file.setdefault(path, set()).update(name for _, _, name in file_refs)

        for node, ref, name in file_refs:
            old_sym, new_sym = old_syms.get(name), new_syms.get(name)
            if old_sym is None and new_sym is None:
                continue
            if old_sym is not None and new_sym is None:
                affected.append({"node_id": node["id"], "ref": ref, "status": "removed",
                                  "old": snippet(old_text, old_sym), "new": None})
            elif old_sym is None and new_sym is not None:
                affected.append({"node_id": node["id"], "ref": ref, "status": "added",
                                  "old": None, "new": snippet(new_text, new_sym)})
            else:
                old_src, new_src = snippet(old_text, old_sym), snippet(new_text, new_sym)
                if old_src.strip() != new_src.strip():
                    affected.append({"node_id": node["id"], "ref": ref, "status": "changed",
                                      "old": old_src, "new": new_src})

        new_names = set(new_syms) - set(old_syms) - referenced_names_by_file[path]
        if new_names and file_refs:  # 이 파일에 이미 연결된 부품이 있을 때만 "여기 새 함수 생겼다"고 후보로 던진다
            candidates.setdefault(path, []).extend(sorted(new_names))

    return affected, candidates


def build_prompt(anatomy: dict, affected: list[dict], candidates: dict[str, list[str]]) -> dict:
    by_node: dict[str, dict] = {}
    for node in anatomy["nodes"]:
        if node["id"] in {a["node_id"] for a in affected}:
            by_node[node["id"]] = node

    node_blocks = []
    for node_id, node in by_node.items():
        diff = [a for a in affected if a["node_id"] == node_id]
        file_candidates = sorted({n for ref in node.get("refs", []) for path, names in candidates.items()
                                   if (ref.partition("::")[0] == path or path.endswith("/" + ref.partition("::")[0]))
                                   for n in names})
        node_blocks.append({
            "node_id": node_id, "label": node["label"],
            "current": {"sum": node["sum"], "ana": node.get("ana", ""), "points": node.get("points", []),
                        "refs": node.get("refs", [])},
            "diff": diff,
            "new_candidates_in_same_file": file_candidates,
        })
    return {"nodes": node_blocks}


SYSTEM_PROMPT = """당신은 소프트웨어 아키텍처 해부도(anatomy.json)를 유지보수하는 역할이에요.
각 "부품(node)"은 sum(한줄 요약)·ana(비유)·points(핵심 포인트 목록)로 코드를 설명해요.
입력으로 각 부품에 영향을 준 코드 diff(함수 단위, 추가/삭제/본문변경)를 줄게요.

판단 기준:
- diff가 기존 설명의 의미를 바꾸지 않으면(변수명 수준, 리팩터링 등) needs_update:false.
- diff가 설명이 틀리게 만들면(동작이 달라짐, 함수가 사라짐, 로직이 바뀜) needs_update:true로 하고
  sum/ana/points를 지금 코드에 맞게 다시 써요. 원래 문체(짧고 구어체, 비유 적극 사용)를 유지해요.
- new_candidates_in_same_file에 있는 함수가 이 부품 설명과 명백히 관련 있으면 points에 언급하거나
  반영해도 좋아요. 애매하면 손대지 마세요.
- 근거 없이 지어내지 말고, 준 diff에 실제로 있는 내용만 바탕으로 쓰세요.

changelog는 이번 커밋에서 "무엇이 왜 바뀌었는지"를 사람이 읽을 한국어 요약으로, 관련된 변경끼리
하나의 항목으로 묶고 무관한 변경은 별도 항목으로 나누세요(과도하게 잘게 쪼개지 마세요).

오직 이 JSON 스키마로만 답하세요(다른 텍스트 없이):
{"patches": [{"node_id": "...", "needs_update": true|false, "sum": "...", "ana": "...", "points": ["..."]}],
 "changelog": [{"summary": "...", "node_ids": ["..."]}]}
"""


def call_claude(payload: dict) -> dict:
    """Claude Code CLI를 헤드리스로 돌린다. CLAUDE_CODE_OAUTH_TOKEN(구독 토큰)으로 인증 —
    API 키/크레딧이 아니라 `claude setup-token`으로 만든 구독 기반 토큰을 써서 과금이 안 붙는다."""
    result = subprocess.run(
        ["claude", "-p", json.dumps(payload, ensure_ascii=False),
         "--output-format", "json", "--model", MODEL, "--restricted",
         "--system-prompt", SYSTEM_PROMPT],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude CLI 종료코드 {result.returncode}: {result.stderr.strip()[:500]}")
    envelope = json.loads(result.stdout)
    if envelope.get("is_error"):
        raise RuntimeError(f"claude CLI 에러: {str(envelope.get('result'))[:500]}")
    text = str(envelope.get("result", "")).strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    return json.loads(text)


def apply_patches(anatomy: dict, patches: list[dict]) -> list[str]:
    by_id = {n["id"]: n for n in anatomy["nodes"]}
    updated = []
    for patch in patches:
        if not patch.get("needs_update"):
            continue
        node = by_id.get(patch.get("node_id"))
        if node is None:
            continue
        for field in ("sum", "ana", "points"):
            if field in patch and patch[field]:
                node[field] = patch[field]
        updated.append(node["id"])
    return updated


def append_history(history_path: Path, after: str, message: str, changelog: list[dict]) -> None:
    entry = {
        "sha": after, "short": after[:7],
        "date": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "message": message.splitlines()[0] if message else "",
        "entries": [{"summary": c.get("summary", ""), "nodes": c.get("node_ids", [])} for c in changelog],
    }
    with history_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--before", required=True)
    parser.add_argument("--after", required=True)
    parser.add_argument("--src", default=".")
    parser.add_argument("--data", default=str(HERE / "anatomy.json"))
    parser.add_argument("--history", default=str(HERE / "history.jsonl"))
    args = parser.parse_args(argv)

    root = Path(args.src).resolve()
    data_path = Path(args.data)

    if args.before == ZERO_SHA:
        print("새로 생긴 브랜치라 비교할 이전 커밋이 없음 — 건너뜀")
        return 0

    repo_top = Path(git("rev-parse", "--show-toplevel", root=root).strip())
    prefix = repo_prefix(root, repo_top)
    changed = changed_py_files(args.before, args.after, repo_top, prefix)
    if not changed:
        print("이번 push에 .py 변경 없음 — 건너뜀")
        return 0

    anatomy = json.loads(data_path.read_text(encoding="utf-8"))
    affected, candidates = collect_diff(anatomy, changed, root, repo_top, args.before)
    if not affected and not candidates:
        print(f".py {len(changed)}개 바뀌었지만 anatomy.json의 어느 부품과도 안 겹침 — Claude 호출 안 함")
        return 0

    payload = build_prompt(anatomy, affected, candidates)
    if not payload["nodes"]:
        print("영향 범위가 refs 없는 파일뿐 — 건너뜀")
        return 0

    print(f"영향받은 부품 {len(payload['nodes'])}개 — Claude({MODEL}) 호출")
    try:
        result = call_claude(payload)
    except Exception as error:  # CLI 미설치, 토큰 없음/만료, 레이트리밋, 파싱 실패 등 — 빌드는 막지 않되 눈에 띄게 알림
        message = f"Claude 호출 실패, 이번 커밋은 설명 갱신 없이 넘어감: {error}"
        print(f"::warning title=anatomy claude-sync 실패::{message}")  # GitHub Actions 요약에 노란 경고로 뜸
        print(f"! {message}", file=sys.stderr)
        return 0

    updated = apply_patches(anatomy, result.get("patches", []))
    changelog = result.get("changelog", [])
    if not updated and not changelog:
        print("Claude 판단: 갱신 필요 없음")
        return 0

    if updated:
        data_path.write_text(json.dumps(anatomy, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"anatomy.json 패치: {', '.join(updated)}")

    if changelog:
        commit_message = git("log", "-1", "--pretty=%s", args.after, root=root).strip()
        append_history(Path(args.history), args.after, commit_message, changelog)
        print(f"history.jsonl에 {len(changelog)}개 항목 추가")

    print("CHANGED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
