"""검색어 추출. build-log(buildlog/search.py)의 query_terms를 가져왔다."""
from __future__ import annotations

import re
import unicodedata

# 한국어 조사를 단어 끝에서 뗀다 ("파티션을" -> "파티션").
_PARTICLES = sorted(["에서는", "으로는", "에서", "으로", "에게", "한테", "까지", "부터", "처럼", "보다", "이랑",
                     "을", "를", "이", "가", "은", "는", "의", "에", "로", "와", "과", "도", "만", "랑"], key=len, reverse=True)
_WORD_RE = re.compile(r"[\w][\w\-.:/]*[\w]|[\w]", re.UNICODE)
_HANGUL_RE = re.compile(r"[가-힣]")


def _strip_particle(word: str) -> str:
    if not _HANGUL_RE.search(word[-1:]):
        return word
    for particle in _PARTICLES:
        if word.endswith(particle) and len(word) - len(particle) >= 2:
            return word[: -len(particle)]
    return word


def query_terms(text: str) -> list[str]:
    """문장의 단어들(조사 제거, 한 글자 제외, 중복 제거)."""
    words = [w for w in (_strip_particle(w) for w in _WORD_RE.findall(unicodedata.normalize("NFKC", text or ""))) if len(w) > 1]
    terms: list[str] = []
    seen: set[str] = set()
    for term in words:
        key = term.casefold()
        if key not in seen:
            seen.add(key)
            terms.append(term)
    return terms
