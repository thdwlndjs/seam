"""단계 표시줄 그림 테스트: 투명 배경·크기·테마, 주소·캐시, 잘못된 입력, 글꼴 없음. 문서: docs/v1.1/spec.md P2-4."""

from __future__ import annotations

import isolation  # noqa: F401,I001  실제 저장소·키를 쓰지 않게(맨 먼저)

import io
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

import main  # noqa: E402
import stage_image  # noqa: E402
from worklog import stages  # noqa: E402


def png(stage: str, theme: str) -> Image.Image:
    return Image.open(io.BytesIO(stage_image.render(stage, theme)))


class RenderTests(unittest.TestCase):
    def test_transparent_high_resolution_and_card_width(self) -> None:
        image = png("2", "light")
        self.assertEqual(image.format, "PNG")
        self.assertEqual(image.mode, "RGBA")
        self.assertEqual(image.size, (stage_image.WIDTH * 2, stage_image.HEIGHT * 2))  # 카드 너비(520) 2배
        self.assertEqual(image.getpixel((0, image.height - 1))[3], 0)  # 바깥은 투명

    def test_phase_and_theme_change_the_picture(self) -> None:
        self.assertNotEqual(stage_image.render("2", "light"), stage_image.render("1", "light"))
        self.assertNotEqual(stage_image.render("2", "light"), stage_image.render("2", "dark"))
        self.assertEqual(stage_image.render("2", "dark"), stage_image.render("2", "dark"))  # 같은 입력은 같은 그림
        for phase, _, _ in stages.PHASES:
            self.assertEqual(png(phase, "dark").size, (1040, stage_image.HEIGHT * 2))

    def test_current_phase_has_a_halo_and_the_others_do_not(self) -> None:
        image = png("2", "light")
        cell = image.width / len(stages.PHASES)
        halo = lambda n: image.getpixel((int(cell * (n + 0.5)), 2 * 22 - 34))  # 원 안쪽 위(후광 영역)
        self.assertGreater(halo(1)[3], 0)  # 현재(2)는 후광이 칠해져 있다
        self.assertEqual(halo(0)[3], 0)    # 다른 칸은 속이 투명
        self.assertEqual(halo(2)[3], 0)

    def test_urls_use_the_phase_of_a_detail_stage(self) -> None:
        self.assertEqual(stage_image.url("2-3", "light"), stage_image.url("2-1", "light"))  # 같은 상위 단계는 같은 그림
        self.assertNotEqual(stage_image.url("2-3", "light"), stage_image.url("3-1", "light"))

    def test_rejects_unknown_input(self) -> None:
        with self.assertRaises(ValueError):
            stage_image.render("9", "light")
        with self.assertRaises(ValueError):
            stage_image.render("2", "sepia")


class RouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(main.app)

    def test_png_route_is_public_cached_and_validates(self) -> None:
        url = stage_image.url("2-3", "dark").replace(stage_image.base_url(), "")
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["content-type"], "image/png")
        self.assertIn("immutable", resp.headers["cache-control"])
        self.assertEqual(resp.content[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(self.client.get("/img/stage.png?s=9&t=light").status_code, 400)
        self.assertEqual(self.client.get("/img/stage.png?s=2&t=../x").status_code, 400)
        self.assertEqual(self.client.get("/img/stage.png").status_code, 422)

    def test_missing_korean_font_gives_503_not_a_broken_picture(self) -> None:
        stage_image.render.cache_clear()
        with patch.object(stage_image, "FONT_CANDIDATES", ["/no/such.ttf"]), patch.object(stage_image, "BOLD_CANDIDATES", []):
            self.assertEqual(self.client.get("/img/stage.png?s=2&t=light").status_code, 503)
        stage_image.render.cache_clear()

    def test_url_changes_with_stage_so_teams_refetches(self) -> None:
        self.assertNotEqual(stage_image.url("1-3", "light"), stage_image.url("2-1", "light"))
        with patch.dict("os.environ", {"BOARD_PUBLIC_URL": "https://bot.example.com/"}):
            self.assertTrue(stage_image.url("2-3", "dark").startswith("https://bot.example.com/img/stage.png?s=2&t=dark&v="))


if __name__ == "__main__":
    unittest.main()
