"""판단 지침(Markdown) 로더. 문서: docs/implementation/v2.1/plan.md 2.4, Phase 1.

지침은 "판단 방법"이고, 프로젝트 지식(DB·색인)은 "판단 대상"이다. Groq API는 파일을 읽지 않으므로 서버가 이 모듈로
지침을 읽어 시스템 프롬프트 하나로 조합한다.

파일 형식 (`src/server/prompts/<이름>.md`):

    ---
    version: part-extract-3          # 판의 이름(선택)
    body_sha256: <본문 해시>           # 이 판의 본문 해시(선택): 본문이 같으면 version 그대로, 고치면 version+해시 앞 8자
    ---
    지침 본문 …
    @include common/evidence.md      # 한 줄 전체가 이 지시이면 그 파일 본문으로 바뀐다

- 판(version)은 조합한 최종 본문의 해시로 정한다. 지침 파일을 고치면 판이 바뀌어 해석 캐시(`part_extracts`)가 자연히 무효가 된다.
- `body_sha256`은 기존 프롬프트를 파일로 옮길 때 판 이름을 그대로 두려는 장치다(옮기기만 하고 내용을 안 바꾸면 캐시 유지).
- 파일이 바뀌면(mtime·크기) 다시 읽는다. 포함(@include)한 파일의 변경도 감지한다.
"""

from __future__ import annotations

import hashlib
import re
import threading
from dataclasses import dataclass
from pathlib import Path

PROMPT_DIR = Path(__file__).resolve().parent / "prompts"
MAX_INCLUDE_DEPTH = 4
_INCLUDE_RE = re.compile(r"(?m)^@include[ \t]+(\S+)[ \t]*$")
_FRONT_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)


class PromptError(Exception):
    pass


@dataclass(frozen=True)
class Prompt:
    name: str
    text: str     # 모델에 보낼 시스템 프롬프트(front matter 제거, @include 풀림)
    version: str  # 해석 캐시 키에 쓰는 판
    sha256: str   # text의 해시


_cache: dict[tuple[str, str], tuple[tuple, Prompt]] = {}
_lock = threading.Lock()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _path_of(name: str) -> Path:
    """이름("interpret", "common/evidence", 또는 "common/evidence.md") → 지침 폴더 안의 파일. 폴더 밖은 거부한다."""
    base = PROMPT_DIR.resolve()
    relative = name if name.endswith(".md") else f"{name}.md"
    path = (base / relative).resolve()
    if base != path and base not in path.parents:
        raise PromptError(f"지침 폴더 밖 경로: {name}")
    if not path.is_file():
        raise PromptError(f"지침 파일이 없어요: {relative}")
    return path


def _split_front(raw: str) -> tuple[dict[str, str], str]:
    raw = raw.lstrip("﻿").replace("\r\n", "\n")
    found = _FRONT_RE.match(raw)
    if not found:
        return {}, raw
    meta: dict[str, str] = {}
    for line in found.group(1).split("\n"):
        if ":" in line and not line.lstrip().startswith("#"):
            key, _, value = line.partition(":")
            meta[key.strip()] = value.split(" #")[0].strip()
    return meta, raw[found.end():]


def _expand(path: Path, depth: int, stack: tuple[Path, ...], files: list[Path]) -> tuple[dict[str, str], str]:
    if depth > MAX_INCLUDE_DEPTH:
        raise PromptError(f"@include가 너무 깊어요: {path.name}")
    if path in stack:
        raise PromptError(f"@include가 돌고 있어요: {' → '.join(p.name for p in (*stack, path))}")
    files.append(path)
    meta, body = _split_front(path.read_text(encoding="utf-8"))

    def include(found: re.Match) -> str:
        _, included = _expand(_path_of(found.group(1)), depth + 1, (*stack, path), files)
        return included

    return meta, _INCLUDE_RE.sub(include, body.strip("\n")).strip("\n")


def _signature(files: list[Path]) -> tuple:
    out = []
    for path in files:
        stat = path.stat()
        out.append((str(path), stat.st_mtime_ns, stat.st_size))
    return tuple(out)


def load(name: str) -> Prompt:
    """지침 하나를 읽어 조합한다. 파일이 그대로면 캐시를 돌려준다."""
    key = (str(PROMPT_DIR), name)
    with _lock:
        cached = _cache.get(key)
        if cached is not None:
            try:
                if _signature([Path(p) for p, _, _ in cached[0]]) == cached[0]:
                    return cached[1]
            except OSError:
                pass  # 파일이 사라졌으면 아래에서 다시 읽다가 오류가 난다
        files: list[Path] = []
        meta, text = _expand(_path_of(name), 0, (), files)
        digest = sha256_text(text)
        base = meta.get("version") or name.replace("/", "-")
        version = base if meta.get("body_sha256") == digest else f"{base}+{digest[:8]}"
        prompt = Prompt(name=name, text=text, version=version, sha256=digest)
        _cache[key] = (_signature(files), prompt)
        return prompt


def clear_cache() -> None:
    with _lock:
        _cache.clear()
