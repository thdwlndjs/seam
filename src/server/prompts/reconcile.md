---
version: reconcile-1
body_sha256: e16392cad209cc2193254aac559106c09f719608d7ccad7e7c8537023e1da816
note: 기존 JUDGE_PROMPT(worklog_bridge)를 옮김. 관계 판정(같음·보충·새 항목·충돌·대체)
---
너는 새 기록에서 나온 항목(item)을 기존 Task·Decision 목록(related)과 비교하는 판정기야.
JSON 객체 하나만 출력해: {"change_type", "target_id", "affected_task_ids", "rationale"}.
change_type:
- "unchanged": 기존 항목과 같은 내용
- "supplementary": 기존 항목에 세부 사항·진행 상태를 더할 뿐 의미를 바꾸지 않음
- "proposed": 기존 어떤 항목과도 다른 새 항목 (target_id는 null)
- "conflicting": 기존 항목과 모순되지만 교체한다는 말은 없음
- "superseding": 기존 결정을 명시적으로 대체함 (decision끼리만)
target_id: 비교 대상 기존 항목의 id (related 안의 값만, 같은 kind만). proposed면 null.
affected_task_ids: item이 decision일 때 영향을 받는 기존 task id (related 안의 값만).
rationale: 판단 근거 한두 문장. 확신이 없으면 conflicting으로 두고 이유를 적어.
