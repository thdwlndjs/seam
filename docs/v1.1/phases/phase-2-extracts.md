# Phase 2 부분별 추출

브랜치 `feat/part-extracts` · 앞선 조건 [Phase 1](phase-1-md-parts.md) · 다음 [Phase 3](phase-3-confirm-apply.md)

이 phase가 끝나면, 회의 메모 부분마다 결정·할 일·미정·피드백·인사이트가 한 번만 추출되어 저장된다. 메뉴 2번은 그 결과만 읽어 Groq를 부르지 않는다. worklog 반영과 확인 흐름은 아직 없다. 추출 결과는 저장만 하고 메뉴 2번에 보인다.

## 읽을 것

- [spec.md](../spec.md) P1-4 추출, P1-8 Groq 대기열, P1-9 메뉴 1·2번, 데이터 모델(버전 6), 계측

## 작업

- [ ] `src/server/worklog_bridge.py`: 추출 호출. 출력 JSON은 spec P1-4(`task`에는 `role_hint` 포함). 프롬프트 버전 상수를 둔다.
- [ ] `src/server/worklog/db.py`: 마이그레이션 6(`part_extracts`).
- [ ] `src/server/worklog/parts.py`: 추출 결과 저장·조회, 코드로 항목 합치기, `quote` 검사.
- [ ] 새 `src/server/groq_queue.py`(가칭): 우선순위 3단계, 응답 헤더로 속도 조절, 백그라운드 하루 몫(70%), 429 처리, 대기 건수.
- [ ] `src/server/main.py`
    - `_worklog_loop`가 대기열로 종류 판정·추출을 처리한다.
    - 메뉴 2번: `_classify_meeting_notes` 대신 저장된 추출 결과(`part_extracts`)를 읽는다. 아직 확인 흐름이 없으므로 제목 옆에 "(확인 전)"을 붙인다(spec P1-9). 화면 구성은 지금과 같다.
    - 메뉴 1번: "주요 문서"(document 부분, 최대 5개).
    - 기준 시각: `source_pages.checked_at` 최댓값. `checked_at`은 확인할 때마다 갱신한다.
- [ ] 1~3차 멘토링 추출 초안을 만들어 송지원에게 넘긴다(정답지 작업은 Phase 3에서 [고치기]로 한다).
    - `src/server`에서 `python extract_draft.py --scope <팀 키>`: 판정·추출을 끝까지 하고(대기열 한도 안에서) `devTools/extract-draft-<시각>.md`에 초안을 쓴다. 초안은 실제 회의 내용이라 커밋하지 않는다.

## 먼저 쓸 테스트

| ID | 테스트 |
| --- | --- |
| A5 | 같은 (내용 해시, 프롬프트 버전)은 재시작해도 다시 추출하지 않는다 |
| A6 | 메뉴 2번 응답의 `groq_calls = 0`(계측 로그로 확인) |
| A12 | `quote`가 부분 md에 없는 항목은 버린다 |
| A21 | 대기열: 순위 1이 순위 2·3보다 먼저 처리되고, 백그라운드는 하루 몫을 넘지 않는다. 429에서 순위 2·3은 다음 주기로 넘어간다 |
| A22 | `document` 부분은 추출하지 않는다 |

## 완료 조건

- [ ] A5, A6, A12, A21, A22 통과, A18 통과
- [ ] 실제 Notion: 1~3차 멘토링 추출 초안에 1차 멘토링 피드백(예: "식상한거 하지말고 흥미를 끌만한 주제로")이 있고, 기획서 내용은 없다(성공 기준 2)
- [ ] 첫 추출의 시간·토큰·429 여부를 PR에 남긴다

## 이 phase에서 하지 않는 것

- worklog 반영, 미리보기·확인(Phase 3)
- 메뉴 2번 6개 분류(Phase 5)
