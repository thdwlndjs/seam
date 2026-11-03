# Phase 0 준비

브랜치 `chore/v1.1-baseline` · 앞선 조건 없음 · 다음 [Phase 1](phase-1-md-parts.md)

이 phase가 끝나면, 이후 phase에서 쓸 기준값과 fixture가 준비되고 실행 문서가 지금 구조와 맞는다. 채팅 메시지를 몰래 저장하던 기능과 1시간 분석은 꺼진다. 그 밖의 기능은 바꾸지 않는다.

## 읽을 것

- [plan.md](../plan.md) "진행 원칙"
- [spec.md](../spec.md) "P1-0 기존 경로 정리", "계측"

## 작업

- [ ] 기준값을 먼저 잰다(아래 마지막 작업). 그다음 기존 경로를 끈다(spec P1-0).
    - `src/server/main.py`: `handle_turn`의 `_is_team_record` 분기와 `_record_turn_text`를 지운다.
    - `src/server/main.py`: `_worklog_loop`에서 `worklog_bridge.analyze_due` 호출을 뺀다. Notion 지도 동기화(`_sync_notion_maps`)는 그대로 둔다.
    - 이 기능을 검사하던 기존 테스트는 지우거나 바꾸고, 목록을 PR에 남긴다.
- [ ] `docs/v1.1/` 문서(업무보고, plan, spec, phases)를 커밋한다.
- [ ] [README.md](../../../README.md)의 실행법과 [pyproject.toml](../../../pyproject.toml)의 `start`가 없어진 `src/main.py`를 가리키는 것을 지금 구조(`src/server/main.py`, `src/bot/main.py`)로 고친다. `.env`에 `BOT_SERVER_SECRET`이 필요하다는 것도 적는다.
- [ ] 계측 로그(spec "계측")를 더한다.
    - Notion 호출 수: `notion_read`의 관문에서 센다.
    - Groq 호출 수·토큰: `worklog_bridge._groq_json`과 `main.py`의 Groq 호출 두 곳에서 센다.
    - 메뉴 응답(`handle_turn` → 메뉴 처리)과 백그라운드(`_worklog_loop`) 한 번마다 `metric …` 한 줄.
- [ ] fixture 녹화 스크립트를 `scripts/`에 두고, 아래를 `src/server/tests/fixtures/notion/`에 저장한다.
    - 1차·2차·3차 멘토링, 3차 질문지 행의 md 응답과 최상위 블록 목록
    - 팀 정보 페이지의 md 응답과 최상위 블록 목록
    - 토큰, 비밀번호(예: 클로바노트 공유 비밀번호)는 지운다.
- [ ] 기준값을 잰다: 처음 수집의 Notion 호출 수와 시간, 메뉴 1·2번 응답 시간과 Groq 호출 수.

## 먼저 쓸 테스트

- A33: 메뉴가 아닌 채팅 메시지를 보내도 `sources`에 아무것도 저장되지 않는다.
- A34: 백그라운드 루프가 `analyze_due`를 부르지 않는다.
- 계측: 메뉴 응답 한 번에 `metric` 줄이 정확히 하나 나오고, 토큰이 들어 있지 않다.
- fixture: 저장된 파일에 `secret_`, `ntn_`, `ghp_`, `gsk_`, `비밀번호:` 뒤의 값이 없다.

## 완료 조건

- [ ] A33, A34 통과
- [ ] 기존 테스트 통과(A18). 시작은 77개이고, P1-0으로 지우거나 바꾼 테스트를 뺀 수를 이후 phase의 기준으로 PR에 남긴다
- [ ] fixture가 저장소에 있고 비밀값이 없다
- [ ] 기준값이 PR 본문에 있다
