"""해부도 사이트 만들기: anatomy.json(사람이 쓴 설명) + 현재 코드 → _site/index.html

코드에서 매번 다시 채우는 것
- 부품마다 연결된 함수의 실제 코드 조각과 GitHub 줄 링크
- 설명에 적힌 함수가 코드에서 사라졌으면 경고
- 어느 부품에도 연결 안 된 함수 목록 (설명을 보강할 곳)
- 함수 호출 관계로 이어지는 부품 사이 연결선

사용법 (저장소 루트에서):
    python anatomy/build.py                # → _site/index.html
    python anatomy/build.py --strict       # 사라진 함수가 있으면 실패 (PR 확인용)

표준 라이브러리만 쓴다.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKIP_DIRS = {".git", ".github", "__pycache__", ".venv", "venv", "env", "node_modules", "_site", "anatomy", "tests", "test"}
SNIPPET_LINES = 14


# ---------------------------------------------------------------- 코드 읽기

def scan_code(root: Path) -> dict:
    """모든 .py 파일의 최상위 함수·클래스: 위치, 첫 줄 설명, 그 안에서 부르는 이름들."""
    symbols: list[dict] = []
    sources: dict[str, list[str]] = {}
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        if any(part in SKIP_DIRS or part.startswith(".") for part in rel.parts[:-1]):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(text)
        except SyntaxError as error:
            print(f"! {rel}: 읽을 수 없음 ({error})", file=sys.stderr)
            continue
        posix = rel.as_posix()
        sources[posix] = text.splitlines()
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            calls = set()
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call):
                    fn = inner.func
                    if isinstance(fn, ast.Name):
                        calls.add(fn.id)
                    elif isinstance(fn, ast.Attribute):
                        calls.add(fn.attr)
                elif isinstance(inner, ast.Name) and isinstance(inner.ctx, ast.Load):
                    calls.add(inner.id)  # 콜백으로 넘기는 함수 이름도 연결로 본다
            start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
            doc = ast.get_docstring(node) or ""
            symbols.append({"path": posix, "name": node.name, "start": start, "def": node.lineno, "end": node.end_lineno,
                            "doc": doc.strip().splitlines()[0] if doc.strip() else "",
                            "kind": "class" if isinstance(node, ast.ClassDef) else "function", "calls": sorted(calls)})
    return {"symbols": symbols, "sources": sources}


def resolve(ref: str, symbols: list[dict]) -> dict | None:
    """'server/main.py::handle_turn' → 그 함수. 경로는 끝부분이 같으면 된다(src/server/main.py도 찾음)."""
    path, _, name = ref.partition("::")
    found = [s for s in symbols if s["name"] == name and (s["path"] == path or s["path"].endswith("/" + path))]
    return found[0] if len(found) == 1 else (sorted(found, key=lambda s: len(s["path"]))[0] if found else None)


# ---------------------------------------------------------------- GitHub 정보

def git_info(root: Path) -> dict:
    def git(*args: str) -> str:
        try:
            return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=10).stdout.strip()
        except Exception:
            return ""

    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not repo:
        remote = git("config", "--get", "remote.origin.url")
        match = re.search(r"github\.com[:/](.+?)(?:\.git)?$", remote)
        repo = match.group(1) if match else ""
    sha = os.environ.get("GITHUB_SHA") or git("rev-parse", "HEAD")
    prefix = ""
    top = git("rev-parse", "--show-toplevel")
    if top:  # 코드 폴더가 저장소의 하위 폴더일 때 링크 경로를 맞춘다
        try:
            prefix = root.resolve().relative_to(Path(top).resolve()).as_posix()
        except ValueError:
            prefix = ""
        prefix = "" if prefix == "." else prefix
    message = git("log", "-1", "--pretty=%s")
    return {"repo": repo, "sha": sha, "short": sha[:7], "prefix": prefix, "message": message,
            "repo_url": f"https://github.com/{repo}" if repo else ""}


# ---------------------------------------------------------------- 합치기

def build(data: dict, code: dict, git: dict) -> dict:
    symbols, sources = code["symbols"], code["sources"]
    owner: dict[tuple[str, str], str] = {}
    missing_total = 0
    for node in data["nodes"]:
        refs = []
        for ref in node.get("refs", []):
            sym = resolve(ref, symbols)
            if sym is None:
                refs.append({"ref": ref, "label": ref.replace("::", " · "), "missing": True})
                missing_total += 1
                continue
            owner.setdefault((sym["path"], sym["name"]), node["id"])
            url = ""
            if git["repo"] and git["sha"]:
                path = f"{git['prefix']}/{sym['path']}" if git["prefix"] else sym["path"]
                url = f"{git['repo_url']}/blob/{git['sha']}/{path}#L{sym['start']}-L{sym['end']}"
            refs.append({"ref": ref, "label": f"{sym['path']} · {sym['name']}{'()' if sym['kind'] == 'function' else ''}",
                         "line": sym["def"], "url": url, "missing": False, "doc": sym["doc"]})
        node["code_refs"] = refs
        node["missing"] = [r["ref"] for r in refs if r["missing"]]
        first = next((r for r in refs if not r["missing"]), None)
        node["code"] = first["label"] if first else (refs[0]["label"] if refs else "")
        if first:
            sym = resolve(first["ref"], symbols)
            lines = sources[sym["path"]][sym["start"] - 1: sym["end"]]
            node["snippet"] = "\n".join(lines[:SNIPPET_LINES]) + ("\n    …" if len(lines) > SNIPPET_LINES else "")
        else:
            node["snippet"] = ""

    # 함수 호출 관계 → 부품 사이 연결선
    by_name: dict[str, list[dict]] = {}
    for sym in symbols:
        by_name.setdefault(sym["name"], []).append(sym)
    code_edges: set[tuple[str, str]] = set()
    for sym in symbols:
        a = owner.get((sym["path"], sym["name"]))
        if not a:
            continue
        for called in sym["calls"]:
            targets = by_name.get(called, [])
            same_file = [t for t in targets if t["path"] == sym["path"]]
            for target in same_file or (targets if len(targets) == 1 else []):
                b = owner.get((target["path"], target["name"]))
                if b and b != a:
                    code_edges.add(tuple(sorted((a, b))))
    data["code_edges"] = sorted(code_edges)

    # 어느 부품에도 연결 안 된 함수
    unmapped: dict[str, list[str]] = {}
    for sym in symbols:
        if (sym["path"], sym["name"]) not in owner:
            unmapped.setdefault(sym["path"], []).append(sym["name"])
    data["meta"] = {
        **git, "built_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "missing": missing_total, "symbols": len(symbols), "mapped": len(owner),
        "unmapped": [{"path": p, "names": n} for p, n in sorted(unmapped.items())],
    }
    return data


def render(data: dict, template: str) -> str:
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    marker = "<!--ANATOMY_DATA-->"
    if marker not in template:
        raise SystemExit("template.html에 <!--ANATOMY_DATA--> 자리가 없다")
    return template.replace(marker, f'<script id="anatomy-data" type="application/json">{payload}</script>')


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--src", default=".", help="코드 폴더 (기본: 현재 폴더)")
    parser.add_argument("--data", default=str(HERE / "anatomy.json"))
    parser.add_argument("--template", default=str(HERE / "template.html"))
    parser.add_argument("--out", default="_site")
    parser.add_argument("--strict", action="store_true", help="사라진 함수가 있으면 실패")
    args = parser.parse_args(argv)

    root = Path(args.src).resolve()
    data = json.loads(Path(args.data).read_text(encoding="utf-8"))
    built = build(data, scan_code(root), git_info(root))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(render(built, Path(args.template).read_text(encoding="utf-8")), encoding="utf-8")
    (out / "anatomy.json").write_text(json.dumps(built, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / ".nojekyll").write_text("", encoding="utf-8")

    meta = built["meta"]
    print(f"✓ {out / 'index.html'}  (함수 {meta['symbols']}개 중 {meta['mapped']}개 연결, 코드 연결선 {len(built['code_edges'])}개)")
    for node in built["nodes"]:
        for ref in node["missing"]:
            print(f"! [{node['label']}] 코드에서 찾을 수 없음: {ref}")
    if args.strict and meta["missing"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
