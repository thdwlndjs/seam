# 해부도 사이트

저장소 코드를 3D로 보는 해부도. `main`에 푸시할 때마다 GitHub Actions가 코드에서 다시 만들어 GitHub Pages에 올린다.

주소: `https://<GitHub 아이디>.github.io/<저장소 이름>/`

## 처음 한 번

1. 이 `anatomy/` 폴더와 `.github/workflows/anatomy.yml`을 저장소 루트에 넣고 푸시한다.
2. 저장소 **Settings → Pages → Build and deployment → Source**를 **GitHub Actions**로 바꾼다.
3. **Actions** 탭에서 `anatomy` 실행이 끝나면 위 주소에서 열린다.

## 무엇이 자동으로 바뀌나

| 자동 (코드에서 매번) | 사람이 고침 (`anatomy.json`) |
|---|---|
| 부품마다 실제 코드 조각, 파일·줄 링크 | 부품 설명·비유·주의 문구 |
| 설명에 적힌 함수가 사라지면 ⚠ 표시 | 부품 위치(3D 좌표), 층 |
| 어느 부품에도 연결 안 된 함수 목록 | 흐름 시나리오 |
| 함수 호출 관계로 이어지는 부품 연결선 | 부품에 연결할 함수(`refs`) |

페이지 오른쪽 아래 **코드 점검**에서 고칠 곳을 바로 볼 수 있다.

## anatomy.json 고치기

부품 하나:

```json
{
  "id": "f1", "layer": "feat", "pos": [-5.0, -1.9, -1.2], "label": "1 팀/프로젝트 정보",
  "refs": ["server/main.py::fetch_project_info"],
  "sum": "한 줄 요약", "ana": "쉬운 비유", "points": ["핵심 1", "핵심 2"], "warn": "주의 (없으면 빼기)"
}
```

- `refs`는 `경로 끝부분::함수·클래스 이름`. 경로는 끝부분만 맞으면 된다(`src/server/main.py`도 `server/main.py`로 찾는다).
- 첫 번째 `refs`의 코드가 코드 조각으로 나온다.
- `layer`는 `layers`에 있는 이름: bot, route, state, feat, store, ext, work.

## 로컬에서 미리 보기

```
python anatomy/build.py          # → _site/index.html 을 브라우저로 열기
python anatomy/build.py --strict # 코드와 다른 부품이 있으면 실패
```

파이썬 표준 라이브러리만 쓴다(설치할 것 없음). PR에서는 `--strict`로 확인만 하고 배포는 `main` 푸시 때만 한다.
