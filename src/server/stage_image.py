"""상위 단계 표시줄 PNG: 문제 정의 / VOC / 개발 세 단계 중 지금 어디인지 그려 Adaptive Card의 Image로 보인다.
문서: docs/v1.1/spec.md P2-4a.

- 투명 배경, 2배 해상도(논리 너비 520px → 1040px), 라이트·다크 두 벌. 카드는 themedUrls로 테마에 맞는 쪽을 쓴다.
- (현재 상위 단계, 테마, 디자인 판)이 같으면 항상 같은 그림이다. 주소에 상위 단계 번호·판을 넣고 오래 캐시한다:
  단계가 바뀌면 주소가 달라져 Teams가 새로 받고, 디자인을 고치면 DESIGN_VERSION을 올려 새 주소로 바꾼다.
- 그림에는 질문 글과 버튼을 넣지 않는다. 세부 단계(2-3 …)는 카드의 질문 문장에만 쓴다.
- 주소에는 민감한 값이 없다(상위 단계 번호·테마뿐). 입력은 허용 목록으로만 받는다.
"""

from __future__ import annotations

import io
import os
import urllib.parse
from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI, HTTPException, Response
from PIL import Image, ImageDraw, ImageFont

from worklog import stages

DESIGN_VERSION = "3"
WIDTH, HEIGHT, SCALE, SUPER = 520, 84, 2, 4  # 논리 크기, 출력 배율, 부드럽게 그리려고 더 크게 그렸다 줄이는 배율
THEMES = {
    "light": {"accent": (74, 76, 184), "halo": (74, 76, 184, 56), "todo": (150, 153, 170), "label": (52, 56, 64)},
    "dark": {"accent": (139, 143, 232), "halo": (139, 143, 232, 70), "todo": (120, 120, 140), "label": (222, 224, 230)},
}
FONT_CANDIDATES = [
    os.environ.get("BOARD_FONT", ""),
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc", "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "C:/Windows/Fonts/malgun.ttf", "C:/Windows/Fonts/NotoSansKR-VF.ttf", "/System/Library/Fonts/AppleSDGothicNeo.ttc",
]
BOLD_CANDIDATES = [
    os.environ.get("BOARD_FONT_BOLD", ""),
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc", "C:/Windows/Fonts/malgunbd.ttf",
]


class FontMissing(RuntimeError):
    pass


def _font(candidates: list[str], size: int) -> ImageFont.FreeTypeFont:
    for path in candidates:
        if path and Path(path).exists():
            return ImageFont.truetype(path, size)
    raise FontMissing("한글 글꼴을 찾지 못했어요(Ubuntu는 fonts-noto-cjk 설치, 또는 BOARD_FONT 환경 변수)")


@lru_cache(maxsize=16)
def render(phase: str, theme: str) -> bytes:
    """현재 상위 단계가 phase("1"·"2"·"3")인 표시줄 PNG(투명 배경). 지나온·앞 단계는 번호 원, 현재는 강조."""
    if phase not in {no for no, _, _ in stages.PHASES} or theme not in THEMES:
        raise ValueError("알 수 없는 단계·테마")
    colors, k = THEMES[theme], SCALE * SUPER
    steps = stages.phases(stages.PHASES[int(phase) - 1][2][0])
    image = Image.new("RGBA", (WIDTH * k, HEIGHT * k), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    regular, bold = _font(FONT_CANDIDATES, 15 * k), _font(BOLD_CANDIDATES or FONT_CANDIDATES, 15 * k)
    number = _font(BOLD_CANDIDATES or FONT_CANDIDATES, 15 * k)
    cell, cy = WIDTH / len(steps), 22
    for n, step in enumerate(steps):
        cx, state = cell * (n + 0.5), step["state"]
        if n:  # 앞 칸과 잇는 선: 지나온 칸에서 현재 칸까지는 강조색, 나머지는 연한 색
            reached = state != "todo"
            x0 = cell * (n - 0.5) + 17
            draw.line([(x0 * k, cy * k), ((cx - 17) * k, cy * k)], fill=colors["accent" if reached else "todo"], width=2 * k)
        if state == "current":  # 연한 후광 + 강조색 테두리
            draw.ellipse([(cx - 19) * k, (cy - 19) * k, (cx + 19) * k, (cy + 19) * k], fill=colors["halo"])
        ring = colors["accent"] if state != "todo" else colors["todo"]
        draw.ellipse([(cx - 15) * k, (cy - 15) * k, (cx + 15) * k, (cy + 15) * k], outline=ring, width=2 * k,
                     fill=None if state == "current" else (0, 0, 0, 0))
        text = step["no"]
        draw.text((cx * k, cy * k), text, font=number, anchor="mm",
                  fill=colors["accent"] if state != "todo" else colors["label"])
        font = bold if state == "current" else regular
        draw.text((cx * k, (cy + 38) * k), step["name"], font=font, anchor="mm",
                  fill=colors["accent"] if state == "current" else colors["label"])
    out = image.resize((WIDTH * SCALE, HEIGHT * SCALE), Image.LANCZOS)
    buffer = io.BytesIO()
    out.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def base_url() -> str:
    return os.environ.get("BOARD_PUBLIC_URL", "http://localhost:3978").rstrip("/")


def url(stage: str, theme: str) -> str:
    """카드 Image가 쓰는 주소. 세부 단계가 속한 상위 단계·테마·디자인 판이 같으면 같은 주소(캐시 키)."""
    query = urllib.parse.urlencode({"s": stages.phase_of(stage), "t": theme, "v": DESIGN_VERSION})
    return f"{base_url()}/img/stage.png?{query}"


def register(app: FastAPI) -> None:
    @app.get("/img/stage.png")
    def stage_png(s: str, t: str = "light", v: str = DESIGN_VERSION) -> Response:
        if s not in {no for no, _, _ in stages.PHASES} or t not in THEMES:
            raise HTTPException(status_code=400, detail="알 수 없는 단계·테마")
        try:
            data = render(s, t)
        except FontMissing as error:
            raise HTTPException(status_code=503, detail=str(error)) from None
        # 주소(단계·테마·판)가 같으면 그림도 같다 → 길게 캐시. 바뀌는 것은 주소가 바뀐다.
        return Response(data, media_type="image/png", headers={"Cache-Control": "public, max-age=31536000, immutable"})
