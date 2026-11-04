# PM봇 (Teams)

팀의 Notion·GitHub를 읽어 Teams에서 팀/프로젝트 정보, 진행상황, 내 역할을 알려 주는 봇입니다. [microsoft-teams](https://github.com/microsoft/teams.py) 기반입니다.

v1.1 계획과 규격은 [docs/v1.1/](docs/v1.1/)에 있습니다([plan](docs/v1.1/plan.md), [spec](docs/v1.1/spec.md), [phase별 작업](docs/v1.1/phases/)).

## 구조

| 경로 | 역할 |
| --- | --- |
| `src/bot/main.py` | Teams 입력단(포트 3978). 받은 턴을 서버의 `/api/turn`으로 넘기고, 서버가 보내는 상태·글·버튼을 Teams로 보낸다. 상태는 갖지 않는다 |
| `src/server/main.py` | 서버(포트 8000). 메뉴 처리, 설정 마법사, Notion·GitHub·Groq 호출 |
| `src/server/worklog/` | 저장소(SQLite). Notion 구조 지도와 수집한 원문 |
| `src/server/notion_map.py`, `notion_read.py` | Notion 읽기(토큰별 호출 관문 포함) |
| `src/server/metrics.py` | 메뉴 응답·백그라운드마다 Notion·Groq 호출 수를 한 줄로 남김 |
| `src/team_configs.json` | 팀별 연결 정보(토큰 포함, 커밋하지 않음). 설정 마법사가 만든다 |
| `src/worklog.sqlite3` | 저장소 파일(커밋하지 않음) |
| `scripts/record_notion_fixtures.py` | 테스트용 Notion 녹화(커밋하지 않는 폴더에 저장) |

## 처음 준비

1. 가상환경: `python -m venv .venv`
2. 의존성: `.venv\Scripts\python.exe -m pip install -e .`
3. `.env.example`을 `.env`로 복사하고 값을 채운다.

| 변수 | 필수 | 설명 |
| --- | --- | --- |
| `BOT_SERVER_SECRET` | 예 | 봇 ↔ 서버 검증용. 봇과 서버가 같은 값을 써야 한다. 없으면 둘 다 시작하지 않는다 |
| `GROQ_API_KEY` | 아니요 | 없으면 요약·분류 없이 원문으로 답한다 |
| `GROQ_MODEL` | 아니요 | 기본 `openai/gpt-oss-120b` |
| `CEREBRAS_API_KEY` | 아니요 | 있으면 Groq 한도가 찼을 때 백그라운드 판정·추출을 Cerebras(같은 모델 gpt-oss-120b)로 넘긴다. 회의록이 Cerebras에도 전송된다 |
| `DANGEROUSLY_ALLOW_UNAUTHENTICATED_REQUESTS` | 로컬만 | Playground로 테스트할 때 `true`. 실제 Teams 배포에서는 끈다 |
| `CLIENT_ID`, `CLIENT_SECRET`, `TENANT_ID` | 배포만 | Teams 봇 자격증명. Playground에서는 주석 처리 |

GitHub·Notion 토큰과 페이지 ID는 `.env`에서 읽지 않습니다. 팀마다 다르므로 봇에게 `설정`이라고 보내면 마법사가 받아서 `src/team_configs.json`에 저장합니다.

## 로컬 Playground에서 실행하기 (Microsoft 365 Agents Playground)

Teams/Azure 등록 없이 브라우저에서 봇을 테스트하는 방법입니다.

**최초 1회**: Node.js 설치 후 `npm install -g @microsoft/m365agentsplayground`

**실행 (터미널 3개, `echo-bot` 폴더에서, 이 순서대로)**

```powershell
# 터미널 1 — 서버 (http://localhost:8000)
.venv\Scripts\python.exe src\server\main.py

# 터미널 2 — 봇 (http://localhost:3978)
.venv\Scripts\python.exe src\bot\main.py

# 터미널 3 — Playground (http://localhost:56150, 브라우저 자동으로 열림)
agentsplayground start -e http://localhost:3978/api/messages -c msteams
```

브라우저가 안 열리면 http://localhost:56150 으로 직접 접속하면 됩니다. 종료는 각 터미널에서 `Ctrl+C`.

**참고**

- 서버 → 봇 → Playground 순서로 띄웁니다. Playground는 `/api/messages`가 응답할 때까지 기다립니다.
- 서버는 켜진 뒤 60초가 지나면 백그라운드로 Notion 지도를 동기화합니다(1시간마다).
- 서버 로그의 `metric kind=menu …` 줄에서 메뉴마다 Notion·Groq 호출 수와 걸린 시간을 볼 수 있습니다.
- 8000 / 3978 / 56150 포트가 이미 사용 중이면 이전 프로세스를 종료하세요(`netstat -ano | findstr :3978` → `taskkill /PID <번호> /F`).

## 테스트

```powershell
cd src\server
$env:GROQ_API_KEY=""; ..\..\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
```

- `GROQ_API_KEY`는 비워 둔다(Groq 없는 경로를 확인하는 테스트가 있다).
- 테스트는 실제 저장소(`src/worklog.sqlite3`)를 쓰지 않는다. 모든 테스트 모듈이 `tests/isolation.py`를 맨 먼저 불러 임시 저장소를 쓰게 한다.
- `BOT_SERVER_SECRET`은 덮어쓰지 않는다(테스트가 `test-secret`을 쓴다).
- 실제 Notion 녹화본(`src/server/tests/fixtures/notion_local/`)이 있으면 그것으로도 확인하고, 없으면 건너뛴다.
- **Playground로 직접 눌러 볼 때는 실제 저장소를 쓰지 않는다.** 버튼을 누르면 단계·항목이 실제로 저장된다. 저장소를 복사해(`Copy-Item src\worklog.sqlite3 $env:TEMP\w.sqlite3`) `WORKLOG_STORE=$env:TEMP\w.sqlite3`로 서버를 띄운다. 복사는 서버를 멈춘 뒤에 한다(WAL 파일이 함께 있다).

## 나중에 추가할 것 (TODO)

- **`2`번(진행상황 조회)에서 태스크별 진행률(%) 표시**: 지금은 GitHub 이슈가 열림/닫힘 둘 중 하나뿐이라
  "몇 % 했는지" 계산할 근거가 없음. 나중에 Jira 연동하면 Jira 쪽 진행률/상태 필드를 가져와서
  이걸 기반으로 태스크별 퍼센트를 보여줄 수 있을 듯 — 그때 다시 검토.
