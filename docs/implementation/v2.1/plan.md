# AI PM v2.1 기술 설계와 Phase별 구현 계획

- 기준 명세: [docs/ai-pm-spec.md](../../ai-pm-spec.md)
- 이전 계획: [docs/v1.1/plan.md](../../v1.1/plan.md), [docs/v1.1/spec.md](../../v1.1/spec.md), `docs/v1.1/phases/phase-0~5`
- 참고(읽기 전용): Build-log `C:\Users\aicam\Desktop\build-log` (`src/buildlog/db.py`, `search.py`, `index.py`, `plan.py`, `skills/buildlog/SKILL.md`)
- 작성일: 2026-11-04. 이 문서는 설계와 계획만 담는다. 코드·DB·외부 시스템은 아직 바꾸지 않았다.
- 개정: 2026-11-04 추가 논의 확정 5건 반영 — ① GitHub 작업물은 메뉴 진입 시 API 조회 후 사람이 골라 확인·공유 ② 문서 해석은 성숙도별 자동 기록, 확인은 판단 불가한 중요 충돌만 ③ Notion은 초기 선택 탐색 + 증분만, 질문 시 재탐색 없음 ④ 조율은 `coordination.md` + 방법론 레퍼런스 5종 선택 제공 ⑤ GitHub 진행상황은 기존 상태 분류 재사용, 완료와 검증 구분, 코드 분석·테스트·시연 검증 제외(아래 "개정 이력").

표기: **[확인]** 코드·DB·로그로 확인한 사실 / **[미확인]** 확인하지 않은 것 / **[제안]** 이 계획의 설계 제안.

---

## 1. v2.1 개요

### 1.1 목표

AI PM을 "단계·게이트를 관리하는 봇"에서 **프로젝트 전체 산출물의 맥락을 이해하고 팀의 의사결정과 작업 흐름을 조율하는 PM**으로 옮긴다. 판단은 근거와 함께 보여 주고, AI의 제안은 사람이 승인해야 결정·작업이 된다.

### 1.2 핵심 사용자 경험 (명세 G-1~6)

| # | 경험 | Teams 진입점 |
| --- | --- | --- |
| U1 | 프로젝트 리딩: 최근 회의와 실제 산출물로 현재 위치, 중요한 결정·문제 | 메뉴 1·2 |
| U2 | 맥락 축적: 흩어진 Notion 자료에서 원문·요약·지식·근거·관계·이력을 점진적으로 쌓기 | 백그라운드, 메뉴 4 |
| U3 | 자연어 확인: "구현됐어? 어디?", "결정됐어?", "이 주장 근거는?" → 확인 내용·근거 위치·조회 범위·미확인 | 메뉴 → 질문하기 |
| U4 | 팀 조율: 지금 정할 것 / 나중에 정할 것 / 역할 간 조율, 병렬 진행 제안 | 메뉴 2에서 연결 |
| U5 | 업무 연동: GitHub Issue 분해·배분 제안 → 승인한 것만 등록, 작업물은 사람이 골라 공유 | 메뉴 3·4 |
| U6 | 지식 대시보드: 기존 탭에서 지식 조회·검색·수정·논리 삭제·복원, 출처·관계·이력 | 🗂 구조 보기·고치기 탭 |

### 1.3 v1.1 As-Is (코드 기준)

