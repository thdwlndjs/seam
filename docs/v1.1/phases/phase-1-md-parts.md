# Phase 1 md 수집과 부분 분할

브랜치 `feat/notion-md-parts` · 앞선 조건 [Phase 0](phase-0-baseline.md) · 다음 [Phase 2](phase-2-extracts.md)

이 phase가 끝나면, Notion 페이지가 md로 한 번에 수집되고 최상위 블록 단위 부분으로 저장된다. 각 부분에는 종류(회의 메모·문서·링크)가 붙는다. 1차 멘토링의 기획서는 회의 메모와 따로 떼어진다. 추출과 메뉴 변경은 아직 하지 않는다.

## 읽을 것

- [spec.md](../spec.md) P1-1 md 수집, P1-2 부분 분할과 블록 대조, P1-3 종류 판정, P1-10 다시 수집과 이전, 데이터 모델(버전 5)

## 작업

- [ ] 새 `src/server/notion_md.py`: `fetch_markdown(client, page_id)`. 버전 헤더, 잘린 응답 이어 받기(최대 20회), 실패 시 `notion_read._block_text`로 대체.
- [ ] 새 `src/server/worklog/parts.py`: `top_level_blocks(md)`, `group_parts(blocks)`, `align_blocks(blocks, top_blocks)`, 부분 저장·조회. 최상위 줄 규칙은 Phase 0의 `tests/test_fixtures.py`의 `top_level_lines`와 같다(그 함수를 `parts.py`로 옮겨 테스트가 그것을 쓰게 한다).
- [ ] `src/server/worklog/db.py`: 마이그레이션 5(`source_parts`).
- [ ] `src/server/notion_map.py`
    - `_collect_section`에서 하위 페이지·DB 행 본문을 md로 받아 부분을 만든다. 수정 시각 비교(`source_units`)는 그대로.
    - `UNREADABLE`에서 `link_to_page`를 빼고, `scan_page`가 대상 페이지를 하위 페이지처럼 넣는다.
- [ ] `src/server/worklog_bridge.py`: 종류 판정 호출(`groq_classify_parts`). 입력·출력 JSON은 spec P1-3.
- [ ] 기존 경로를 그대로 둔다. 지금처럼 `sources`와 `work_records`에도 쓰고, 부분(`source_parts`)은 함께 저장한다. 메뉴가 부분을 읽기 시작하는 것은 Phase 2부터다.
- [ ] `src/server/worklog/__main__.py`: `reindex --scope` 명령. 저장소 백업, md 다시 수집, "판정 못 함" 섹션 재판정.

## 먼저 쓸 테스트

| ID | 테스트 | 입력 |
| --- | --- | --- |
| A1 | `meeting_sample.md`가 부분 6개로 나뉜다. 실제 녹화본이 있으면 1차 6개, 2차 5개(spec P1-2 기대 결과 표) | `tests/fixtures/notion/`, `notion_local/` |
| A2 | `구체화`가 `document` 부분으로 떼어지고, `스마트 옷장` 부분 md에는 없다 | fixture md + 가짜 Groq 응답 |
| A3 | 표 6개의 셀 내용이 부분 md에 있다 | fixture md |
| A4 | 블록 수가 다르면 블록 읽기로 대체한다 | 블록 하나를 뺀 fixture |
| A19 | `truncated` 응답을 `unknown_block_ids`로 이어 받고, 20회를 넘으면 블록 읽기로 대체한다 | 가짜 Notion 응답 |
| A20 | 같은 내용 해시의 부분은 다시 판정하지 않는다. `kind_source = 'human'`은 해시가 바뀌어도 유지한다 | 같은 fixture 두 번 |

## 완료 조건

- [ ] A1~A4, A19, A20 통과, A18 통과
- [ ] 실제 Notion: 이 팀 페이지 전부를 `reindex`했을 때 블록 대조가 맞거나, 어긋난 페이지가 블록 읽기로 대체된다. 어긋난 비율을 PR에 남긴다
- [ ] 처음 수집의 Notion 호출 수·시간을 Phase 0 기준값과 비교해 PR에 남긴다

## 이 phase에서 하지 않는 것

- 추출, 확인 흐름, 메뉴 변경(Phase 2·3)
- 메뉴는 이 phase가 끝나도 지금처럼 동작해야 한다
