의사결정 상태는 코드가 전달한 decision_state(value, label)를 따른다.
- unknown(합의 여부 미확인)은 합의인지 기록으로 확인할 수 없다는 뜻이다. "합의가 없다", "미합의다", "결정되지 않았다"로 바꾸지 않는다.
  maturity의 null·누락·알 수 없는 값은 unknown이다. kind=decision, status=active나 저장 승인은 합의 근거가 아니다.
- draft/recommended/discussed는 각각 가안/조언/논의로 기록된 상태다. 해당 기록만으로 프로젝트 전체에 합의가 없다고 단정하지 않는다.
- agreed는 합의로 기록된 상태다. 원문을 다시 검증했다는 뜻은 아니며 GitHub 작업 완료와도 다르다.
- answer, claim, note, overview, unknowns, next에도 같은 구분을 적용한다. 미확인은 "합의 여부를 확인하지 못했다"로 표현한다.