브랜치 `feat/confirm-and-apply`(PR #6, 미병합). 마지막 커밋 이후 이 세션의 변경(구조 탭, 자동 반영, 단계 카드·이미지, 초기 설정 흐름)은 **커밋 전**이다 [확인].

| 영역 | 구현 상태 | 근거 |
| --- | --- | --- |
| Notion 수집·변경 감지 | 구현 | `notion_map.sync`·`sync_roots`(구조 지문 `fingerprint`), `_sync_notion_maps`(1시간), `_check_on_open`(10분) [`src/server/main.py`] |
| 부분별 Markdown·블록 ID | 구현 | `parts.build_unit_parts`, `source_parts.md`·`block_ids_json`·`content_hash` |
| 섹션 역할·부분 종류 분류(LLM) | 구현 | `CLASSIFY_PROMPT`, `PART_KIND_PROMPT` [`worklog_bridge.py`] |
| 의미 추출(LLM) | **회의 메모만** | `parts.extract_pending`("문서는 추출하지 않는다"), `apply.py`의 `kind = 'meeting'` 조건 8곳 이상 |
| 근거·이력 | 구현 | `sources`, `evidence`, `object_evidence`, `revisions` [`worklog/db.py` migration 1] |
| 관계 판단 | 부분 | `CHANGE_TYPES` 5종, `core._judge`·`JUDGE_PROMPT`. LLM 판정은 할 일·결정만(`apply.LLM_JUDGED_KINDS`), 대체는 결정끼리만(`core.py:304`) |
| 자동 반영·되돌리기 | 구현 | `apply.auto_apply_due`(날짜 속성 순), `apply.revert`, 결정 대체 알림 |
| 검색 | **없음** | 항목 비교용 `core.find_related`(단어 겹침 상위 5개)만 있음. FTS 없음 |
| 메뉴 1 | 구현 | `fetch_project_info` → `sections.snapshot`(페이지 순서로 잘라 읽음) → `_summarize_snapshot`(Groq 스트리밍) |
| 메뉴 2 | 구현(구성 문제) | `fetch_team_status`: 단계 / 확정된 흐름 / 진행 중(게이트 섞임) / 이전 단계 남은 것 / 추가 논의 / 천천히 / 피드백. LLM 호출 없음 |
| 메뉴 3 | 구현 | `fetch_my_role`, `_take_card`, `[내가 맡기]`(`apply.take_task`) |
| 메뉴 4 | 부분 | ① 완료 공유 = GitHub Project Done(`share_completed_work`, 저장본 미반영) ② Notion 반영(미리보기 확인) ③ 최근 반영 ④ 회의록 올리기(항상 회의 메모) ⑤ 소스 추가 |
| 단계·게이트 | 구현(과비중) | `worklog/stages.py`, 메인 카드 단계 카드, 자동 게이트 질문(`gate_missing`, `skipped_ahead`) |
| 탭 | 부분 | [`board_tab.py`](../../../src/server/board_tab.py), [`worklog/board.py`](../../../src/server/worklog/board.py), [`tab/board.html`](../../../src/server/tab/board.html): 항목 목록·검색(브라우저 안)·고치기·반영 취소(`cancelled`)·확인 필요 답·결정 대체 되돌리기·단계 상세. **복원·산출물 문서·관계 보기 없음** |
| GitHub | 부분 | 조회: 이슈·커밋·Project. 쓰기: 담당 배정(PATCH), Project Status(GraphQL). **이슈 생성·PR·파일 조회 없음** |
| Jira·Figma·슬라이드 본문·Clova | 미구현 | 관련 코드 없음 [확인] |
| LLM | 구현 | `gpt-oss-120b`, Groq → Cerebras, JSON 한 번 응답, `llm_queue`(하루·분당 몫, 순위 1~3), 추출 캐시(`part_extracts`) |

### 1.4 Gap (명세 대비)

1. 일반 문서(PRD·시장조사·발표)를 해석하지 않아 최종 기획·결정이 지식에 없다 (실데이터: 결정 3건, 문서 부분 100여 개 `collected`).
2. 미정을 닫거나 다른 종류를 대체하는 관계가 없어 "결정됐나/바뀌었나"에 답할 수 없다 (실데이터: `SUPERSEDES` 0건, 미정 18건).
3. 검색·질의 경로가 없다. 질문 메뉴도 없다.
4. 상태 구분이 없다: 가안 / 조언 / 논의 / 합의 / 작업 등록 / GitHub 완료(이슈 종료·PR 병합). 기능 검증은 v2.1에서 확인하지 않는 별도 사실로 둔다.
5. 게이트·단계가 메인 카드·메뉴 2·LLM 사용량의 중심을 차지한다.
6. 탭은 항목만 다루고, 산출물 문서·관계·복원이 없다. 사람 수정 보호 규칙이 없다.
7. GitHub: 이슈 생성이 없고, PR 목록·변경 파일 경로를 보지 않으며, 이슈 상태가 회의 결정·요구사항과 연결되지 않고, 완료 공유가 저장본과 단절돼 있다. 작업물 공유하기는 GitHub 작업물 목록을 보여 주지 않는다. 연결 저장소가 테스트용(`thdwlndjs/n8nTest`)일 수 있다 [미확인: 실제 대상].

### 1.5 재사용 범위

**그대로 쓴다:** Notion 수집·변경 감지·md 보관, 부분 나누기·블록 대조, 인용 검증(`core.normalize_item`·`parts._squash`), 추출 캐시, `sources`·`evidence`·`object_evidence`·`revisions`·`runs`, `work_items`·`work_links`·`change_candidates`, 자동 반영·되돌리기, `llm_queue`, 메뉴·카드 구조(버튼·링크·드롭다운·단계 이미지), 권한(`people.can_approve`), GitHub 호출 함수(`_github_get`·`_github_patch`·GraphQL), 탭의 인증 링크·API 틀.

**바꾼다:** 추출 대상 조건, 관계 판단 범위, 메뉴 1·2 구성, 메인 카드의 게이트 질문, 업로드 종류, 완료 공유의 저장본 반영, 프롬프트 위치(코드 → Markdown 지침).

**새로 만든다(최소):** FTS5 검색 색인(파생), 지침 로더, 조율 방법론 레퍼런스 5종, 질문하기 세션과 응답 카드, 이슈 분해 제안·승인 카드, 작업물 선택·확인 카드, 탭의 문서·관계·복원 화면, GitHub PR·커밋 목록·PR 변경 파일 경로 조회와 이슈 생성.

**v2.1에서 하지 않는다(확정):** GitHub Actions 기반 변경 수집, 자동 공유·브랜치 자동 생성, 질문 시 해석하지 않은 Notion 문서 재탐색, 실시간 외부 검색, 코드 분석·테스트 실행·시연 검증.

---

## 2. 전체 기술 설계

### 2.1 To-Be 아키텍처와 데이터 흐름

```
[Notion] ─(코드: 수집·변경 감지, 기존)─▶ source_pages / source_sections / source_units / source_parts(md) / sources(원문 판)
                                              │
             (LLM: 탐색 선택 explore, 최근 회의 우선)   │  ← 처음과 구조가 바뀐 소스만
                                              ▼
             (LLM: 산출물 해석 interpret)  바뀐 부분만, 내용 해시 캐시
                                              ▼
   산출물 문서(work_items kind=artifact, Markdown 요약)  +  지식 항목(decision/requirement/hypothesis/open/task/insight/feedback)
                                              │
             (코드 → 필요시 LLM: 관계 판단 reconcile, prior_check)
                                              ▼
   change_candidates → (코드: 승인 정책) → work_items / work_links / revisions   ──▶  FTS5 색인(파생, 재구축 가능)
                                                                                         │
   [Teams 메뉴·카드]  리딩(1·2) · 질문하기 · 조율 · 이슈 제안 · 작업물 공유  ◀──(코드: 검색·예산 조립)──┘
   [Teams 탭]        지식 대시보드: 목록·검색·본문·출처·관계·이력·수정·논리 삭제·복원
   [GitHub]          조회(이슈·PR·커밋 목록, PR 변경 파일 경로, 기존 작업 상태 분류), 승인된 쓰기(이슈 생성·배정·Project 상태)
                     작업물 공유하기 진입 시에만 최근 PR·커밋 목록 조회 → 사람이 선택 → 정리 → 확인 → Teams 공유
   [지침]            prompts/*.md(판단 방법) + prompts/methods/*.md(조율 방법론 레퍼런스, 필요한 것만 선택 제공)
```

원칙:
- 코드: 조회·변경 감지·검증·저장·검색·예산 조립·승인 정책·외부 쓰기.
- LLM: 무엇을 더 읽을지 고르기, 산출물 해석, 관계 판단, 종합 판단·답변·조율안 작성. 결과는 개발자가 정한 JSON 계약으로만 받는다.
- 평소 질문은 저장된 지식과 색인으로만 답한다. Notion을 다시 읽거나 해석하지 않은 문서를 질문 때 새로 탐색하지 않는다(v2.1 확정).

### 2.2 Notion 동적 탐색과 증분 지식 축적

**초기 파악 (팀 첫 연결 또는 재색인)** [제안]
1. 코드가 카탈로그를 만든다: 페이지 계층·DB 스키마(속성 이름·종류)·제목·날짜 속성·마지막 수정·부분 수·글자 수·링크. 본문은 넣지 않는다.
2. 최근 회의록부터: 날짜 속성(없으면 마지막 수정) 기준 최근 회의 행·페이지 N개(기본 3)를 먼저 해석한다.
3. LLM `explore` 호출 1회: 카탈로그 + 최근 회의 요약을 보고 "더 읽을 소스·영역 id와 이유"를 JSON으로 고른다(팀 규칙·목표·PRD·역할·관련 조사 등). 고정 페이지명을 전제하지 않는다.
4. 코드가 고른 영역만 `interpret`로 보낸다. 다 읽은 뒤 `explore`를 한 번 더 부를 수 있다(상한 2회).
5. 고르지 않은 영역은 수집·원문만 보관하고 해석하지 않는다. v2.1에서는 질문 때 이 영역을 다시 탐색·해석하지 않는다. 이 영역은 검색 색인에도 넣지 않고, 질문 답의 "조회 범위"에서 해석 대상이 아니었음을 밝힌다.

**증분 반영** [제안]
- 내용 변경(부분 `content_hash` 변경): 그 부분만 `interpret` → `reconcile`. 이미 해석한 같은 해시는 다시 부르지 않는다(기존 `part_extracts` 캐시 방식).
- 구조 변경(`fingerprint` 변경·새 섹션·새 페이지): 그 소스에 대해서만 `explore`를 다시 부른다.
- 사라짐: 기존 `status = 'gone'` 감지를 쓰되 바로 지우지 않는다. 블록 ID·인용으로 이동·병합을 먼저 대조하고(코드), 어디에도 없으면 "출처 없음"으로 표시해 탭에서 확인한다.

**가안·확정 구분과 자동 기록** [확정]
- 지식 항목의 `attributes_json.maturity`: `draft`(가안) / `recommended`(조언·멘토 제안) / `discussed`(회의 논의) / `agreed`(합의·확정). LLM이 원문과 합의 근거(인용)로 정하고, 인용이 없으면 `agreed`로 두지 않는다.
- 출처 종류(회의록·일반 문서)만으로 확인 요청을 하지 않는다. 일반 문서의 내용도 성숙도에 따라 자동 기록한다.
- 새 가안·조언이 기존 결정과 다르면 기존 결정을 유지하고, 새 내용은 대안(`ALTERNATIVE_TO` 링크)으로 기록한다.
- 원문에 명확한 변경(합의 근거가 있는 새 결정)이 있으면 대체(`SUPERSEDES`)로 자동 반영하고 출처와 대체 관계를 남긴다(알림 + 되돌리기).
- 중요한 충돌인데 근거만으로 판단할 수 없을 때만 확인 필요(`needs_discussion`)로 보낸다. "중요"는 결정·요구사항 대상이고 관련 할 일이나 다른 결정이 연결된 경우로 시작한다(Phase 2에서 측정 후 조정).
- 모든 자동 반영은 출처·이력(`revisions`)·탭 수정·되돌리기·복원이 가능해야 한다.
- 작업 상태는 지식이 아니라 사실로 계산한다(코드): 기존 GitHub 상태 분류(완료 / 진행 중(커밋 있음) / 이슈만 등록 / 이슈 미등록, `_classify_tasks_by_github_state`) + PR 병합 여부. **GitHub 완료(이슈 종료·PR 병합)는 기능 검증 완료가 아니다.** v2.1은 코드 분석·테스트 실행·시연 검증을 하지 않으므로 "검증됨" 상태를 만들지 않는다.
- "최신 수정일", "최종" 제목만으로 `agreed`로 두지 않는다(지침에 명시, 검증 테스트로 확인).

**사람 수정 보호** [제안]
- 탭에서 사람이 고친 항목은 `revisions.actor`가 사람이다. 재수집·재해석이 그 항목을 바꾸려 하면 덮어쓰지 않고 `needs_discussion`(확인 필요)으로 보낸다.
- 사람이 논리 삭제한 항목은 같은 근거로 다시 만들지 않는다(지문 `change_candidates.fingerprint` + 항목 상태 확인).

### 2.3 Build-log식 Markdown·DB·검색·관계·이력

Build-log 구조 확인 결과 [확인]: `pages(type, title, summary_md, body_md, status, version)`, `sources`, `evidence`(원문 위치 대조, `plan._locate`), `relations`(SUPERSEDES·CONTRADICTS 등), `revisions`, FTS5(`search_index`, trigram, 파생·`reindex`), `search.query`(바이트 예산 안에서 Markdown으로 반환, 대체·폐기 감점 `_STATUS_PENALTY`, 충돌 표시 `_conflicts`), 저장 전 `prior_check`. 판단은 외부 에이전트가 `SKILL.md` 지침으로 하고 Build-log 코드는 검증·저장·검색만 한다.

**AI PM에 매핑 (스키마 최소화)** [제안]

| Build-log | AI PM v2.1 | 변경 |
| --- | --- | --- |
| `pages` (body_md, summary_md, status, version) | `work_items` kind=`artifact`: `title`, `statement_md`(Markdown 요약 본문), `attributes_json`(`summary`, `artifact_type`, `source_ids`, `part_ids`, `maturity`), `status`, `version` | 새 표 없음. kind 값 추가 |
| 지식 항목 | `work_items` kind=`decision`·`requirement`·`hypothesis`·`open`·`task`·`insight`·`feedback` | `requirement`·`hypothesis` 값 추가 |
| `sources`·`evidence` | 그대로 | 없음 |
| `relations` | `work_links.type`: 기존 `SUPERSEDES`·`AFFECTS` + `DERIVED_FROM`(항목→산출물 문서), `RESOLVES`(답→미정), `ALTERNATIVE_TO`(가안·조언→기존 결정), `RELATED_TO` | 글자 값 추가(표 구조 그대로) |
| (GitHub 연결) | `work_links`는 양쪽이 `work_items` 외래 키라 GitHub를 담지 못한다 [확인]. 기존 `attributes.issue_refs`를 `attributes.github_refs: [{type: issue/pr, number, basis: explicit/inferred}]`로 넓힌다(기존 `issue_refs`는 explicit로 읽음) | JSON 칸 안의 키 추가 |
| `revisions` | 그대로 | 없음 |
| `search_index`(FTS5) | 새 가상 표 `knowledge_fts(item_id UNINDEXED, kind, title, body, extra, tokenize='trigram')` | **migration 9**(파생, `reindex`로 재생성) |
| `search.query` | `worklog/search.py`(신규 모듈): 검색 → 점수 → 관계 이웃 → 예산 안 Markdown 조립 | 코드만 |
| `prior_check` | `reconcile` 전 코드가 FTS + `find_related`로 후보를 찾아 LLM에 함께 준다 | 코드만 |

- SQLite 3.45.3에서 FTS5 trigram 생성 확인 [확인].
- **단문 검색 제약과 보완** [확인 + 제안]: 메모리 DB 실험에서 trigram `MATCH`는 3글자 미만 검색어("미러", "UI", "옷")에 0건을 돌려주고, 같은 FTS 표의 열에 `LIKE '%어%'`를 쓰면 찾아진다(실행 계획상 전체 훑기). 그래서 검색은 같은 색인 하나로 두 경로를 쓴다.
  - 검색어 정리: 조사 떼기(Build-log `search._strip_particle` 패턴 재사용, "미러를" → "미러"), 중복 제거.
  - 3글자 이상 검색어: `MATCH`(색인 사용, bm25 순위).
  - 3글자 미만 검색어: 같은 표에서 `scope` 조건 + 열 `LIKE` 부분 일치(새 색인·새 표 없음). 훑는 범위는 그 팀의 살아 있는 항목으로 한정한다.
  - 두 경로의 후보를 합쳐 같은 점수 규칙(제목 > 본문, 짧은 검색어는 가중치 낮춤, 대체·취소 감점)으로 정렬한다.
  - 선택 이유: 별도 n-gram 표나 형태소 분석기는 새 구조가 되고, 지금 데이터 규모(팀당 항목 수백~수천)에서는 범위를 좁힌 부분 일치 훑기로 충분하다고 본다. 성능은 Phase 1에서 측정하고, 기준을 넘으면 그때 대안을 다시 검토한다.
- `work_items_live_title` 고유 인덱스(scope, kind, normalized_title)는 `artifact`에도 걸린다. 산출물 제목은 경로를 포함해 만든다(같은 이름 페이지 충돌 방지).
- `status` 값 추가: `resolved`(미정이 답을 얻음). `superseded`·`cancelled`(논리 삭제)는 기존 그대로. 고유 인덱스 제외 목록에 `resolved` 포함 여부를 Phase 1에서 정한다.

### 2.4 메뉴·하위 기능별 LLM Markdown 지침

지침은 **판단 방법**, 지식 DB는 **판단 대상**. 서버가 지침 파일을 읽어 조합한다(Groq가 파일을 읽지 않음).

**파일 트리** [제안]
```
src/server/prompts/
  common/
    evidence.md        # 근거·인용·출처 표기, 찾지 못한 것을 단정하지 않기, 조회 범위 밝히기
    states.md          # 가안/조언/논의/합의, 작업 등록/GitHub 완료(이슈 종료·PR 병합) 구분, "검증됨"은 v2.1에서 말하지 않음
    output-json.md     # JSON만 출력, 계약 밖 키 금지
  explore.md           # 카탈로그에서 더 읽을 영역 고르기
  interpret.md         # 산출물 해석: 요약 + 지식 항목(모든 문장을 쪼개지 않기)  ← PART_EXTRACT_PROMPT 이관
  reconcile.md         # 새 정보 ↔ 기존 정보 관계  ← JUDGE_PROMPT 이관
  project-info.md      # 메뉴 1 설명  ← _summarize_snapshot 프롬프트 이관
  project-leading.md   # 메뉴 2 브리핑
  qa.md                # 질문하기 답변
  coordination.md      # 지금/나중/역할 간 조율, 대안 검토
  issue-breakdown.md   # 개발 이슈 분해·담당·완료 기준 제안
  work-share.md        # 선택된 작업물 공유 정리
  methods/             # 조율 방법론 레퍼런스(coordination이 필요한 것만 포함)
    index.md           # 방법론별 한 줄: 이름·적용 조건(항상 포함, 짧게)
    scrum-inspect-adapt.md
    raid.md            # Risks, Assumptions, Issues, Dependencies/Decisions
    product-discovery.md  # 가치·사용성·실현 가능성·사업성
    design-sprint.md   # 문제 정의·프로토타입·검증
    spike.md           # 불확실성 해소를 위한 짧은 기술 탐색
```
- 방법론 파일 형식(공통 5칸): 적용 조건 / 핵심 판단 질문 / 예상 산출물 / 비적용 조건 / 원문 출처. 파일당 약 400토큰 이내. 실시간 외부 검색은 하지 않으므로 출처는 작성 시 원문을 확인해 고정한다.
- 섹션 분류(`CLASSIFY_PROMPT`)·부분 분류(`PART_KIND_PROMPT`)·단계 판정(`STAGE_PROMPT`)은 Phase 2에서 역할이 줄어든다. 파일로 옮기되 내용은 유지한다.
- 로더 `prompts.load(name)`: 본문 + `@include common/evidence.md` 같은 포함 지시를 풀어 하나의 시스템 프롬프트로 만든다. 결과의 해시를 `prompt_version`으로 써서 지침을 고치면 캐시가 자연히 무효가 된다(기존 `EXTRACT_PROMPT_VERSION` 대체).
- 메뉴 하나 = 호출 하나로 고정하지 않는다. 기능별 호출 수는 2.5 표를 따른다.

**기능 ↔ 지침 ↔ 호출 매핑**

| 기능 | 트리거 | 입력 근거(코드가 조립) | 지침 | 출력 JSON | 호출 수 |
| --- | --- | --- | --- | --- | --- |
| 탐색 선택 | 첫 연결, 구조 변경 | 카탈로그, 최근 회의 요약 | explore + common/output-json | `{read: [{area_id, reason}], skip_reason?}` | 소스당 1~2 |
| 산출물 해석 | 바뀐 부분 | 부분 Markdown 원문(필요시 나눠서), 경로, 날짜 | interpret + common/* | `{artifact: {type, summary, maturity}, items: [{kind, title, detail, quote, maturity, owner, due, confidence}]}` | 부분당 1(캐시) |
| 관계 판단 | 새 항목에 후보가 있을 때 | 새 항목(성숙도 포함), 후보(FTS + find_related, 종류 무관) | reconcile + common/states | `{relation: same/supplement/new/alternative/supersede/resolve/conflict, target_ids, decidable: bool, rationale, confidence}` | 항목당 0~1 |
| 메뉴 1 설명 | 메뉴 1 | 산출물 문서 요약(최근 순), 팀 정보 | project-info + common/evidence | 스트리밍 글 | 1 |
| 메뉴 2 브리핑 | 메뉴 2 | 최근 회의 산출물 + 합의/논의/미정 항목 + 작업 사실 | project-leading + common/* | `{decided:[…], doing:[…], unclear:[…]}` 각 줄에 근거 id | 1 |
| 질문하기 | 사용자 질문 | 축적된 지식 검색 결과(예산 조립), 구현·작업 질문이면 GitHub 작업 상태 분류·연결된 이슈·PR(+변경 파일 경로) | qa + common/* | `QaResult`(아래 계약) | 질문당 전체 최대 2(아래 흐름) |
| 팀 조율 | 메뉴 2 → [조율 보기], 또는 질문하기의 `intent=coordination` | 미정·확인 필요·충돌·대안·의존(AFFECTS)·작업 사실 + 방법론 index + 코드가 고른 레퍼런스 최대 2개 | coordination + methods/index + methods/(선택) + common/* | `CoordinationResult`(아래 계약) | 메뉴 경로 1(+`need_method`면 1) / 질문 경로 1(추가 없음) |

**질문하기·조율 출력 계약** [확정 설계]
```json
// QaResult — qa 호출의 출력
{
  "intent": "answer | coordination",
  "answer": "string",
  "findings": [{"claim": "string", "evidence_ids": ["item_id|github_ref"], "location": "string", "link_basis": "explicit|inferred"}],
  "work_status": [{"ref": "issue#n|pr#n", "github_state": "done|in_progress|not_started|unlinked|merged|open", "linked_items": ["item_id"]}],
  "searched_scope": [],          // 코드가 채운다. 모델 값은 무시
  "unknowns": ["string"],
  "need": [{"type": "item_source|github", "id": "item_id|issue#n|pr#n"}],  // 최대 5개, 1차 호출에서만 유효
  "next": [{"type": "ask|agenda", "text": "string"}]
}
// CoordinationResult — coordination 호출의 출력
{
  "now": [...], "later": [...], "cross_role": [...], "parallel": [...],
  "compared_alternatives": [{"option": "string", "fits_when": "string", "tradeoffs": "string"}],
  "methods_used": ["scrum-inspect-adapt|raid|product-discovery|design-sprint|spike"],
  "need_method": "method_id | null"   // 메뉴 경로에서만 유효
}
```

**질문 한 건의 호출 흐름(상한 2회)**
1. 1차 `qa` 호출.
2. 코드 분기(한 가지만 택함, 우선순위 순):
   - `intent = coordination` → 2차는 `coordination` 1회(질문과 1차 결과를 입력에 포함). 이 경로에서 `need_method`는 무시하고, 코드가 미리 고른 레퍼런스만 쓴다.
   - `need`가 비어 있지 않음 → 코드가 검증한다. 이미 해석된 항목의 원문 구간과 GitHub 상태 사실만 허용하고, 해석 대상이 아닌 영역이나 알 수 없는 id는 버리고 `unknowns`에 기록한다. 통과한 근거를 붙여 2차 `qa` 1회를 부른다.
   - 둘 다 아님 → 1차 결과로 답한다(호출 1회).
3. 2차 결과의 `need`·`intent`는 무시한다. 더 필요한 것은 `unknowns`와 `next`로만 남긴다.
4. 어느 호출이든 JSON 계약 위반이면, 그 호출을 같은 단계에서 한 번 다시 시도하지 않고 바로 "답을 만들지 못함 + 조회 범위" 카드로 끝낸다(질문당 2회 상한 유지). 공급자 전환(Groq → Cerebras)은 같은 호출의 재전송이라 횟수에 넣지 않는다.
| 이슈 분해 | 결정/요구사항에서 [이슈 제안] | 선택한 항목 + 관련 근거 | issue-breakdown | `{issues:[{title, body, acceptance, assignee_hint}]}` | 1 |
| 작업물 공유 | 메뉴 4 → 작업물 공유하기 → 목록(코드, GitHub API)에서 선택 | 선택한 PR·커밋(제목·본문·변경 파일 경로·연결 이슈) + 관련 지식 검색 | work-share + common/evidence | `{summary, links, related_item_ids}` | 선택 1건당 1 |

### 2.5 Groq 호출 전략·토큰 예산·실패 대응

**현재 실측** [확인]: `llm_usage` 11/03 Groq 42,026 + Cerebras 185,597 토큰, 11/04 Groq 19,384 + Cerebras 5,580 토큰. 추출 85회의 입력 최대 2,505 토큰. 출력이 입력보다 많음(Cerebras 입력 44,253 / 출력 71,396): 추론형 모델의 출력 비중이 크다.
**코드상 몫** [확인]: Groq 하루 200,000(백그라운드 70%), Cerebras 하루 1,000,000(백그라운드 90%)·분당 5회, 메뉴 입력 4,000(`GROQ_INPUT_TOKENS`), 백그라운드 출력 3,000(`WORKLOG_MAX_TOKENS`). 이 값은 앱의 예산이며 모델의 문맥 한계가 아니다. 공급자 공식 한도는 [미확인].

**전략** [제안]
- 순위: 사용자 대기(질문·메뉴) > 해석·관계 판단 > 탐색 > 단계 판정. 기존 `llm_queue` 순위에 `explore`를 추가한다.
- 기능별 입력 예산(초기값, 측정 후 조정): explore 6,000 / interpret 부분당 4,000(넘으면 블록 경계로 나눔) / reconcile 2,000 / 메뉴 1·2 4,000 / qa 5,000 / coordination 5,000(방법론 index ~200 + 레퍼런스 최대 2개 ~800 포함) / issue-breakdown 3,000 / work-share 3,000. 출력 상한 1,500~3,000.
- 개정 영향:
  - 문서 해석의 확인 요청 축소는 호출 수를 바꾸지 않는다(관계 판단 1회 안에서 `decidable`로 구분).
  - 질문 때 재탐색을 하지 않아 질문당 호출이 최대 2회로 고정되고, 해석 안 한 원문을 끌어오는 토큰이 없다.
  - 조율은 방법론 전체(5개 × ~400)를 넣지 않고 최대 2개만 넣어 호출당 약 1,200토큰을 아낀다. 메뉴 경로에서 `need_method`가 있을 때만 1회 더 부르고, 질문 경로에서는 추가 호출이 없다(질문당 2회 상한).
  - 작업물 공유는 목록 조회가 코드(GitHub API)라 LLM이 없고, 사람이 고른 작업물 1건당 정리 1회다.
  - GitHub 진행상황은 기존 상태 분류(코드)를 써서 LLM 호출이 늘지 않는다.
- 캐시: interpret는 (부분 해시, 지침 해시) / 메뉴 1·2는 (입력 지문) / qa는 캐시하지 않는다.
- 실패(백그라운드: 해석·관계 판단·탐색): JSON 파싱·계약 위반 → 같은 공급자에 오류를 붙여 1회 재시도 → 다음 공급자 → `llm_queue.Busy`면 다음 주기. 사용자 대기 호출(메뉴·질문)은 재시도 없이 대체 표시(질문은 2.4 흐름, 메뉴는 기존 목록형).
- **검증 오류와 의사결정 충돌을 나눈다** [확정 설계]:
  - **기술적 검증 오류**(인용이 원문에 없음, JSON·계약 위반, 관계 판단 호출 실패·한도 초과)는 지식이 아니라 처리 기록이다. 기존 `runs` 표(`stage = interpret|reconcile|explore`, `status = failed|partial`, `error_kind = quote_mismatch|contract|llm_failed|busy`, `attempts`, `error`, `result_json` = 버려진 후보와 이유)에 남긴다. 해당 후보는 **항목으로 만들지 않는다**(근거 없는 확정 방지). 부분 상태는 `extracted`로 남겨 재처리 대상으로 둔다.
  - **재처리 경로**: (1) 다음 주기 자동 재시도(인용 불일치 목록을 오류 피드백으로 붙여 1회), (2) 지침·프롬프트 해시가 바뀌면 캐시가 무효가 되어 자연 재처리, (3) 수동 명령 `python -m worklog reprocess --scope <팀> [--error-kind ...]`(기존 `worklog/__main__.py`에 추가). 자동 재시도 상한은 부분당 3회(`attempts`), 넘으면 `failed`로 멈추고 계측(`metrics`) 로그와 관리자용 요약 줄(메인 카드 알림, 기존 알림 줄 재사용)로만 알린다.
  - **의사결정 충돌**만 `needs_discussion`: 관계 판단이 정상으로 끝났고 결과가 `conflict` + `decidable = false` + 중요 대상일 때(Phase 2 정책 표). 기술 오류는 어떤 경우에도 `needs_discussion`이 되지 않는다.
  - v1.1의 "판정 실패(fallback)·확신 0.7 미만 → `needs_discussion`" 규칙([`apply._apply_one`](../../../src/server/worklog/apply.py))은 이 구분으로 바뀐다: 판정 실패는 기술 오류(재처리), 낮은 확신은 관계 결과의 `decidable`로 판단한다.
- 단계 판정은 기본 하루 1회로 줄인다(항목 변화마다 → 하루 1회 또는 메뉴 2 요청 시). [제안]
- `reasoning_effort` 등 공급자 옵션으로 출력 토큰을 줄일 수 있는지 [미확인] → Phase 1에서 측정.

### 2.6 GitHub 업무 관리와 Teams 연동

- 대상 저장소와 권한 확인이 선행 조건이다(현재 설정 `thdwlndjs/n8nTest`, Project #4) [미확인: 실제 대상·토큰 범위].
- **진행상황:** 기존 메뉴의 GitHub 작업 상태 분류(`_classify_tasks_by_github_state`: 완료 / 진행 중(커밋 있음) / 이슈만 등록 / 이슈 미등록)를 그대로 쓴다. 이 상태를 회의 결정·요구사항·산출물 지식과 연결해 설명한다.
- **조회 추가(메타데이터만):** `GET /repos/{repo}/pulls?state=all`(PR 목록·병합 여부), `GET /pulls/{n}/files`(변경 파일 경로), 커밋 목록(기존). 코드 본문 분석·코드 검색·테스트 실행·시연 검증은 v2.1에서 하지 않는다.
- **연결 구분:** 명시적(이슈·PR 본문·커밋 메시지의 `#번호`, 저장본 `github_refs.basis = explicit`)과 추정(LLM이 의미로 연결, `basis = inferred`)을 따로 표시한다. 추정 연결은 "추정"으로만 말한다.
- **완료 ≠ 검증:** 이슈 종료·PR 병합은 "GitHub에서 완료"로만 표시한다. 기능이 실제로 동작·검증됐다고 말하지 않는다.
- **쓰기:** 이슈 생성 `POST /repos/{repo}/issues`(신규), 배정(기존 PATCH), Project 상태(기존 GraphQL). **모두 승인 카드 뒤에만** 실행하고, 실패하면 저장본을 바꾸지 않는다.
- **작업물 공유하기:** 사용자가 메뉴에 들어오면 그때 GitHub API로 최근 PR·커밋 목록을 조회해 카드로 보인다 → 팀원이 고르면 관련 맥락(연결 이슈·결정·요구사항)을 정리한 미리보기 → 팀원이 확인하면 Teams에 공유하고 지식 근거로 남긴다. 기존 4번 ④ 파일·텍스트 올리기는 그대로 둔다.
- **하지 않는 것:** GitHub Actions 기반 변경 수집, 모든 커밋의 자동 공지·자동 공유, 브랜치 자동 생성·자동 병합(약식 GitHub Flow는 팀 규칙으로만 안내).

### 2.7 기존 탭을 활용한 지식 대시보드

현재 탭 [확인]: 항목 종류별 묶음, 출처 필터, 검색(브라우저 안), 항목 패널(종류·제목·담당·내용·출처·이력), 저장·반영 취소(`cancelled`), 확인 필요 [확정][미정][무시], 결정 대체 되돌리기, 단계 상세 대화상자. API는 `GET /api/board`, `POST /api/board/items/{id}`, `/cancel`, `/answer`, `/changes/{id}/revert`.

v2.1 [제안]: 같은 화면에 "산출물 문서" 묶음(Markdown 본문 보기·고치기), 항목의 관계 목록(`work_links`), 논리 삭제 항목 보기와 **복원**(`revisions` 이전 스냅샷), 서버 검색(FTS) 연결. 저장 시 색인 갱신과 사람 수정 표시. 원본 Notion·GitHub는 바꾸지 않는다.

---

## 3. Phase별 상세 구현 계획

**순서 근거 (코드 의존성)**: 지침 로더·지식 모델·검색(P1)이 해석(P2)·화면(P3~)의 공통 기반 → 해석이 지식을 채워야(P2) 대시보드·리딩·질문이 의미 있음 → 대시보드(P3)로 사람이 정정할 수 있어야 리딩·질문(P4·P5)의 근거를 믿을 수 있음 → 조율(P6)은 질문의 검색 조립을 재사용 → GitHub 쓰기(P7)는 저장소 확인과 승인 카드가 필요해 마지막.

| Phase | 이름 | 사용자가 얻는 것 | 선행 |
| --- | --- | --- | --- |
| 0 | 정리와 안전장치 | 자잘한 게이트 질문이 사라짐, 메뉴 2가 덜 어수선함 | — |
| 1 | 지식 기반: 지침 로더·지식 모델·검색 색인 | (내부) 이후 기능의 공통 기반 | 0 |
| 2 | 산출물 해석과 증분 축적 | 기획·조사·발표 문서가 지식으로 들어옴, 결정 변경·미정 해결이 이어짐 | 1 |
| 3 | 탭 지식 대시보드 | 문서·지식 조회·검색·수정·삭제·복원, 출처·관계·이력 | 1, 2 |
| 4 | 프로젝트 리딩(메뉴 1·2) | 근거 있는 현재 상황 브리핑 | 2 |
| 5 | 질문하기(자연어 확인) | "구현됐어? 결정됐어? 근거는?"에 위치·범위·미확인과 함께 답 | 1, 2, 4 |
| 6 | 팀 조율 | 지금/나중/역할 간 조율과 병렬 진행 제안(방법론 레퍼런스 근거), 안건·보류 선택, 팀원 대안 비교 | 4, 5 |
| 7 | 업무 연동(GitHub) | 이슈 분해 제안 → 승인 → 등록·배분, 작업물 공유하기(목록 조회 → 선택 → 확인 → 공유), 완료 동기화 | 1, 5, 저장소 확인 |

개정 후에도 순서는 그대로다. 작업물 공유하기의 PR·커밋 목록 조회 함수가 Phase 5의 GitHub 조회 함수를 재사용하므로 Phase 7이 Phase 5 뒤에 오는 의존은 유지된다. 조율 방법론 레퍼런스는 Phase 6 안에서 작성한다(다른 Phase가 쓰지 않음).

---

### Phase 0. 정리와 안전장치

**Goal**
- 이미 합의한 인터랙션 정리(게이트 자동 질문 제거)와 테스트 데이터 원상복구로 v2.1 작업의 출발점을 깨끗하게 한다.

**As-Is**
- `worklog/stages.py` `proposals()`가 현재 단계 확정 후 `gate_missing`·`skipped_ahead`를 만든다(266~290행). [정할게요]는 기본값과 같은 값을 쓴다.
- 메뉴 2 `fetch_team_status`가 게이트를 "정할 것"으로 진행 중 목록에 섞는다(`main.py` 1390행 부근).
- 실제 저장소에 테스트로 바뀐 3건: `team_stages`(2-1 확정), `prompts_asked`(답 2건), `stage_gates`(팀 행 1건). 백업 `src/worklog.sqlite3.bak-before-initflow-20261104-150842`.
- 이 세션 변경 사항이 커밋 전이다.

**To-Be**
- 단계 확정 후 메인 카드에 게이트 질문이 뜨지 않는다. `stage_advance`·`pivot`·`stage_init`은 그대로.
- 메뉴 2: 게이트 문장이 "진행 중"에서 빠지고, 단계는 "확정 / AI 추정" 한 줄.
- 테스트는 실제 저장소가 아닌 복사본으로 돌리는 절차가 문서에 있다.

**Implementation**
1. (사용자 승인 후) 현재 변경분 커밋 정리: PR #6 범위에 포함할지 결정 → 커밋.
2. `stages.proposals()`에서 `gate_missing` 루프, `skipped_ahead` 블록 제거. `answer()`의 decide/later/skip 분기 제거. `set_gate_state`·`edit_gate`는 유지.
3. `stages.board()`에 `candidate`(카드와 같은 규칙: 확정 > 논의 중 단계 > 게이트 기준) 추가. `worklog/board.py`의 중복 규칙을 이것으로 교체.
4. `main.fetch_team_status`: `doing`에서 게이트 줄 제거, 단계 표시 문구를 `확정: X` / `AI 추정: Y`로.
5. 데이터 복구(승인 후): SQLite 백업 API로 새 백업 → 3건을 되돌리는 SQL을 한 트랜잭션으로 → 시험 전 백업과 세 표 비교.
6. `docs/v1.1/spec.md` P2-4 표에서 게이트 질문 행 정리, "알려진 한계: 게이트 later/skipped 지정 경로 없음" 기록.
7. 테스트 실행 절차: `WORKLOG_STORE=<복사본>`으로 서버를 띄우는 방법을 README에 추가.

**Dependencies**
- 사용자 승인: 커밋 범위, 데이터 복구.

**Acceptance Criteria**
- 단계를 확정해도 다음 메인 카드에 게이트 질문이 없다.
- 메뉴 2에 "정할 것:" 줄이 없고 단계 표기가 한 줄이며, 카드·탭·메뉴 2가 같은 단계 후보를 보인다.
- 실제 저장소의 세 표가 시험 전 백업과 같다.

**Verification**
- 단위: `test_stages`(게이트 질문이 생기지 않음, advance·pivot 유지), `test_menus_v11`(메뉴 2 문구), `test_board`(후보 일치).
- 전체 테스트 OK. 복사본 저장소로 Playground에서 메인 카드·메뉴 2 확인.

**Out of Scope**
- later/skipped 의미 재정의, 게이트 상태 탭 UI, 단계 직접 변경.

---

### Phase 1. 지식 기반: 지침 로더·지식 모델·검색 색인

**Goal**
- 판단 방법(Markdown 지침)과 판단 대상(지식 DB·색인)의 공통 기반을 만든다. 사용자 화면 변화는 거의 없다.

**As-Is**
- 프롬프트가 코드 상수(`worklog_bridge.EXTRACT_PROMPT`, `PART_EXTRACT_PROMPT`, `JUDGE_PROMPT`, `CLASSIFY_PROMPT`, `PART_KIND_PROMPT`, `STAGE_PROMPT`, `main._summarize_snapshot` 프롬프트). 판 관리는 `EXTRACT_PROMPT_VERSION` 수동 문자열.
- `work_items` 종류 5개, `work_links` 2종, 검색 없음. 고유 인덱스 `work_items_live_title`(migration 7).

**To-Be**
- `src/server/prompts/` 지침 파일과 로더. 기존 프롬프트가 파일로 옮겨지고 결과는 같다(회귀 없음).
- 지식 모델 확장: kind `artifact`·`requirement`·`hypothesis`, `attributes_json.maturity`, `attributes_json.github_refs`(기존 `issue_refs` 호환), status `resolved`, 링크 `DERIVED_FROM`·`RESOLVES`·`ALTERNATIVE_TO`·`RELATED_TO`.
- `knowledge_fts` 색인과 `worklog/search.py`(검색 → 예산 조립). 항목을 쓰는 모든 경로가 색인을 갱신한다.

**Implementation**
1. `src/server/prompts/` 생성, 2.4 트리의 `common/*`, `interpret.md`(기존 `PART_EXTRACT_PROMPT` 내용 이관), `reconcile.md`(기존 `JUDGE_PROMPT`), `project-info.md`(기존 요약 프롬프트). 나머지 파일은 Phase별로 추가한다.
2. `src/server/prompts.py`(신규): `load(name) -> (text, version_hash)`, `@include` 해석, 파일 변경 시 다시 읽기(프로세스 캐시 + mtime).
3. `worklog_bridge`: `PART_EXTRACT_PROMPT` 등 상수 사용처를 `prompts.load`로 교체. `EXTRACT_PROMPT_VERSION`은 로더 해시로 대체(기존 캐시를 무효화하지 않도록 첫 판은 같은 문자열로 고정 가능).
4. `worklog/db.py` migration 9:
   - `CREATE VIRTUAL TABLE knowledge_fts USING fts5(item_id UNINDEXED, scope UNINDEXED, kind, title, body, extra, tokenize='trigram')`
   - `work_items_live_title` 재생성: 제외 상태에 `resolved`를 넣을지 결정해 반영.
5. `worklog/core.py`: `KINDS`(또는 검증 목록)에 새 종류 추가, `TASK_STATUSES` 영향 확인, `_create_item`의 `attributes` 키에 `maturity`·`artifact_type`·`summary`·`github_refs` 허용. `issue_refs`를 읽는 곳(`take_task`, `main._take_card`, `_ISSUE_RE` 추출)은 `github_refs`가 없으면 `issue_refs`를 explicit 이슈로 읽게 한다.
6. `worklog/search.py`(신규, Build-log `search.py`·`index.py` 패턴):
   - `index_items(c, ids)` / `rebuild(store, scope)`
   - `query(store, scope, text, kinds=None, limit=8, max_tokens=3000) -> {records, related, conflicts, searched}`: 검색어 정리(조사 떼기) → 3글자 이상은 `MATCH`, 3글자 미만은 같은 표의 `scope` 한정 `LIKE` 부분 일치(2.3) → 후보 합치기 → 점수(제목·본문 가중, 짧은 검색어 가중치 낮춤, `superseded`·`cancelled` 감점) → `work_links` 이웃 → 예산 안 Markdown 조립, 조회 범위(`searched`) 기록.
   - 예산 규칙: 결과 Markdown이 `max_tokens`를 넘으면 낮은 점수 기록부터 빼고, 첫 기록 하나가 예산보다 크면 본문을 잘라 "이하 생략"과 원문 위치를 붙인다(Build-log `BudgetTooSmall` 대신 잘라서 돌려줌).
7. 색인 갱신 지점: `core._write_item`, `core._create_item`, `board.edit/cancel/resolve`, `apply.revert`.
8. `reindex` 명령: `python -m worklog reindex-fts --scope`(기존 `worklog/__main__.py`에 추가).

**Dependencies**
- Phase 0.

**Acceptance Criteria**
- 기존 테스트가 프롬프트 이관 뒤에도 모두 통과한다(추출 결과 동일).
- 지침 파일을 고치면 해석 캐시 키가 바뀐다.
- **단문 검색:** 1글자("옷"), 2글자("미러", "UI", "타깃"), 조사 붙은 2글자("미러를") 검색어가 그 단어를 제목·본문에 가진 항목을 돌려준다(복사본 데이터, 기대 항목 목록과 비교).
- **일반 검색:** 3글자 이상과 여러 단어 질의("미러 디스플레이 결정")가 `MATCH` 경로로 관련 항목을 돌려주고, 단문 경로를 추가하기 전과 결과 상위가 같다(회귀).
- **관련성:** 질의 세트 20개(단문 6 포함)에서 기대 항목이 상위 5개 안에 드는 비율을 기록한다. 시작 기준 80% 이상 [가정, 측정 후 조정]. 대체·취소된 항목이 살아 있는 같은 주제 항목보다 위에 오지 않는다.
- **검색 예산:** 모든 결과 Markdown이 `max_tokens`를 넘지 않고, 넘칠 때 빠진 기록 수와 잘림이 `searched`에 표시된다.
- **성능:** 복사본 데이터와, 항목 5,000개로 늘린 합성 데이터에서 단문 질의 1회가 100ms 이내 [가정, 측정 후 조정].
- 색인을 지우고 `reindex-fts`로 다시 만들면 같은 검색 결과가 나온다.

**Verification**
- 신규 `test_prompts.py`(include 해석, 해시 변화), `test_search.py`(1·2글자 단문, 조사 떼기, 3글자 이상 `MATCH`, 두 경로 합치기·중복 제거, 감점, 예산 초과·잘림, 재구축 후 동일 결과, 합성 5,000건 성능), 기존 `test_extracts`·`test_apply` 회귀.
- 실데이터 복사본에서 질의 20개(단문 6 포함) 관련성 기록.

**Out of Scope**
- 새 종류를 실제로 만드는 해석(Phase 2), 화면(Phase 3~).

---

### Phase 2. 산출물 해석과 증분 축적

**Goal**
- 회의록뿐 아니라 기획·조사·발표·피드백 문서를 해석해 산출물 문서와 지식 항목을 만들고, 새 정보가 기존 정보를 보충·대체·해결·충돌하는지 이어 붙인다.

**As-Is**
- 수집·분류는 됨. 추출은 회의 메모만(`parts.extract_pending`, `apply` 조건). 업로드는 항상 회의 메모(`meeting_inputs.save_note`).
- 관계: 할 일·결정만 LLM, 미정 등은 제목 일치, 대체는 결정끼리(`core.py:304`).
- 자동 반영은 회의 메모 페이지만(`apply.auto_apply_due`).

**To-Be**
- 최근 회의 우선 + `explore` 선택으로 해석 대상이 정해진다. 모든 산출물 유형이 같은 `interpret` 경로를 탄다.
- 산출물마다 `artifact` 항목(요약 Markdown) 1개 + 지식 항목들. 항목은 `DERIVED_FROM`으로 산출물에 연결된다.
- `reconcile`이 모든 종류에 적용되고 `alternative`(기존 결정과 다른 가안·조언), `resolve`(미정 해결), 종류를 넘는 대체를 판단한다.
- 승인 정책(확정, 2.2): 출처 종류로 확인 요청하지 않는다. 성숙도에 따라 자동 기록한다.

| 관계 판단 결과 | 처리 |
| --- | --- |
| same | 기록 없음(근거만 추가) |
| new / supplement | 자동 반영 |
| alternative(새 가안·조언 ≠ 기존 결정) | 기존 결정 유지, 새 항목을 `ALTERNATIVE_TO`로 자동 기록 |
| supersede(합의 근거가 있는 명확한 변경) | 자동 반영 + 출처·`SUPERSEDES` + 알림·되돌리기 |
| resolve | 자동 반영(미정 → `resolved`, `RESOLVES`) |
| conflict + `decidable = false` + 중요 대상 | 확인 필요(`needs_discussion`) |
| conflict + 중요하지 않음 | 두 항목 모두 유지, `RELATED_TO` + 충돌 표시(확인 요청 없음) |

- 사람 수정 보호(2.2)는 위 표보다 먼저 적용한다.

**Implementation**
1. 처리 조건 일반화:
   - `parts.extract_pending`: `kind = 'meeting'` 조건을 "해석 대상으로 선택된 부분"으로 바꾼다. 선택 표시는 `source_parts.state`(`collected` → `selected` 값 추가) 또는 `runs`(stage=`explore`)의 결과로 결정 [Phase 2 시작 시 선택].
   - `apply.py`의 `kind = 'meeting'` 조건 8곳을 같은 기준으로 교체(`meeting_parts`, `promote_due`, `pending_pages`, `recent_pages`, `auto_apply_due`, `gone_confirmed_parts` 등).
2. 탐색 선택: `worklog_bridge.groq_explore(catalog)` + `prompts/explore.md`. 카탈로그 생성 `worklog/sections.catalog(store, scope)`(신규, 기존 `source_pages`·`source_sections`·`source_parts` 조회). 결과는 `runs`(stage=`explore`, `result_json`)에 저장해 재사용, 구조 지문이 바뀐 소스만 다시 부른다.
3. 큰 부분 나누기: `parts`에서 4,000토큰 초과 부분을 블록 경계로 나눠 해석(요약하지 않음).
4. 해석: `prompts/interpret.md` 출력 계약(2.4) → `core.normalize_item`(인용 검증 유지) → `artifact` 항목 생성/갱신(`statement_md`=요약 Markdown, `attributes.part_ids`·`source_ids`·`artifact_type`·`maturity`).
5. 관계 판단: `apply._judgment`에서 `LLM_JUDGED_KINDS` 제한 제거, `find_related` + `search.query` 결과를 후보로(prior_check). `prompts/reconcile.md`에 `alternative`·`resolve`·`decidable` 추가. `core.CHANGE_TYPES`에 `alternative`·`resolving` 추가. `core.approve`: `alternative`(새 항목 생성 + `ALTERNATIVE_TO`, 기존 결정 그대로), `resolving`(미정 → `resolved`, `RESOLVES`) 추가, 대체 검사를 "같은 종류끼리"로 완화. `apply.revert`에 두 값의 되돌리기 추가.
6. 승인 정책 함수 `apply.policy(relation, decidable, target)`(신규, 코드): 위 표대로 `auto` / `notify` / `needs_discussion`을 돌려준다. 출처 종류(회의록·문서)를 조건으로 쓰지 않는다. 사람 수정 보호 규칙(2.2)을 먼저 적용한다. 기존 `CONFIDENCE`(0.7) 미만 규칙은 "conflict + decidable=false"로 흡수한다.
6-1. 검증 오류 처리(2.5): `core.normalize_item`의 인용 불일치(지금은 버리고 `part_extracts.dropped`만 셈)를 `runs`에 후보·이유와 함께 기록하도록 바꾼다. `apply._apply_one`에서 관계 판단 실패(`judged_by = fallback`)를 `needs_discussion`으로 만드는 분기를 없애고, 그 항목을 반영하지 않은 채 `runs`(`error_kind = llm_failed|busy`)에 남긴다. 다음 주기에 같은 부분을 다시 처리한다(상한 3회). `worklog/__main__.py`에 `reprocess` 명령 추가.
7. 업로드: 4번 ④ 카드에 "자료 종류" 드롭다운(회의록 / 기획 / 조사 / 디자인 / 개발 / 발표 / 피드백 / 기타) → `meeting_inputs.save_note`가 종류를 `kind_note`·산출물 `artifact_type`으로 남긴다. 해석 경로는 같다.
8. 사라짐 처리: `parts.save_unit_parts`의 `gone` 표시 뒤 이동·병합 대조(블록 ID·`_squash` 인용) → 연결 이전 또는 "출처 없음" 표시.
9. 메뉴 4번 ②의 미리보기 확인 경로: 자동 반영 + 결과 요약으로 바꾸고 채팅 [고치기] 세션은 제거(탭에서 고치기) [제안, 사용자 승인 후].

**Dependencies**
- Phase 1(지침 로더, 새 kind·status·링크, 검색).

**Acceptance Criteria**
- 실데이터 복사본: 선택 탐색된 기획·시장조사 문서마다 `artifact` 항목이 생기고 요약에 출처 링크가 있다.
- 문서에서 나온 결정·요구사항이 `DERIVED_FROM`으로 연결되고, 성숙도(가안·조언·논의·합의)가 원문 인용과 함께 자동 기록된다. 출처가 일반 문서라는 이유만으로 확인 필요가 되지 않는다.
- 기존 결정과 다른 가안은 기존 결정을 바꾸지 않고 `ALTERNATIVE_TO`로 남는다.
- 같은 대상의 명확한 나중 결정이 앞 결정을 `superseded`로 만들고, 답이 미정을 `resolved`로 만든 사례가 각각 1건 이상 생긴다.
- 확인 필요는 "중요 + 판단 불가 충돌"에만 생긴다. 전체 재해석 후 확인 필요 건수를 기록하고, 기존 v1.1 방식(확신 0.7 미만·판정 실패 전부) 대비 줄었는지 비교한다.
- 인용 불일치·계약 위반·판정 실패는 `needs_discussion`이 아니라 `runs`의 오류 기록으로만 남고, 그 후보는 항목이 되지 않는다. 다음 주기나 `reprocess`로 다시 처리되며, 성공하면 정상 항목으로 반영되고 오류 기록은 `succeeded`로 닫힌다. 3회 실패하면 멈추고 관리자 요약 줄에 건수가 보인다.
- 모든 자동 반영 항목이 탭에서 출처·이력을 보이고 되돌리기·복원이 된다(Phase 3에서 화면 확인).
- 같은 내용을 다시 수집해도 LLM을 다시 부르지 않는다(캐시 적중).
- "최종" 제목만 있고 합의 근거 인용이 없는 주장은 `agreed`가 되지 않는다.
- 사람이 고친 항목을 재해석이 덮어쓰지 않는다.

**Verification**
- 단위: 해석 계약 검증, 승인 정책 표(관계별 7가지 경우), `alternative`·`resolving` 적용·되돌리기, 사람 수정 보호, 이동 대조, "최종" 제목만 있는 가안이 `agreed`가 되지 않음.
- 실패·복구 테스트(각각 독립):
  - 인용 불일치: 원문에 없는 인용을 섞은 가짜 추출기 → 그 후보만 항목이 안 되고 `runs`(`quote_mismatch`)에 남음 → 다음 주기 재시도에서 올바른 인용을 내면 항목 생성, 기록 `succeeded`.
  - 계약 위반 JSON: 1회 재시도 → 다음 공급자 → 실패 시 `runs`(`contract`), 부분 상태 `extracted` 유지, 항목·`needs_discussion` 없음.
  - 관계 판단 실패·한도 초과(`Busy`): 항목을 반영하지 않고 `runs`(`llm_failed|busy`), 다음 주기에 재판정, `needs_discussion` 없음.
  - 재시도 상한: 3회 실패 후 멈춤, 관리자 요약 줄 표시, `reprocess`로 수동 재처리하면 다시 시도.
  - 의사결정 충돌: 정상 판정 + `conflict` + `decidable=false` + 중요 대상일 때만 `needs_discussion`이 생김.
- 통합: 복사본 저장소 전체 재해석 → 항목 수·관계 수·확인 필요 수 보고, LLM 토큰 사용량 기록(Phase 1 기준 대비).
- 회귀: 기존 회의록 반영 결과(종류·인용)가 크게 달라지지 않는지 정답지(`extract_eval`) 비교.

**Out of Scope**
- Figma·슬라이드 본문·Clova 녹음 수집, Jira, 정형 WBS 필드 매핑(후속), 메뉴 화면.
- 선택하지 않은 영역의 지연 해석(질문 시 재탐색) — v2.1 제외 확정.

---

### Phase 3. 탭 지식 대시보드

**Goal**
- 기존 탭에서 축적된 지식을 사람이 조회·검색·수정·논리 삭제·복원하고 출처·관계·이력을 확인한다. 수정은 검색과 이후 판단에 반영된다.

**As-Is**
- 탭: 항목 목록·브라우저 내 검색·고치기·반영 취소·확인 필요 답·결정 대체 되돌리기·단계 상세 [확인]. 산출물 문서·관계·복원·서버 검색 없음. 입장은 서명 링크(`board_tab.issue/verify`), 고치기는 권한자만.

**To-Be**
- "산출물 문서" 묶음: 문서 목록(종류·날짜·성숙도), Markdown 본문 보기·고치기, 연결된 지식 항목.
- 항목 패널에 관계 목록(`SUPERSEDES`·`RESOLVES`·`ALTERNATIVE_TO`·`DERIVED_FROM`·`AFFECTS`·`RELATED_TO`)과 이력, 성숙도 표시(가안·조언·논의·합의), 충돌 표시, GitHub 연결(명시/추정 구분), 논리 삭제된 항목 보기와 [복원].
- Phase 2에서 자동 반영된 항목(대체·해결·대안 포함)을 여기서 고치거나 되돌린다. 이것이 "확인 요청을 줄이는 대신 사후에 고친다"는 원칙의 실제 화면이다.
- 검색창이 서버 검색(`search.query`)을 쓴다. 저장하면 색인이 갱신되고, 사람 수정 표시로 재수집 덮어쓰기를 막는다.

**Implementation**
1. `worklog/board.py`: `snapshot`에 `artifacts`(kind=`artifact`)·`links`(항목별)·`deleted`(최근 `cancelled`) 추가. `restore(store, scope, item_id, actor)`(신규: `revisions` 직전 스냅샷의 상태로 되돌림, 기존 `apply._restore` 재사용). `edit`가 `statement_md`(Markdown) 고치기를 허용(이미 있음)하고 색인 갱신.
2. `board_tab.py`: `GET /api/board/search?q=`(서버 검색), `POST /api/board/items/{id}/restore`.
3. `tab/board.html`: 산출물 문서 묶음과 본문 보기(Markdown은 안전한 텍스트 렌더링, HTML 삽입 금지), 관계 목록, 삭제된 항목 보기·[복원], 검색을 서버 호출로.
4. 사람 수정 표시: `revisions.actor`가 사람인 마지막 판을 "사람 수정"으로 보여 주고, Phase 2의 보호 규칙이 읽는다.

**Dependencies**
- Phase 1(색인), Phase 2(artifact 항목·관계).

**Acceptance Criteria**
- 문서의 Markdown 요약을 고치면 바로 서버 검색과 다음 리딩·질문 입력에 고친 내용이 쓰인다.
- 삭제(논리)한 항목은 검색·리딩·질문에서 빠지고, [복원]하면 돌아온다. 이력에 둘 다 남는다.
- 재수집이 사람이 고친 내용을 덮어쓰지 않고 확인 필요로 보낸다.
- 원본 Notion·GitHub에 쓰기 요청이 없다(테스트로 확인).

**Verification**
- `test_board` 확장(복원, 검색 API, 관계, 권한), 사람 수정 보호 통합 테스트.
- 복사본 저장소로 브라우저 확인(데스크톱·좁은 화면·다크 테마).

**Out of Scope**
- 관계를 손으로 새로 만드는 편집기(후속), Teams 탭 SSO(서명 링크 유지).

---

### Phase 4. 프로젝트 리딩 (메뉴 1·2)

**Goal**
- 메뉴 1·2에서 최근 회의와 실제 산출물을 종합해 프로젝트가 어디까지 왔고 무엇이 중요한지 근거와 함께 보여 준다.

**As-Is**
- 메뉴 1: 팀 페이지를 페이지 순서로 잘라 요약(`sections.snapshot`, `_summarize_snapshot`). 최신 문서가 상한 밖으로 밀릴 수 있다.
- 메뉴 2: 저장 항목 목록(LLM 없음), 근거 없음, 최근 회의 기준 아님.

**To-Be**
- 메뉴 1: 산출물 문서 요약(최근 순) + 팀 정보로 "프로젝트 설명"을 만든다. 각 문단에 출처.
- 메뉴 2: 결정됨 / 진행 중 / 불명확 브리핑. 줄마다 근거(회의·문서 이름과 날짜, 탭 링크). 가안·조언·논의·합의를 구분해 표기하고, 결정에 대안이 있으면 "대안 n건"으로 표시. 단계는 한 줄 참고 정보.
- 진행 중 항목은 기존 GitHub 상태 분류(완료 / 진행 중(커밋 있음) / 이슈만 등록 / 이슈 미등록)를 결정·요구사항과 연결해 보여 준다. 연결이 추정이면 "추정"으로 표시하고, GitHub 완료를 "검증 완료"로 표현하지 않는다.
- 메뉴 2 카드 아래 버튼: [💬 질문하기](Phase 5), [🧭 조율 보기](Phase 6).

**Implementation**
1. `prompts/project-info.md`, `prompts/project-leading.md` 작성(common 포함).
2. `main.fetch_project_info`: 입력을 `search`의 산출물 조립(최근 날짜 우선, 예산 4,000)으로 교체. 기존 캐시 방식 유지(입력 지문).
3. `main.fetch_team_status`: 저장 항목 목록 대신 브리핑 호출. 입력 = 최근 회의 산출물(날짜 속성 기준 최근 N) + 성숙도별 결정·요구사항(대안 수 포함) + 미정·확인 필요 + 작업 사실(기존 `_classify_tasks_by_github_state` 결과를 그대로, 항목의 `github_refs`로 명시/추정 연결 표시). 출력 계약의 근거 id를 코드가 실제 출처 이름·날짜·탭 링크로 바꿔 표시(LLM이 쓴 위치를 그대로 믿지 않음).
3-1. `prompts/project-leading.md`에 "GitHub 완료(이슈 종료·PR 병합)는 기능 검증이 아니다", "추정 연결은 추정으로만 말한다"를 명시한다.
4. 브리핑 캐시: 입력 지문이 같으면 재호출하지 않는다.
5. LLM 실패 시 기존 목록형 메뉴 2로 대체 표시.

**Dependencies**
- Phase 2(산출물·지식), Phase 1(검색·지침). Phase 3은 권장(정정 가능).

**Acceptance Criteria**
- 메뉴 2의 모든 줄에 근거 출처·날짜가 있고, 근거 id가 저장본에 실제로 있다(없는 id는 표시하지 않음).
- 가안·조언·논의·합의가 섞이지 않는다(복사본 데이터 수동 검토 + 계약 검사).
- 진행 중 항목의 GitHub 상태가 기존 분류와 같고, 명시/추정 연결이 구분되며, "검증 완료" 표현이 없다.
- 메뉴 1이 최신 기획 내용을 포함한다(최종 기획 문서가 상한 밖으로 밀리지 않음).
- 같은 입력으로 두 번 열면 두 번째는 LLM을 부르지 않는다.

**Verification**
- 단위: 근거 id 검증, 실패 대체, 캐시. 통합: 명세 시나리오 (a) 최근 회의 → PRD·조사·피드백 연결 현황.
- 토큰: 메뉴 1·2 한 번당 입력·출력 기록.

**Out of Scope**
- 다음 행동 제안(Phase 6), 질문 응답(Phase 5).

---

### Phase 5. 질문하기 (자연어 확인)

**Goal**
- 메뉴에서 [질문하기]로 들어가 자연어로 물으면, **이미 축적된 지식과 검색 색인**, 그리고 필요한 GitHub 작업 상태로 **확인한 작업 상태·관련 문서·출처·조회 범위·미확인 사항**을 답한다.

**As-Is**
- 질문 입구 없음. 세션 패턴은 있음(`upload_sessions`, `source_add_sessions`). GitHub는 이슈·커밋 조회와 기존 상태 분류(`_classify_tasks_by_github_state`)가 있음.

**To-Be**
- 메인 메뉴에 [💬 질문하기] 버튼(또는 메뉴 2 아래) → 다음 메시지를 질문으로 받는 세션(10분).
- 서버: 질문 → `search.query`(축적 지식) → 질문이 구현·작업 상태에 관한 것이면 GitHub 조회(기존 상태 분류 + 연결된 이슈·PR 상태 + PR 변경 파일 경로) → 예산 안 조립 → `qa` 호출 → 답 카드.
- **조회 범위의 정직성:** 해석하지 않은 Notion 문서를 질문 때 새로 탐색하지 않는다. 답의 `searched_scope`는 코드가 실제로 본 것(색인 대상 지식, 조회한 GitHub 범위)으로만 채우고, "해석 대상이 아니었던 자료는 확인하지 않음"을 밝힌다. 확인하지 않은 자료를 조회했다고 쓰지 않는다.
- **GitHub 사실의 구분:** 이슈 종료·PR 병합·변경 파일 경로까지만 사실로 말한다. 코드 분석·테스트 실행·시연 검증은 하지 않으므로 "동작한다·검증됐다"고 말하지 않고, 그 부분은 미확인으로 남긴다.
- 답 카드: 확인한 작업 상태, 관련 결정·요구사항·문서(근거 링크: Notion 문서·섹션, GitHub Issue·PR·파일 경로), 명시/추정 연결 표시, 찾은 범위, 찾지 못한 것, 다음 행동 버튼 [담당자에게 확인 요청 안건으로][다음 회의 안건으로].

**Implementation**
1. `prompts/qa.md`(common 포함). 출력 계약 2.4.
2. `main`: `qa_sessions` + `_handle_text` 분기, 메뉴 버튼 추가. 권한: 누구나 질문 가능.
3. `worklog/search.py`: `query`에 질문 유형 힌트(구현·결정·근거) 없이 동작, 결과에 `searched`(본 소스 목록) 포함.
4. GitHub 조회 함수(신규, `main.py`의 `_github_get_cached` 재사용, Phase 7 작업물 목록에서도 씀): `_github_prs(repo, state)`(목록·병합 여부), `_github_pr_files(n)`(변경 파일 경로만), 기존 커밋 목록. 상태는 기존 `_classify_tasks_by_github_state` 결과를 그대로 쓰고, 항목의 `github_refs.basis`로 명시/추정을 구분한다. 코드 검색(`/search/code`)·파일 본문 조회는 넣지 않는다.
5. 호출 흐름(2.4 "질문 한 건의 호출 흐름"): 1차 `qa` → `intent=coordination`이면 2차 `coordination`(Phase 6 전에는 2차 없이 "조율 보기는 준비 중"과 1차 답을 보임), 아니면 `need` 검증 후 2차 `qa`, 둘 다 아니면 1차로 끝. 2차 결과의 `need`·`intent`는 무시한다. 질문 핸들러에 호출 계수기를 두고 2를 넘으면 예외로 막는다.
5-1. `need` 검증(코드): 이미 해석된 항목 id의 근거 인용 위치 원문과 GitHub 상태 사실만 허용, 최대 5개. 해석 대상이 아닌 영역·알 수 없는 id는 `unknowns`에 "확인하지 않음"으로 기록한다.
6. 답 카드 렌더링: 근거 id → 실제 위치 링크로 코드가 변환. 다음 행동 버튼은 "안건" 항목(kind=`open`, `attributes.agenda=true`) 생성으로 연결(사람이 누를 때만).

**Dependencies**
- Phase 1·2(지식·검색), Phase 4(지침 공통·근거 표시 방식). GitHub 조회는 저장소 확인 필요(없으면 GitHub 범위를 "조회 안 함"으로 표시).

**Acceptance Criteria**
- 명세 시나리오 (b) "저번 요구사항 결정됐어?": 결정·대체·해결 관계를 근거와 함께 답하고, 없으면 "어디까지 찾았는지"를 말한다.
- 명세 시나리오 (c) "이 기능 구현됐어? 어디에 있어?": 관련 결정·요구사항, 연결된 이슈·PR 상태, PR 변경 파일 경로를 근거로 답한다. "GitHub에서 완료"와 "기능 검증"을 구분하고 검증은 미확인으로 둔다. 근거가 없으면 미구현으로 단정하지 않는다.
- 답의 `searched_scope`에 실제로 보지 않은 자료가 없다(코드가 채운 값과 비교하는 테스트). 해석 대상이 아니었던 문서가 있으면 그 사실을 밝힌다.
- 명시 연결과 추정 연결이 답에서 구분된다.
- 답의 근거 링크가 모두 실제로 열린다(존재 검사).
- 질문 1회당 LLM 호출 2회 이하(분기 3가지 모두 테스트), Notion API 호출 0회.
- `QaResult` 계약 위반(필드 누락·잘못된 `intent`·`need` 6개 이상)은 코드가 막고, 위반 시 재시도 없이 "답을 만들지 못함 + 조회 범위" 카드가 나온다.
- 2차 결과에 `need`·`intent=coordination`이 있어도 3차 호출이 일어나지 않는다.

**Verification**
- 질문 세트 20개(결정·구현·근거·찾을 수 없는 것 포함) 기대 답 정답지와 비교(사람 판정), 토큰·지연 기록.
- 단위: 세션, 근거 링크 변환, GitHub 조회 모킹, 계약 검증(정상·위반), 호출 흐름 분기별 호출 수(1회: 바로 답 / 2회: need / 2회: coordination / 2회: 2차가 다시 need를 내도 멈춤), `need` 검증(해석 안 한 영역·없는 id 거르기).

**Out of Scope**
- 자연어로 외부 시스템을 바꾸는 명령, 도구 호출형 에이전트, Jira·Figma 조회.
- 질문 시 해석하지 않은 Notion 문서 재탐색, 실시간 외부 검색.
- 코드 분석·코드 검색·테스트 실행·시연 검증.

---

### Phase 6. 팀 조율

**Goal**
- 회의에서 정한 것과 현재 작업 의존성을 보고 **지금 함께 정할 것 / 나중에 정할 것 / 역할 간 조율 / 병렬 진행**을 제안한다. 사용자는 카드에서 확인·안건 등록·보류를 고른다. 제안은 자동으로 결정이 되지 않는다.

**As-Is**
- 조율 기능 없음. 미정·확인 필요·`AFFECTS` 링크·할 일 담당·GitHub 상태 사실은 있다.

**To-Be**
- 메뉴 2 브리핑 아래 [🧭 조율 보기] → 조율 카드: 항목마다 이유(막고 있는 작업·관련 역할)와 근거, 참고한 방법론 이름, 버튼 [다음 회의 안건으로][보류][결정 기록하기(사람이 내용 입력)].
- 판단 기준은 `coordination.md`(기본 지침). 상황에 맞는 방법론 레퍼런스만 함께 넣는다: Scrum(Inspect & Adapt), RAID, Product Discovery(가치·사용성·실현 가능성·사업성), Design Sprint(문제 정의·프로토타입·검증), Spike(짧은 기술 탐색).
- 고정 프로세스를 강요하지 않는다. 팀원이 다른 방식을 제안하면(예: "만들면서 QA") 질문하기에서 그 대안과 현재 안을 레퍼런스의 적용·비적용 조건으로 비교한다.
- 실시간 외부 검색은 하지 않는다. 레퍼런스는 저장소의 Markdown만 쓴다.

**Implementation**
1. `prompts/coordination.md`(기본 지침: 지금/나중/역할 간/병렬 판단 기준, 대안 비교 방법, 강요 금지, 근거 규칙 포함).
2. `prompts/methods/` 6개 파일(2.4 트리). 각 방법론 파일은 공통 5칸(적용 조건 / 핵심 판단 질문 / 예상 산출물 / 비적용 조건 / 원문 출처), 파일당 약 400토큰 이내. 출처는 작성할 때 원문을 확인해 적는다(후보: Scrum Guide 2020, SVPG "Four Big Risks", GV Design Sprint, Extreme Programming의 spike 설명. RAID는 단일 공식 원문이 없어 사용한 출처를 그대로 적는다).
3. 레퍼런스 선택(코드, 토큰 절약): `methods/index.md`(한 줄씩, 항상 포함) + 신호에 따라 최대 2개 본문을 넣는다. 신호 예: 가설·미정 결정이 많음 → Product Discovery·Spike, 의존·충돌·리스크 → RAID, 기능 범위·와이어프레임 엇갈림 → Design Sprint, 회의 주기·회고 → Scrum. 메뉴 경로에서만, 모델이 다른 레퍼런스가 필요하다고 답하면(`need_method`) 그 레퍼런스를 넣어 1회 더 부른다. 출력은 `CoordinationResult` 계약(2.4)으로 검증한다.
4. 입력 조립(코드): 미정·확인 필요·충돌·대안 항목, 관련 할 일과 담당·역할(`people.team`), `AFFECTS`·`DERIVED_FROM`·`ALTERNATIVE_TO` 관계, 작업 사실(기존 GitHub 상태 분류), 단계 후보(참고).
5. `confirm_flow`(또는 신규 `coordination.py`): 카드 생성, 버튼 처리 → 안건 항목(`attributes.agenda=true`)·보류(`attributes.deferred=true`)·결정 기록(사람이 입력한 문장으로 `decision` 생성, `maturity=agreed`, 근거=사람 입력).
6. 질문하기의 대안 검토: 1차 `qa`가 `intent=coordination`을 내면 Phase 5의 분기가 2차로 `coordination`을 1회 부른다(조율 입력 + 코드가 고른 레퍼런스 + 질문과 1차 결과). 이 경로에서는 `need_method`를 무시해 질문당 2회 상한을 지킨다. 결과는 `compared_alternatives`로 대안과 현재 안을 비교해 보인다.
7. 안건 목록 보기: 메뉴 2 브리핑과 탭에 "다음 회의 안건" 묶음.

**Dependencies**
- Phase 4(브리핑), Phase 5(검색 조립·근거 표시).

**Acceptance Criteria**
- 명세 시나리오 (d): 지금/나중 분리와 역할별 조율안이 근거와 참고 방법론 이름과 함께 나온다.
- 방법론 파일 5개가 모두 공통 5칸을 갖추고 출처가 적혀 있다.
- 한 번의 조율 호출에 들어간 레퍼런스 본문이 2개 이하다(로그로 확인). 전체 5개를 넣지 않는다.
- 메뉴 경로 조율은 최대 2회(`need_method`일 때만 2회), 질문 경로 조율은 질문 전체 2회 안에 끝난다.
- `CoordinationResult` 계약 위반은 코드가 막고 기존 브리핑으로 대체 표시한다.
- 팀원이 낸 대안("만들면서 QA" 등)을 현재 안과 비교한 답이 나오고, 특정 프로세스를 강제하는 문장이 없다.
- 카드에서 어떤 버튼도 누르지 않으면 결정·작업이 생기지 않는다.
- [결정 기록하기]는 사람이 쓴 문장만 결정으로 남긴다.

**Verification**
- 복사본 데이터 시나리오 검토, 버튼별 단위 테스트, 자동 확정이 없음을 확인하는 테스트.
- 레퍼런스 선택 규칙 단위 테스트(신호별 선택 결과, 상한 2개), 지침 로더가 선택된 파일만 포함하는지 확인.
- 호출 수 테스트: 메뉴 경로(need_method 없음 1회 / 있음 2회), 질문 경로(`intent=coordination` → 정확히 2회, `need_method`가 와도 3회째 없음).
- 조율 호출의 입력 토큰을 레퍼런스 포함 전후로 기록.

**Out of Scope**
- 일정 자동 조정, 개인별 업무 코칭, 게이트 기반 진행 강제, 실시간 외부 검색, 방법론 레퍼런스 자동 갱신.

---

### Phase 7. 업무 연동 (GitHub)

**Goal**
- 개발 업무는 AI가 GitHub Issue 분해·담당·완료 기준을 **제안**하고, 사람이 고치고 승인한 것만 등록·배정한다. 작업물은 '작업물 공유하기'에서 사람이 골라 공유한다. 완료 공유가 저장본과 맞는다.

**As-Is**
- GitHub 쓰기: 배정(PATCH), Project Status(GraphQL). 이슈 생성 없음. 4번 ①은 Project만 바꾸고 저장본 미반영. 할 일 ↔ 이슈 연결은 `#번호` 문자열만.

**To-Be**
- 결정·요구사항(탭 또는 질문 답)에서 [이슈 제안] → 분해 카드(이슈 제목·본문·완료 기준·담당 후보) → 카드에서 고치기·빼기 → [등록] → `POST /issues` + 배정 → 저장본 할 일과 `github_refs`(explicit) 연결.
- 4번 '작업물 공유하기': 메뉴에 들어오면 그때 GitHub API로 최근 PR·커밋 목록을 조회해 선택 카드로 보인다 → 팀원이 고른다 → 관련 맥락(연결 이슈, 관련 결정·요구사항, 변경 파일 경로)을 `work-share`로 정리한 미리보기 → [공유][고치기][취소] → [공유]를 누를 때만 Teams에 보내고 `artifact` 항목(종류: 개발 산출물, 출처: PR·커밋 URL)으로 지식에 남긴다.
- 기존 4번 ④ 파일·텍스트 올리기는 그대로 둔다.
- 4번 ① 완료 공유: Project Done과 함께 연결된 할 일 상태를 `done`으로(`supplement`, 이력). "GitHub 완료"로만 기록하고 검증 완료로 표시하지 않는다.

**Implementation**
1. 사전 확인(사용자): 대상 저장소·Project·토큰 권한(issues: write, project). 테스트 저장소에서 먼저 검증.
2. `prompts/issue-breakdown.md`, `prompts/work-share.md`.
3. `main`: `_github_post(repo, path, body)`(신규), 이슈 생성 함수, 실패 시 부분 성공 처리(만든 이슈 목록 보고, 저장본은 성공한 것만 연결).
4. 승인 카드: Adaptive Card 입력(제목·본문 텍스트, 담당 드롭다운, 포함 체크) → 제출 데이터 검증(서버) → 권한자만 등록.
5. `share_completed_work`: Done 처리한 이슈와 `github_refs`(또는 기존 `issue_refs`)가 같은 할 일을 `supplement`로 `done` 처리.
6. 작업물 공유하기(`confirm_flow.SUBMENU`에 추가):
   - 진입 시 코드가 Phase 5의 `_github_prs`·커밋 목록 조회로 최근 N개(기본 PR 10·커밋 10)를 가져와 드롭다운 카드로 보인다. LLM은 부르지 않는다. 조회 실패·권한 없음이면 이유를 보이고 4번 ④ 안내.
   - 선택 → `work-share` 호출 1회(선택한 PR·커밋의 제목·본문·변경 파일 경로·연결 이슈 + 관련 지식 검색 결과) → 미리보기 카드.
   - [공유]를 눌러야 Teams 메시지 전송 + `artifact` 항목 생성. [취소]면 아무것도 남지 않는다. 자동 공유·브랜치 생성은 없다.

**Dependencies**
- Phase 1(지식 모델), Phase 5(GitHub 조회), 저장소·권한 확인.

**Acceptance Criteria**
- 명세 시나리오 (e): 승인한 이슈만 등록되고, 고친 내용 그대로 등록된다. 거절·미승인은 아무것도 만들지 않는다.
- 등록 실패 시 저장본이 바뀌지 않고 실패 이유가 카드에 남는다.
- 완료 공유 후 메뉴 2·3에서 해당 할 일이 "GitHub 완료"로 보인다(검증 완료 표현 없음).
- 작업물 공유하기: 메뉴 진입 때만 GitHub 목록을 조회하고, 사람이 고르고 [공유]를 누른 것만 공유된다. [취소]·무응답이면 공유도 지식 기록도 없다. 자동 공지·자동 공유가 없다.
- 작업물 하나를 공유할 때 LLM 호출은 1회다(목록 조회는 코드).

**Verification**
- GitHub API 모킹 단위 테스트(성공·부분 실패·권한 오류·목록 비어 있음), 테스트 저장소 수동 검증(실제 이슈 생성은 테스트 저장소에서만), 회귀(배정·Project 상태).
- 작업물 공유 흐름 테스트: 목록 → 선택 → 미리보기 → 취소(아무것도 안 남음) / 공유(메시지 + 지식 1건).

**Out of Scope**
- 브랜치 자동 생성·자동 병합, Jira, 비개발 업무의 이슈 강제, GitHub Actions 기반 변경 수집·목록화, 모든 커밋 자동 공지.

---

## 4. 전체 검증과 완료 기준

### 4.1 요구사항 ↔ Phase

| 명세 | Phase |
| --- | --- |
| G-1 프로젝트 리딩 | 0, 4 |
| G-2 맥락 축적 | 1, 2 |
| G-3 자연어 확인 | 5 |
| G-4 팀 조율 | 6 |
| G-5 업무 연동 | 7 |
| G-6 지식 대시보드 | 3 |
| C-2 탐색·증분·가안/확정·사람 수정 보호 | 2, 3 |
| C-3 기능별 지침 | 1(로더·이관), 각 Phase(지침 파일), 6(방법론 레퍼런스) |
| C-4 근거 위치·조회 범위·구현 사실 구분 | 4(진행 중 GitHub 상태), 5 |
| C-5 승인 후 쓰기·작업물 선택 공유 | 7 |
| 개정 ① 작업물 목록 조회 → 선택 → 확인 → 공유 | 7(조회 함수는 5) |
| 개정 ② 성숙도별 자동 기록·대안 기록·판단 불가 충돌만 확인 | 2(정책), 3(사후 수정·복원) |
| 개정 ③ 초기 선택 탐색 + 증분, 질문 시 재탐색 없음 | 2, 5 |
| 개정 ④ coordination + 방법론 레퍼런스 선택 제공 | 6 |
| 개정 ⑤ 기존 GitHub 상태 분류 재사용, 완료 ≠ 검증 | 4, 5, 7 |
| C-6 탭 CRUD·원본 보호 | 3 |
| B-5 시나리오 (a)~(f) | (a) 4 · (b)(c) 5 · (d) 6 · (e) 7 · (f) 3 |

### 4.2 통합 테스트 시나리오 (복사본 저장소)

1. 새 팀 연결 → 최근 회의 3건 해석 → `explore`로 기획·조사 문서 선택 → 해석 → 메뉴 1·2 브리핑에 근거가 붙는다.
2. 회의록에 결정 변경을 추가 → 변경 감지 → 대체 관계 → 메뉴 2·탭 이력에 반영, 질문 "결정 바뀌었어?"에 근거와 함께 답. 기획 문서에 다른 가안을 추가 → 기존 결정은 그대로, 대안으로만 기록되고 확인 요청이 없음.
3. 질문 "X 구현됐어?" → 연결된 이슈·PR 상태와 변경 파일 경로로 답하고 "GitHub 완료 ≠ 검증"을 표시, 근거 없는 기능은 "찾은 범위(축적 지식·GitHub)"와 함께 미확인으로 답. 이 질문 동안 Notion 호출 0회.
4. 조율 보기 → 참고 방법론(최대 2개)과 함께 조율안 → 안건 등록 → 메뉴 2·탭에 안건 표시, 결정은 생기지 않음. 질문하기로 "만들면서 QA는 어때?" → 현재 안과 비교.
5. 이슈 제안 → 카드에서 1개 빼고 등록 → 테스트 저장소에 나머지만 생성·배정 → 할 일 연결. 작업물 공유하기 → PR 목록 → 하나 선택 → 미리보기 → 취소(흔적 없음) / 공유(메시지 + 지식).
6. 탭에서 요약 수정·항목 삭제·복원 → 검색·다음 브리핑에 반영 → 재수집 후에도 사람 수정 유지.

### 4.3 기존 기능 회귀

- 전체 단위 테스트 OK(현재 210개 기준, 각 Phase에서 갱신).
- 메뉴 1~4, 설정, 별칭, 권한, 처음 등록, 초기 단계 설정·선택 카드, 단계 넘기기·Pivot, 자동 반영·되돌리기, 결정 대체 알림, 사라진 부분 확인, 담당 맡기·이슈 배정, Project Done.
- Playground 확인은 복사본 저장소로(`WORKLOG_STORE`).

### 4.4 성능·Groq 사용량

- 기준선(Phase 1 시작 전): 일 사용량(`llm_usage`), 기능별 호출 수·입력·출력 토큰(`metrics`).
- Phase마다: 기능별 평균·최대 토큰, 하루 몫 대비 사용률, 캐시 적중률, 메뉴 응답 시간(1·2·질문하기 목표: 첫 응답 15초 이내 [가정]).
- 전체 재해석(Phase 2)의 총 토큰과 소요 주기 수를 기록한다. 대상은 최근 회의 + explore가 고른 영역뿐이다(나머지는 해석하지 않음). 하루 몫 안에서 끝나지 않으면 최근 회의 → explore 선택 순서로 나눠 처리한다.
- 개정 후 기대 효과(측정해서 확인): 질문당 호출 ≤ 2·Notion 호출 0, 조율 호출당 레퍼런스 ≤ 2개(약 1,200토큰 절약), 작업물 목록은 LLM 0회·공유 1건당 1회, 확인 필요 건수 감소(v1.1 방식 대비).

### 4.5 주요 기술 리스크와 미확정 사항

| 항목 | 상태 | 대응 |
| --- | --- | --- |
| GitHub 대상 저장소·권한(`n8nTest`, Project #4) | [미확인] | Phase 7 전 사용자 확인. 그전 질문하기는 조회만, 범위 표시 |
| 공급자 공식 한도·`reasoning_effort` 같은 출력 절감 옵션 | [미확인] | Phase 1에서 측정 |
| 문서 해석 총비용(문서 부분 100여 개, 3만 자 이상 단일 부분 포함) | 추정 불가 | 최근 회의 우선 + explore 선택 해석만(선택 밖은 해석 안 함), Phase 2에서 실측 |
| 검색 정확도(한국어 trigram, 짧은 단어) | `MATCH`는 3글자 미만 0건 [확인] | 단문은 같은 색인의 범위 한정 부분 일치로 보완(2.3). 관련성·성능은 Phase 1 질의 세트로 측정, 기준 미달이면 대안 재검토 |
| Notion 소스 혼재(가안·최종 표기) | 확인됨(실데이터) | 성숙도 규칙 + 인용 필수 + 대안 기록, 판단 불가한 중요 충돌만 확인 필요 |
| 자동 기록 오판(가안을 합의로, 대체를 대안으로) | 설계상 위험 | 인용 없는 `agreed` 금지, 모든 자동 반영의 탭 수정·되돌리기·복원(Phase 3), Phase 2 정답지 비교 |
| "중요 충돌" 기준 | 초기 규칙(결정·요구사항 + 연결 있음) | Phase 2에서 확인 필요 건수와 놓친 충돌을 보고 조정 |
| 선택하지 않은 문서에 답이 있는 질문 | 설계상 한계(v2.1 확정) | 답에서 "해석 대상이 아니었음"을 밝히고 다음 회의 안건·담당자 확인으로 연결. 해당 소스는 탭이나 소스 추가로 사람이 해석 대상에 넣을 수 있게 하는지는 Phase 2 구현 시 기존 소스 추가 흐름으로 확인 |
| 방법론 레퍼런스 출처 | 작성 시 원문 확인 필요 | Phase 6에서 출처 고정, 실시간 검색 없음 |
| 작업물 목록 조회의 GitHub 호출 한도 | [미확인] | 메뉴 진입 때만 호출 + 기존 캐시(`_github_get_cached`) |
| 구현 여부 질문의 한계 | v2.1 확정 | PR 병합·변경 파일 경로까지만. 동작·검증 여부는 항상 미확인으로 답 |
| 재수집과 사람 수정 충돌 | 설계 | 사람 수정 보호 규칙(Phase 2·3 테스트) |
| 인용 검증 실패 처리 | 기존 동작: 버리고 건수만 셈(`part_extracts.dropped`) [확인] | v2.1: 기술 오류로 `runs`에 후보·이유 기록 → 자동 재시도(상한 3회)·`reprocess` 명령. 항목은 만들지 않고 `needs_discussion`과 섞지 않음(2.5) |
| 관계 판단 실패의 처리 | 기존 동작: `needs_discussion` [확인, `apply._apply_one`] | v2.1: 기술 오류로 재처리, 확인 요청 아님 |
| 탭 CRUD 복구 | 기존: 반영 취소만, 복원 없음 | Phase 3 복원 |
| 게이트 later/skipped 지정 경로 | Phase 0 이후 없음 | 알려진 한계로 기록, 의미 재정의와 함께 후속 |
| 단계 직접 변경 | 없음 | 후속(초기 설정 선택 카드 재사용 후보) |
| 정형 WBS 필드 매핑(BindingPlan 전용 표) | 이전 논의에서 보류 | v2.1 범위 밖. 합의 안 된 WBS는 현재 상태·지연 근거로 쓰지 않음 |
| Jira·Figma·슬라이드 본문·Clova | 미구현 | 범위 밖. Clova는 텍스트 업로드(4번 ④)로 안내 |
| 테스트 데이터 원상복구 | 미완 | Phase 0 |
| 이 세션 미커밋 변경과 PR #6 범위 | 미정 | Phase 0에서 사용자 결정 |

### 4.6 개정 이력

| 날짜 | 변경 | 영향 Phase | 의존성 |
| --- | --- | --- | --- |
| 2026-11-04 | 최초 작성 | 0~7 | — |
| 2026-11-04 | ① GitHub 작업물: Actions 수집 제외, 작업물 공유하기 진입 시 API로 PR·커밋 목록 조회 → 선택 → 정리 → 확인 후 공유, 자동 공유·브랜치 생성 없음 | 2.6, 7 | 7이 5의 GitHub 조회 함수를 재사용(기존 순서 유지) |
| 2026-11-04 | ② 문서 해석: 출처 종류로 확인 요청하지 않음, 성숙도별 자동 기록, 다른 가안은 `ALTERNATIVE_TO`, 명확한 변경은 `SUPERSEDES`, 판단 불가한 중요 충돌만 확인 필요 | 2.2, 2.3, 2.4, 2, 3 | 변경 없음 |
| 2026-11-04 | ③ Notion 탐색: 초기 최근 회의 + 메타구조 선택 탐색, 이후 증분만, 질문 시 재탐색(지연 해석) 제외, 조회 범위를 실제로 본 것으로만 표시 | 2.1, 2.2, 2, 5 | 변경 없음 |
| 2026-11-04 | ④ 팀 조율: `coordination.md` + `methods/` 레퍼런스 5종(공통 5칸), 코드가 최대 2개 선택, 대안 비교, 실시간 외부 검색 없음 | 2.4, 2.5, 6 | 변경 없음 |
| 2026-11-04 | ⑤ GitHub 진행상황: 기존 상태 분류 재사용, 지식과 연결, 명시/추정 구분(`github_refs`), 완료 ≠ 검증, 코드 분석·테스트·시연 검증 제외 | 2.2, 2.3, 2.6, 1, 4, 5, 7 | 변경 없음 |
| 2026-11-04 | 정합성 보완 ⑥ 단문 검색: trigram `MATCH`의 3글자 미만 0건 확인, 같은 색인에서 3글자 미만은 범위 한정 부분 일치로 보완, 단문·일반·관련성·예산·성능 기준 추가 | 2.3, 1, 4.5 | 변경 없음 |
| 2026-11-04 | 정합성 보완 ⑦ 출력 계약: `QaResult`(`intent`, `need` 포함)·`CoordinationResult` 명시, 질문 한 건 호출 흐름과 2회 상한(2차 결과의 `need`·`intent` 무시, 질문 경로 `need_method` 무시) | 2.4, 2.5, 5, 6 | 변경 없음 |
| 2026-11-04 | 정합성 보완 ⑧ 오류와 충돌 분리: 인용·계약·판정 실패는 `runs` 오류 기록 + 재처리(자동 3회·`reprocess`), 항목 미생성. `needs_discussion`은 판단 불가한 중요 결정 충돌만 | 2.5, 2, 4.5 | 변경 없음 |
