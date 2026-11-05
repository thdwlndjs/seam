"""저장된 성숙도의 공통 표시 정책. 원문 재검증이나 저장값 변경은 하지 않는다."""

from __future__ import annotations

from typing import Any

MATURITY_LABELS = {"draft": "가안", "recommended": "조언", "discussed": "논의", "agreed": "합의"}
UNKNOWN_LABEL = "합의 여부 미확인"


def resolve(maturity: Any) -> dict[str, str]:
    """종류·저장 승인·작업 상태로 합의를 추론하지 않고, 유효한 저장 성숙도만 읽는다."""
    if isinstance(maturity, str) and maturity in MATURITY_LABELS:
        return {"value": maturity, "label": MATURITY_LABELS[maturity]}
    return {"value": "unknown", "label": UNKNOWN_LABEL}
