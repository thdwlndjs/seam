# Phase 4 회의록 입구

브랜치 `feat/meeting-inputs` · 앞선 조건 [Phase 3](phase-3-confirm-apply.md) · Phase 5와는 독립

이 phase가 끝나면, Notion 밖의 회의록을 Teams 채팅으로 받을 수 있다. 붙여넣은 텍스트, txt·docx 파일, 클로바노트에서 내려받은 파일이 모두 같은 미리보기 → 확인 흐름을 탄다.

## 읽을 것

- [spec.md](../spec.md) P1-7 회의록 입구, P1-5 반영 흐름

## 작업

- [ ] `src/bot/main.py`: `_turn_payload`가 `attachments`(이름, `contentType`, 다운로드 URL)도 서버로 넘긴다. 지금은 `text`와 `value`만 넘긴다.
- [ ] `src/server/main.py`: `TurnRequest`에 `attachments`를 더한다. 메뉴 4번 ④ "회의록 올리기"를 누른 뒤의 다음 메시지(텍스트나 파일)를 회의록으로 받는다. 파일 내려받기는 서버가 한다.
- [ ] `pyproject.toml`: `python-docx` 의존성.
- [ ] 새 `src/server/meeting_inputs.py`(가칭): 텍스트·txt·docx를 md로 바꾼다. docx는 문단·목록·표를 살린다.
- [ ] 받은 내용을 `source_parts`(`origin = 'teams'` 또는 `'clova'`)로 만들고, 바로 직접 반영 경로(미리보기)로 보낸다.
- [ ] 클로바노트 공유 링크(`clovanote.naver.com/s/…`)가 오면 파일이나 텍스트를 보내 달라고 답한다.
- [ ] Teams 파일 받기 확인
    - 개인 채팅: 앱 매니페스트([appPackage/](../../../appPackage/))의 파일 지원 설정.
    - 채널·그룹 채팅: 파일이 SharePoint 링크로 오는지, 추가 권한이 필요한지.
    - 안 되는 경로는 붙여넣기만 지원하고 안내 문구를 바꾼다.

## 먼저 쓸 테스트

| ID | 테스트 |
| --- | --- |
| A13 | 클로바노트 공유 링크를 받으면 파일을 보내 달라고 답한다 |
| A25 | 붙여넣은 텍스트가 `origin = 'teams'` 부분이 되고 미리보기 카드로 간다 |
| A26 | docx의 문단·목록·표가 md로 바뀐다 |

## 완료 조건

- [ ] A13, A25, A26 통과, A18 통과
- [ ] Playground: 붙여넣기 하나와 파일 하나로 미리보기 → 반영까지 간다
- [ ] 파일이 안 되는 경로가 있으면 그 이유와 안내 문구를 PR에 남긴다
