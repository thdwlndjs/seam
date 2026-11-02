# Python Teams Echo Bot

This is a minimal Microsoft Teams echo bot template using [microsoft-teams](https://github.com/microsoft/teams.py).

## Structure

- `src/main.py`: Main application code for the Teams bot.
- `pyproject.toml`: Project dependencies and metadata.

## Getting Started

1. Create a virtual environment: `python -m venv .venv`
2. Activate it: `. .venv/bin/activate` (Unix) or `.venv\Scripts\activate` (Windows)
3. Install dependencies: `pip install -e .`
4. Run the app: `python src/main.py`

Required environment variables:

- `N8N_SHARED_SECRET`: shared secret for the n8n → bot notify webhook
- `GROQ_API_KEY` (optional): used to summarize meeting notes into tasks
- `GROQ_MODEL` (optional, default `llama-3.1-8b-instant`)

`GITHUB_TOKEN`, `GITHUB_REPO`, `NOTION_TOKEN`, and the Notion page/GitHub project IDs are
**not** read from `.env` — they're per-team/per-user, collected through the bot's `설정`
chat command and stored in `team_configs.json` (gitignored, since it holds tokens).

## 로컬 Playground에서 실행하기 (Microsoft 365 Agents Playground)

Teams/Azure 등록 없이 브라우저에서 봇을 테스트하는 방법입니다.

**사전 준비 (최초 1회)**

- Node.js 설치 후 Playground CLI 설치: `npm install -g @microsoft/m365agentsplayground`
- 위 Getting Started 1~3번으로 `.venv` 만들고 의존성 설치
- `.env` 에서 `CLIENT_ID` / `CLIENT_SECRET` / `TENANT_ID` 는 **주석 처리**,
  `DANGEROUSLY_ALLOW_UNAUTHENTICATED_REQUESTS=true` 는 **주석 해제** (Playground는 인증 없이 요청을 보냄)

**실행 (터미널 2개, `echo-bot` 폴더에서)**

```powershell
# 터미널 1 — 봇 서버 (http://localhost:3978)
.venv\Scripts\python.exe src\main.py

# 터미널 2 — Playground (http://localhost:56150, 브라우저 자동으로 열림)
agentsplayground start -e http://localhost:3978/api/messages -c msteams
```

브라우저가 안 열리면 http://localhost:56150 으로 직접 접속하면 됩니다. 종료는 각 터미널에서 `Ctrl+C`.

**참고**

- 봇 서버를 먼저 띄워야 합니다. Playground는 `/api/messages` 가 응답할 때까지 기다립니다.
- 3978 / 56150 포트가 이미 사용 중이면 이전 프로세스를 종료하세요 (`netstat -ano | findstr :3978` → `taskkill /PID <번호> /F`).
- 실제 Teams에 배포할 땐 `.env` 주석을 반대로 되돌리세요 (자격증명 활성화, `DANGEROUSLY_...` 비활성화).

## 나중에 추가할 것 (TODO)

- **`2`번(진행상황 조회)에서 태스크별 진행률(%) 표시**: 지금은 GitHub 이슈가 열림/닫힘 둘 중 하나뿐이라
  "몇 % 했는지" 계산할 근거가 없음. 나중에 Jira 연동하면 Jira 쪽 진행률/상태 필드를 가져와서
  이걸 기반으로 태스크별 퍼센트를 보여줄 수 있을 듯 — 그때 다시 검토.
