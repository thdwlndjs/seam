# AI PM v2.1 의미적 정합성 설계

- 작성일·레퍼런스 확인일: 2026-11-05
- 상태: **A의 공통 상태 판정·브리핑/QA 정합성 수정과 회귀 검증 완료. B의 품질 고도화는 미착수.**
- 기준: [plan.md](plan.md) §2.2–2.5, Phase 2·4·5, [progress.md](progress.md), 현재 작업 트리, 기존 20문항 정합성 조사.
- 이번 적용 범위는 A의 공통 의사결정 상태 판정과 메뉴 2 브리핑·Phase 5 QA의 해석 정합성이다. 지식 추출·요약 생성·검증 변경은 B의 후속 명세로 보존한다.
- 즉시 수정에서도 기존 요약·종합 기능을 유지한다. B는 근거 있는 재서술·압축·복수 출처 종합을 유지하는 후속 설계다.
- Phase 5의 사람 판정 미완료, 의미 오류, 브라우저 출처 이동·서버 최신 코드 반영 미확인 상태는 이 문서 작성으로 해소되지 않는다.

## A. 즉시 반영하는 정합성 수정

### A.1 범위와 결정

이번 변경은 저장된 성숙도를 읽는 공통 함수와 브리핑·QA의 입력·표시에 한정한다. 근거가 있는 요약·종합 기능과 기존 질문 호출 분기를 유지한다. 실제 원문을 다시 판단하거나 기존 지식을 정정하는 작업은 포함하지 않는다.

| 저장값 | 공통 value | 표시와 해석 |
| --- | --- | --- |
| draft | draft | 가안으로 기록됨 |
| recommended | recommended | 조언으로 기록됨 |
| discussed | discussed | 논의로 기록됨 |
| agreed | agreed | 합의로 기록됨. 원문 의미를 새로 검증했다는 뜻은 아님 |
| 누락·null·알 수 없는 값 | unknown | 합의 여부 미확인. 합의·미합의·결정 부재를 추론하지 않음 |

kind=decision, status=active, 일반 저장 승인, 자동 반영, GitHub 완료를 합의 근거로 사용하지 않는다. 사람의 명시적인 성숙도 수정값도 저장된 maturity를 통해 읽는다. 일반 본문 편집이나 revision actor만으로 합의를 추정하지 않는다. 이번 단계는 기존 maturity 기록의 근거 자체를 검증하지 않으며, 이 검증은 B에서 다룬다.

### A.2 구현 계약과 호환성

- 새 worklog/decision_state.py의 resolve(maturity)는 {value, label}을 반환하는 순수 함수다. 유효한 저장값을 그대로 해석하고 그 밖에는 unknown을 반환한다. 외부 조회·DB 쓰기·LLM 호출을 하지 않는다.
- leading._section_of는 성숙도가 없는 decision의 과거 확정 가정을 제거한다. 합의로 기록된 항목만 기존 결정됨 분류를 사용할 수 있다. 담당자에 따른 task 분류, needs_discussion/open/hypothesis의 불명확 분류와 GitHub 상태 규칙은 유지한다.
- 브리핑의 decision/requirement에 성숙도가 없으면 [합의 여부 미확인]을 표시한다. [확정, 성숙도 미기록] 표시는 제거한다. 유효한 기존 성숙도 표시는 유지한다.
- 브리핑·QA 모델 입력에 동일 함수가 만든 decision_state를 추가한다. 기존 maturity 키·유효 값·null과 응답 계약을 유지한다. 저장 데이터나 enum에 unknown을 쓰지 않는다.
- QA는 실제 표시 가능한 finding이 참조한 decision/requirement/hypothesis/open의 상태를 코드로 표시한다. 입력에서 제외됐거나 근거 링크가 없는 항목의 상태는 추가하지 않는다. 이 표시는 저장 기록의 해석이다.
- common/decision-state.md를 두 생성 프롬프트만 공유한다. A 작업에서 qa-2/project-leading-2로 판을 올려 브리핑 캐시를 무효화했다. 이후 Phase 6 대안 분기를 반영해 QA는 qa-3이다. 추출·관계 판정이 공유하는 common/states.md는 고치지 않아 기존 추출 캐시와 판단 규칙을 유지한다.
- A 작업에서 QA의 기존 분기와 최대 2회 상한을 유지했다. 이후 Phase 6은 조율 질문을 QA 1회와 조율 1회로 처리한다. 별도 원문 대조 호출·새 wire schema·semantic 저장 속성은 도입하지 않았다.

### A.3 이번 수정의 검증·완료 기준

1. null·알 수 없는 값·가안·조언·논의·합의를 양쪽이 같은 value/label로 해석한다.
2. 과거 decision/active 항목의 성숙도가 없으면 브리핑의 결정됨 구역에 넣지 않는다. QA 입력·코드 표시도 합의 여부 미확인이다.
3. 명시적으로 저장된 agreed는 유지한다. 작업 완료·담당자·확인 필요 상태를 합의 판정과 혼동하지 않는다.
4. 격리된 합성 저장소의 일반·과거 데이터를 읽고 표시한 뒤 저장값·버전·revision 수가 바뀌지 않는다.
5. 관련 회귀 테스트와 src/server/tests 전체 회귀가 통과한다. 테스트는 임시 저장소·가짜 LLM·모킹된 외부 조회를 사용한다.

구현 대상은 decision_state.py, leading.py, qa.py, QA/브리핑 프롬프트와 관련 테스트다. 테스트 결과와 미완료 항목은 [progress.md](progress.md)에 기록한다.

2026-11-05 검증 결과: 관련 회귀 61개 통과. 최종 코드의 전체 회귀는 기본 실행 및 외부 HTTP 차단 실행 모두 352개 통과했다(차단 실행 0 실패·0 오류·0 skip, HTTP 시도 2건 차단). A는 구현·회귀 기준을 충족했다. 기존 테스트의 외부 조회 격리 누락은 progress.md에 기술적 문제로 남겼으며, 이 결과를 B의 의미 평가나 실연동 검증 결과로 사용하지 않는다.

### A.4 기준선과 남는 문제

기존 실데이터 20문항 결과와 검토 파일은 변경하지 않는다. 문항 2·3의 미확인→합의 부재 오류는 회귀 기준선으로 남기며, 이번 합성 테스트 통과만으로 기존 답변의 의미 정확성이 개선됐다고 판정하지 않는다. 15의 고객 수 제한, 16의 인과관계, 17의 중요도 왜곡도 미해결로 유지한다. 사람 판정, 브라우저 링크 이동, 실행 서버의 최신 코드 반영 여부는 각각 미완료·미확인이다.

공통 함수와 코드 표시가 일치해도 모델의 자유 answer/claim/note/overview/unknowns/next가 지침을 어길 가능성은 남는다. 이번 작업은 임의의 자연어 의미를 검증하거나 이를 모두 차단하는 변경이 아니다.

## B. 후속 품질 고도화 설계 — 미착수

아래 §1~12는 기존 Astra 설계의 원인 분석·문헌·데이터 계약·검증 계획을 보존한 후속 명세다. 요약 생성·검증 파이프라인 변경, QA 초안+LLM 대조 2회, 구조화 출력 확장, provenance 저장, 관계 반영 정책 변경, 대규모 독립 평가가 여기에 속한다. A의 완료 조건과 구분하며, 이번 구현·전체 회귀 통과를 B의 완료로 간주하지 않는다.

2026-11-06 현재 Phase 6의 조율 질문은 QA 1회와 조율 1회를 이미 사용한다. B의 QA 원문 대조까지 같은 질문에 추가하면 2회 상한과 충돌한다. 두 기능의 통합 방식은 B 착수 시 별도로 결정하며, 아래 설계만으로 세 번째 호출을 허용하지 않는다.

### 1. 후속 설계의 최종 결정

**근거를 먼저 구성하는 제어 가능한 생성 + 주장 단위 구조화 + 코드의 상태·출처 검증 + 질문의 두 번째 호출을 이용한 원문 대조**를 채택한다.

1. 모델은 원문에서 뒷받침되는 내용을 자유롭게 요약하고 여러 근거를 종합할 수 있다. 각 문장과 추출 필드는 사용한 원문 구간에 연결한다.
2. 저장 요약은 검색과 내용 선택에 사용한다. 새로운 요약·답변의 근거에는 해당 요약의 원문 구간도 반드시 전달한다. 요약만을 다시 요약하며 의미가 강화되는 경로를 막는다.
3. 합의·작업 상태·검색 범위는 공통 코드 정책으로 해석한다. 종류, 저장 승인, 누락값, 검색 실패를 사실상의 합의·부재로 바꾸지 않는다.
4. QA의 실질적인 내용 답변은 초안 1회 + 원문 대조 1회로 처리한다. 두 번째 호출은 이미 작성된 문장의 판정만 하며 새 문장을 생성하지 않는다. 최종 응답은 통과한 문장을 재생성 없이 조립한다.
5. 추출·메뉴는 기존 정상 생성 1회 제한을 유지한다. 이 경로에는 별도 온라인 의미 판정 호출을 추가하지 않는다. 원문 연결·구조 검증·독립적인 출시 평가를 적용하되, 결과를 “자동 사실 검증 완료”로 표시하지 않는다.
6. 인용 그대로의 출력은 근거 부족·검증 실패 때의 부분 대체 수단이다. 전체 답변을 인용 목록으로 고정하지 않는다.
7. 새 DB 테이블·컬럼·벡터 DB·상시 NLI 모델 서버는 도입하지 않는다. 기존 JSON 필드와 evidence/revisions/runs를 사용한다.

#### 보장 범위

코드가 보장할 것은 **출처 참조 무결성, 실제 입력 범위, 상태 해석 규칙, 호출 상한, 검증을 우회하는 출력 경로의 차단**이다. 일반적인 자연어 함의·인과관계 판단은 모델과 평가의 영역이다. 구조화된 출력, 두 번째 모델 판정, 같은 모델의 자기 검토 어느 것도 의미적 무오류를 증명하지 않는다.

이 설계의 출시 조건은 §10의 독립적인 원문 기반 평가를 통과하는 것이다. 특히 1회 경로의 의미 정확도는 실측해야 한다. 원문 자체의 사실성·최신성이나 조회하지 않은 문서까지의 완전성은 보장하지 않는다.

### 2. 원인과 근거

#### 2.1 실제 관찰과 이전 분석의 정정

실제 업무 원문·개인 식별자·Notion ID는 이 문서와 공개 테스트 픽스처에 복사하지 않는다. 아래 번호는 기존 20문항 검토 기록과 대응한다.

| 문항 | 최초 확인된 문제 | 전달 과정 및 확실성 |
| --- | --- | --- |
| 2·3 | QA가 성숙도 미기록을 합의 부재로 단정 | 저장 항목의 maturity는 null. “합의 없음”은 최종 답변에서 확인됨. 이전 decision 분류 자체의 정당성은 합의의 증거가 아님 |
| 15 | Phase 2의 part-extract-3 결과 detail에 원문에 없는 고객 수 제한 추가 | detail → 변경 후보 statement → 저장 본문이 같은 내용. **저장 단계에서 처음 생겼다는 이전 설명은 detail 확인 누락으로 인한 오진** |
| 16 | 저장 지식의 제한 표현을 계승하고 QA에서 인과관계 추가 | 원문은 두 내용을 병렬로 언급. 저장된 제한 표현과 별개로 “때문에”에 해당하는 관계가 답변에서 추가됨 |
| 17 | Phase 2 추출 제목에 원문에 없는 중요도 추가 | 제목 → 저장 지식 → 질문 문구·답변으로 전달. 짧은 원문을 확정적인 중요도·분석 결과로 확장 |

다른 문항의 합의·결정·담당자 부재 단정도 같은 실패 유형에 속한다. 기존 AI 예비 판정을 사람 정답으로 승격하지 않는다. 정확한 당시 입력 전체는 기존 검토 JSON에 없으므로, 현재 코드로 복원한 입력과 실제 저장된 실행 입력을 구별한다.

#### 2.2 수정 전 코드에서 확인한 재발 경로

| 경계 | A 수정 전 확인한 동작 | 필요한 수정 |
| --- | --- | --- |
| 원문 → 추출 | [parts.check_items](../../../src/server/worklog/parts.py)는 quote 존재를 확인하지만 title/detail 의미는 검사하지 않음 | 필드별 원문 연결과 의미 보존 제약. 인용 존재와 함의 판정의 상태를 구분 |
| 추출 → 저장 | [apply._as_core_item](../../../src/server/worklog/apply.py)은 detail 또는 title을 statement로 전달. agreement_quote는 전달하지 않음 | 요약을 유지하면서 원문·필드 연결과 상태 근거를 함께 보존 |
| 산출물 요약 | [lineage.sync_artifacts](../../../src/server/worklog/lineage.py)는 부분별 summary를 이어 붙임. 문장별 근거가 없고 기존 artifact 근거는 record 앞부분일 수 있음 | artifact의 각 요약 문장과 해당 원문 구간을 연결 |
| 요약 → 메뉴 | [leading.project_docs / briefing_payload](../../../src/server/worklog/leading.py)는 저장 요약을 다시 모델에 전달 | 원문 근거를 같이 전달. 산출물 전체 성숙도를 모든 하위 주장에 전파하지 않음 |
| 검색 → QA | [qa.gather / build_payload](../../../src/server/worklog/qa.py)는 인용을 읽어 놓고 첫 payload에는 넣지 않음 | 처음부터 원문 구간 제공. 저장 제목·본문은 derived 값으로 구별 |
| 근거 위치 | [board._quotes](../../../src/server/worklog/board.py)는 인용을 문자열로 반환 | source_id·판·부분·인용 위치를 함께 반환. 최신 링크와 과거 인용을 무조건 결합하지 않음 |
| 상태 | parts는 누락 성숙도를 종류로 보충. leading은 성숙도 없는 decision을 확정 취급 | A에서 브리핑·QA의 저장 성숙도 해석만 통일. 추출 기본값 변경·원문 상태 근거 검증은 B에 남음 |
| 관계 반영 | [core._judge](../../../src/server/worklog/core.py)는 superseding에서 maturity=None을 허용 | 합의·변경 근거가 없는 자동 대체 차단. 관계 근거를 보존 |
| 출력 | QA는 유효한 ID가 있으면 claim을 허용하며, 일부 findings가 통과하면 별도 answer도 표시 | 문장별 참조·판정과 실제 표시 문장을 결합. 자유 answer 우회 제거 |
| 메뉴 정책 | project-info 지침은 최신 문서를 우선 사용하도록 함 | 최신이라는 이유로 이전 결정·모순을 지우지 않음. 변경 근거가 없으면 차이를 함께 설명 |
| 호출·대체 | [worklog_bridge.lead_call](../../../src/server/worklog_bridge.py)은 qa 외에는 잘못된 JSON을 재시도할 수 있음. 메뉴 1 실패 뒤 예전 요약 경로도 존재 | 메뉴 전체 논리 호출 계수기로 재생성·대체 호출까지 제한 |
| 평가 | 기존 기대 근거는 저장 DB에서 복사했고 질문에 추출 제목이 들어감 | 원문에서 독립적으로 작성한 정답과 중립적인 질문이 필요 |

오류가 자동 반영됐다는 사실은 사람이 문장 의미를 검토했다는 증거가 아니다. 확인한 2·3번 항목의 revision actor도 자동 반영이었다.

### 3. 레퍼런스 조사와 접근 비교

다음은 1차 논문·공식 기술 문서에 근거한 비교다. 논문이 검증한 결과와 이 프로젝트에 적용하려는 설계 추론을 구분한다. 한국어 업무 메모에서의 정확도는 아래 문헌으로 입증되지 않는다.

| 접근·1차 출처 | 확인한 내용 | 채택 결정과 적용 한계 |
| --- | --- | --- |
| [CTRLsum, EMNLP 2022](https://aclanthology.org/2022.emnlp-main.396/) | 키워드와 제어 신호로 대상·길이·질문 중심 요약을 제어 | 질문·관점·길이·필수 불확실성의 입력 제어를 채택. 학습된 모델의 실험을 현재 모델의 프롬프트만으로 재현했다고 주장하지 않음 |
| [FROST, TACL 2021](https://aclanthology.org/2021.tacl-1.88/) | 엔터티 계획을 먼저 생성하고 이를 조건으로 추상 요약. 계획 수정으로 환각을 줄이는 실험 | 지원 근거를 먼저 고르는 구조를 참고. 엔터티 일치만으로 관계·합의 정확성을 보장할 수 없음. 추가 학습은 미채택 |
| [Attribute First, then Generate, ACL 2024](https://aclanthology.org/2024.acl-long.182/) | 내용 선택·문장 계획·순차 문장 생성으로 짧고 구체적인 attribution 제공. 복수 문서 요약과 장문 QA 평가 | 근거 단위와 문장 연결을 채택. 논문의 다단계 순차 생성은 그대로 쓰지 않음. 한 호출 안의 근거 선택+생성은 이 프로젝트의 적용 가설이며 평가 필요 |
| [ALCE, EMNLP 2023](https://aclanthology.org/2023.emnlp-main.398/) | 유창성·정확성·인용 품질을 나누어 평가. 인용이 있어도 전체 내용을 지지하지 못하는 사례 확인 | 링크 존재, 인용의 실제 지지, 답의 충분성을 서로 다른 지표로 채택 |
| [FRANK, NAACL 2021](https://aclanthology.org/2021.naacl-main.383/) | 사실 오류 유형을 분류하고 사람 판정으로 지표별 강약점 비교 | 주체·술어·조건·담화 관계 등의 오류 분류에 참고. 프로젝트의 합의 상태·부재 단정은 별도 범주로 확장 |
| [SummaC, TACL 2022](https://aclanthology.org/2022.tacl-1.10/) | 문서 전체와 문장 수준 NLI의 단위 불일치를 다루고 문장별 점수를 집계 | 문장 단위 대조를 채택. 범용 NLI를 한국어 판정기로 바로 배포하는 것은 보류 |
| [QAFactEval, NAACL 2022](https://aclanthology.org/2022.naacl-main.187/) | 질문 생성·응답 가능성 판정이 사실성 평가 성능에 영향을 줌. NLI와 QA 평가가 상보적 | 독립 평가에서 NLI/LLM 판정과 원문 질문 대조를 비교. 실시간 질문마다 추가 QA 평가 호출을 넣지는 않음 |
| [FActScore, EMNLP 2023](https://aclanthology.org/2023.emnlp-main.741/) | 생성물을 원자적 사실로 나누어 근거가 있는 비율을 평가 | 사람 평가의 주장 단위 정밀도에 참고. 전기·외부 지식 평가 결과를 한국어 회의록에 일반화하지 않음. 정밀도만 높이려고 답을 줄이는 것을 막기 위해 포괄성도 측정 |
| [TRUE, NAACL 2022](https://aclanthology.org/2022.naacl-main.287/) | 시스템 평균 상관뿐 아니라 개별 사례 수준의 사실성 평가 성능을 비교 | 검증기 자체의 오수락·오거절률을 측정. 평균 점수만으로 검증기를 신뢰하지 않음 |
| [MiniCheck, EMNLP 2024](https://aclanthology.org/2024.emnlp-main.499/) | 문서에 근거한 사실 판정을 작은 모델로 수행하는 효율적인 접근 | 전 단계 온라인 검증의 유력한 후속 대안. 현재 환경의 자원·한국어 데이터 교정 결과가 없어 기본 배포에서 제외 |
| [RARR, ACL 2023](https://aclanthology.org/2023.acl-long.910/) | 생성 후 근거 탐색과 수정으로 원문 출력의 유용성을 유지하려는 접근 | 오류 주장 대조 원리를 참고. 온라인 외부 재탐색과 반복 수정은 QA 범위·호출 상한에 맞지 않아 미채택 |
| [Groq Structured Outputs](https://console.groq.com/docs/structured-outputs), [Cerebras Structured Outputs](https://inference-docs.cerebras.ai/capabilities/structured-outputs) | 공식 문서상 두 공급자의 gpt-oss-120b 계열에 JSON Schema strict 지원. Groq는 해당 모드의 streaming/tool use 제한도 명시 | 지원 모델에는 schema constrained decoding 채택. 형식 준수와 내용의 참은 별개. 실제 설정 모델의 지원 여부는 구현 시 확인 |

#### 대안 비교 결론

| 대안 | 요약·종합 | 추가 운영 부담 | 결정 |
| --- | --- | --- | --- |
| 프롬프트·금칙어만 강화 | 유지 | 작음 | 단독 해결책으로 기각. 우회 표현·잘못 저장된 의미를 막지 못함 |
| 모든 출력을 원문 복사로 제한 | 크게 감소 | 작음 | 기본 경로로 기각. 실패 시 국소 대체만 사용 |
| 현재 요약에 JSON Schema만 적용 | 유지 | 작음 | 필요한 형식 방어이나 단독 해결책으로 기각 |
| 근거 연결 생성 + 코드 정책 + QA 검토 | 유지 | QA 내용 응답은 통상 2회 | **채택**. 기존 상한 안에서 가장 직접적인 오류 대조 경로 |
| 모든 단계 생성+별도 LLM 검증 | 유지 | 추출·메뉴 호출 상한 변경 | 미채택 |
| 로컬 NLI/MiniCheck로 모든 문장 검증 | 유지 가능 | 새 모델·메모리·한국어 교정·운영 필요 | 오프라인 비교 후 별도 도입 판단 |
| 대규모 재학습·별도 지식 그래프 | 유지 가능 | 데이터·구조 변경 큼 | 이번 최소 수정 범위에서 제외 |

같은 공급자·동일 모델을 두 번 호출해도 독립적인 정답 판정자가 생기는 것은 아니다. 서로 다른 공급자가 동일 기반 모델을 제공해도 이를 모델 다양성으로 세지 않는다.

### 4. 의미 보존 규칙

#### 4.1 허용하는 요약·종합

- 같은 주체의 반복 진술 통합, 같은 뜻의 재서술, 관련 내용을 주제별로 재배열할 수 있다.
- 서로 다른 출처가 말한 내용을 함께 정리할 수 있다. 예: 합성 원문 A “A안을 제안했다”, B “A안의 비용 검토가 필요하다” → “A안이 제안됐으며 비용 검토 의견이 남아 있다.”
- 원문에 명시된 인과관계·중요도·합의·부정을 그대로 요약할 수 있다. 해당 표현을 일괄 금칙어로 막지 않는다.
- 문서의 주장과 외부 세계에서 검증된 사실을 구별한다. 조사 메모의 “시장이 작다”는 시장 규모를 실제 검증했다는 의미로 승격하지 않는다.

#### 4.2 금지하는 의미 변화

- 존재 언급 → 수량 제한, 병렬 언급 → 인과관계, 비용 언급 → 최우선 이슈.
- 질문·제안 → 합의, 미기록 → 미합의, 검색 부재 → 실제 부재.
- 일부 표본·특정 시점의 관찰 → 전체 고객·현재 프로젝트에 대한 단정.
- 인용의 긍정 구절만 남기고 같은 문맥의 부정·조건·예외·발언자를 제거.
- 새로운 문서라는 이유로 기존 합의를 자동 대체하거나 충돌하는 증거를 숨김.
- 사용자 질문에 포함된 전제를 근거로 취급. 질문 속 “주요 이슈”도 원문 대조 대상이다.

#### 4.3 상태의 공통 해석

후속 단계에서는 A의 저장 성숙도 판정을 확장해 새 함수 resolve_state(item, support, revision) 하나를 메뉴·QA·관계 반영에서 공유한다. A에는 아래의 원문 근거·revision 연결 검증을 구현하지 않는다. 저장 enum은 바꾸지 않고 표시용 unknown을 추가한다.

| 입력 | 표시·행동 |
| --- | --- |
| maturity 누락/유효한 근거 연결 없음 | unknown. 합의·가안·미합의를 자동 추론하지 않음 |
| kind=decision, status=active | 분류·활성 상태일 뿐 합의 증거가 아님 |
| 원문에서 근거가 연결된 draft/recommended/discussed | 해당 출처의 가안·조언·논의 기록으로 표시 |
| agreed + 해당 항목의 합의 근거 연결 | “해당 기록에서 합의로 남음”으로 취급. 최종 의미 판정은 여전히 오류 가능 |
| 원문이 명시적으로 미합의를 말함 | 그 출처·시점의 미합의. 전체 프로젝트의 결정 부재로 확대하지 않음 |
| 사용자가 명시적으로 성숙도를 수정한 revision | 사용자 지정 상태로 귀속. 일반 본문 수정·저장 승인을 합의 확인으로 간주하지 않음 |
| SUPERSEDES/RESOLVES 연결 있음 | 양 끝 항목·상태·관계 근거를 함께 사용. 근거 없는 과거 연결은 “기록된 연결, 근거 미확인” |
| 연결 없음/검색 결과 없음 | 조회 범위에서 대체·해결 여부를 확인하지 못함 |
| 이슈 closed/PR merged | GitHub 작업 상태만. 동작·테스트·기능 구현의 검증으로 승격하지 않음 |

공통 함수의 통과는 형식·근거 연결 확인이다. 원문의 어떤 문장이 실제 합의인지를 코드가 완전히 판별했다고 이름 붙이지 않는다. 모델 선언과 코드 확인 결과를 별도 필드로 보존한다.

코드 검사는 상태 값·참조·출처·revision의 조합을 검사한다. 임의의 한국어 문장에서 “합의했다”의 모든 동의 표현을 알아내는 판정기로 구현하지 않는다. 자유 요약 안의 상태 의미는 QA 원문 대조 및 전 경로의 독립 평가 대상이다. facets 누락이나 금칙어 미검출을 안전하다는 증거로 삼지 않는다.

### 5. 데이터 계약

#### 5.1 공통 구현 위치와 버전

새 worklog/grounding.py에 근거 조립·무결성·상태 정책·표시 단위 검증을 둔다. 새 worklog/semantic_contracts.py에 역할별 JSON Schema와 검증 한도를 둔다. 기존 parts/apply/lineage/leading/qa가 이를 호출한다. 별도 서비스는 만들지 않는다.

새 wire 계약은 schema_version=2, guard_version="sc-1"로 시작한다. 모든 object는 additionalProperties=false, 모든 정의 키는 required, 선택 값은 null 또는 빈 배열로 표현한다. 런타임 validator도 같은 필드·형식·한도를 검사한다. 공급자 strict가 없더라도 validator를 생략하지 않는다.

#### 5.2 EvidenceUnit: 코드가 만드는 원문 단위

~~~json
{
  "id": "e1",
  "origin": "notion",
  "source_id": "opaque-source-id",
  "part_id": "opaque-part-id",
  "source_hash": "sha256-of-source-snapshot",
  "canonical_hash": "sha256-of-canonical-part-text",
  "span": {"start": 120, "end": 188},
  "text": "합성 예시: A안을 제안했다. 비용 검토는 아직 필요하다.",
  "context": {"heading": "검토 안건", "speaker": null, "source_date": null},
  "completeness": "complete",
  "visibility": "current"
}
~~~

규칙:

- id는 요청 내부 별칭이다. 모델이 source_id·해시·날짜·URL을 만들어 채우지 않는다.
- origin: notion | human_revision. GitHub 상태는 아래 Fact로 전달한다. visibility: current | historical. completeness: complete | partial. 찾을 수 없는 원문은 가짜 빈 EvidenceUnit으로 만들지 않고 item의 support_ids=[]와 source_missing 사유로 나타낸다.
- 현재 parts.md_to_text에 버전을 부여한 정규화 함수를 사용한다. 줄·문단·제목·부정·인용 경계를 보존한다. span은 이 canonical 문자열의 Python Unicode code point 기준 반개방 구간이다. 조각 분할 후에도 원래 부분 전체의 좌표로 저장하며 bytes/UTF-16 위치와 혼용하지 않는다.
- canonical_text[start:end]와 text가 정확히 일치해야 한다. 기존 _squash/find_quote는 과거 인용의 위치를 찾는 보조 수단으로만 사용한다. 위치를 찾은 뒤 실제 원문 구간을 다시 꺼낸다.
- 완전한 문단/목록 항목을 기본 단위로 하고 필요한 상위 제목·인접 답변을 함께 제공한다. 너무 긴 단위를 중간 글자에서 자르지 않는다. 문맥을 충분히 넣지 못하면 partial로 표시한다.
- source_id가 가리키는 저장 원문 판과 근거를 묶는다. 변경 가능한 source_parts의 최신 본문에 과거 인용을 무조건 재연결하지 않는다. 판을 특정하지 못하면 source_missing, 판은 있으나 문맥 일부가 없으면 partial로 처리한다.
- Notion 근거는 source_id와 part_id가 모두 필요하다. human_revision은 source_id를 revision ID로 쓰고 part_id=null로 둔다. source_hash는 해당 저장 원문/revision 본문의 해시이며 두 origin 모두 text와 span을 검증한다. context의 heading/speaker/source_date는 원문·메타데이터에서 확인한 값만 넣고 나머지는 null이다.
- QA가 읽는 원문은 이미 해석한 부분으로 제한한다. 같은 페이지의 미해석 부분을 주변 문맥이라는 이름으로 끌어오지 않는다. 구데이터에서 부분 대응을 복구하지 못하면 페이지 전체를 새로 해석하지 않고 미확인으로 남긴다. current는 저장본 기준 최신이라는 뜻이며 외부 원문의 현재 상태를 새로 확인했다는 뜻이 아니다.
- 표시 URL은 기존 코드로 만든다. 정확한 블록 연결이 없으면 페이지 링크를 사용하고 블록 검증을 했다고 표시하지 않는다.
- 원문에 포함된 명령은 자료로 취급한다. 근거 단위 안의 지시가 시스템 지침·출력 계약을 바꾸지 못한다.

#### 5.3 생성 입력: Guidance와 원문을 함께 전달

~~~json
{
  "schema_version": 2,
  "guidance": {
    "task": "qa",
    "question": "A안은 현재 어떤 상태인가요?",
    "focus": ["내용", "의사결정 상태", "미확인 사항"],
    "max_claims": 8,
    "allow_synthesis": true
  },
  "items": [
    {
      "id": "k1",
      "derived_title": "A안 검토",
      "derived_statement": "저장된 요약",
      "support_ids": ["e1"],
      "state": {"value": "unknown", "basis_ids": [], "origin": "unrecorded"}
    }
  ],
  "evidence": [],
  "facts": [],
  "coverage": {"omitted_items": 0, "omitted_evidence": 0, "partial_context": false}
}
~~~

evidence에는 실제 EvidenceUnit을 담는다. derived 값과 질문 문장은 증거가 아니다. evidence가 없는 요약은 검색 후보·추가 확인 대상으로만 쓰며 사실 생성 근거로 사용하지 않는다. focus는 출력의 관점이며 “주요·합의됨” 같은 결론을 주입하지 않는다.

facts는 GitHub 상태·조회 범위·명시적 사용자 수정처럼 코드가 출처와 값의 관계를 확인한 구조화 사실이다. facts도 원문 의미의 보편적 진실을 인증하지 않는다.

guidance.task의 enum은 interpret | reconcile | project_info | project_leading | qa | qa_review다. focus는 해당 역할의 고정 정책과 사용자 질문에서 구성하며 최대 5개다. state.origin은 source_reported | human_set | unrecorded, state.value는 기존 maturity enum 또는 unknown이다. 모델이 선언한 합의를 코드가 독립적으로 증명한 것처럼 source_reported를 verified로 바꾸지 않는다.

Fact의 필드는 id, type(github_state | scope | human_state), subject_id, value, observed_at, origin_ref다. 모두 코드가 채운다. origin_ref는 기존 GitHub URL/사용자 revision ID/요청 ID 중 type에 맞는 값이다. 모델은 Fact를 만들거나 갱신하지 않는다.

#### 5.4 Claim: 요약을 유지하는 최소 표시 단위

~~~json
{
  "id": "c1",
  "text": "A안이 제안됐으며 비용 검토 의견이 남아 있습니다.",
  "mode": "synthesis",
  "support_ids": ["e1", "e2"],
  "facets": ["modality"],
  "item_ids": ["k1"]
}
~~~

- mode: paraphrase | synthesis. 원문에 명시된 가설을 요약하는 것은 paraphrase다. 모델이 새로 만든 가설·권고는 이번 계약의 사실 답변에 넣지 않는다.
- text는 1문장, 최대 220자. synthesis는 복수 출처의 정합적인 결합을 허용한다. 한 문장에 복수 주장이 있으면 모두 지지돼야 통과한다.
- support_ids는 evidence 또는 facts의 코드 발급 ID, 최대 4개. 해당 문장 전체를 함께 지지해야 한다. 단순히 관련된 페이지를 연결하는 것으로 충분하지 않다.
- facets: entity | quantity | polarity | modality | attribution | time_scope | causality | priority | decision_state | work_state 중 0개 이상. 생성 모델의 신고값이지 완전한 위험 탐지 결과가 아니다.
- item_ids는 탐색·상세보기 연결용이며 근거를 대신하지 않는다.
- content plan은 claim 배열의 순서와 support 선택으로 표현한다. 별도 장문의 추론 과정이나 chain-of-thought는 요청·저장하지 않는다.
- 최종 응답은 claim의 text를 그대로 연결한다. 추가 자유 문장으로 요약을 다시 만들지 않는다. 기본 경로는 자연스러운 재서술·종합 문장이지 인용문 목록이 아니다.

#### 5.5 ExtractV2

정상 호출 하나가 근거 선택과 추출·요약을 함께 수행한다. 코드가 입력에 evidence 단위를 만들어 주므로 모델이 근거 문자열을 다시 길게 복사할 필요가 없다.

~~~json
{
  "schema_version": 2,
  "items": [
    {
      "kind": "requirement",
      "title": "A안의 비용 검토",
      "detail": "A안은 비용 검토가 필요하다고 기록돼 있다.",
      "owner": null,
      "due": null,
      "role_hint": null,
      "maturity": null,
      "support": {
        "title": ["e1"],
        "detail": ["e1"],
        "owner": [],
        "due": [],
        "maturity": []
      },
      "confidence": 0.8
    }
  ],
  "artifact": {
    "type": "기획",
    "maturity": null,
    "maturity_support_ids": [],
    "summary_units": [
      {"id": "c1", "text": "A안과 비용 검토 조건을 정리한 문서다.", "mode": "paraphrase", "support_ids": ["e1"], "facets": [], "item_ids": []}
    ]
  }
}
~~~

- kind/role_hint/type은 기존 enum을 사용한다. artifact는 기존 정책에 따라 null일 수 있다.
- 항목 title은 최대 80자, detail은 최대 300자, 각 support 배열은 최대 4개를 초기 한도로 둔다. artifact.summary_units는 최대 6문장이다. 이 한도는 새로운 생성 계약이며 기존 DB 값을 자르거나 일괄 변환하는 규칙이 아니다.
- 필드가 null이면 그 필드의 support는 빈 배열이다. title은 항상 근거 필요, detail/owner/due/maturity는 값이 있으면 근거 필요.
- kind의 의미도 원문에 맞춰 판단한다. 외부 멘토의 발언이라고 제목·화살표만으로 단정하는 기존 지침을 제거한다. 발언자 불명이면 불명 상태를 유지한다.
- confidence는 모델의 출력값이며 승인·합의·의미 검증의 기준으로 사용하지 않는다.
- check_items는 참조와 상태 정책을 검사하고 기존 형식의 title/detail/quote/maturity를 만들되 semantic envelope도 함께 반환한다. quote는 원문에서 코드가 선택한 대표 구절이다.
- artifact.summary는 검사를 통과한 summary_units.text를 이어 붙여 코드가 만든다. summary_units에 없는 문장은 넣지 않는다.
- parts.clean_artifact와 조각 합치기의 기존 summary[:600]을 근거 본문에는 적용하지 않는다. 전체 문장 단위 summary_units를 보존한다. 목록용 길이 제한은 완전한 문장 단위의 별도 preview로 처리하며, 빠진 문장 수를 기록한다. preview를 완전한 원문 근거로 다시 쓰지 않는다.
- 한 조각의 기존 최대 항목 수 20을 유지한다. 출력 초과·잘린 JSON을 부분 성공으로 파싱하지 않는다. 큰 입력은 기존 조각 분할 정책으로 처리하며 조각별 정상 1회다. 조각별 사용량을 합산 기록한다.
- 잘못된 support/형식은 기존 contract 또는 quote_mismatch 오류 경로로 보낸다. 해당 후보는 반영하지 않는다. 다른 정상 후보는 기존 partial 처리 정책을 따른다.
- **여기서 얻는 상태는 source_linked이며 semantically_verified가 아니다.** 한 호출 제한 아래 독립적인 의미 판정은 수행되지 않는다.

#### 5.6 저장용 semantic envelope

part_extracts.items_json, change_candidates.proposal_json, work_items.attributes_json에 다음 선택 속성을 추가한다. schema 자체의 ALTER TABLE은 필요 없다.

모델 응답 검증이 끝난 후 코드가 semantic을 만든다. 새 저장용 part_extracts는 {schema_version:2, items:[기존 정규화 필드+semantic], artifact:null 또는 기존 summary/type/maturity+semantic} 형태다. 후보에는 proposal_json.semantic, 최종 항목에는 attributes_json.semantic으로 전달한다. 모델 wire 계약에 semantic을 직접 쓰게 하지 않는다.

~~~json
{
  "semantic": {
    "schema_version": 2,
    "guard_version": "sc-1",
    "origin": "model_extract",
    "status": "source_linked",
    "content_fingerprint": "sha256(canonical-json(kind,title,statement,maturity,owner,due))",
    "support_by_field": {"title": ["stable-ref-1"], "detail": ["stable-ref-1"], "owner": [], "due": [], "maturity": []},
    "evidence_refs": [],
    "content_units": [],
    "summary_units": [],
    "agreement_basis": null,
    "verification": null
  }
}
~~~

- evidence_refs의 각 원소는 {ref_id, canonical_version, origin, source_id, part_id, source_hash, canonical_hash, span, text, context, completeness}다. 요청마다 바뀌는 별칭을 저장하지 않는다. ref_id는 origin·source_id·part_id·source_hash·canonical_version·span의 canonical JSON을 SHA-256으로 계산한다. 요청 생성 시 이 참조를 e1 등의 별칭으로 매핑하며 동일 참조는 중복 저장하지 않는다. 과거 판의 구절을 보존하되 current/historical 여부는 읽을 때 판정한다.
- fingerprint의 statement는 실제 저장될 statement_md다. 일반 항목은 detail 또는 title, artifact는 summary_units에서 조립된 본문이다. kind/title/statement/maturity 외에 owner/due도 포함하며 누락값은 null이다. canonical JSON은 key 정렬, separators=(',', ':'), ensure_ascii=False, UTF-8, 줄바꿈 LF로 고정한다. 공백이나 문장부호를 임의로 제거하지 않는다.
- fingerprint의 owner는 core의 assignee와 대응한다. support_by_field는 추출 필드명(title/detail/owner/due/maturity)을 유지하며 어댑터가 저장 필드명과 대응시킨다.
- content_units는 보충 전후의 내용을 구분하는 저장 단위다. 각 원소는 {unit_id, text, support_ref_ids, state:{value,basis_ref_ids,origin}}이고 초기 일반 항목은 detail 또는 title로 만든 1개 단위다. 단위의 text를 다시 생성하거나 문장 중간에서 자르지 않는다. artifact는 기존 Claim 형식의 summary_units를 쓰되 저장 시 support_ids 값을 stable ref_id로 변환하고, 요청 입력을 만들 때 다시 별칭으로 바꾼다. provenance가 없던 옛 본문 단위는 state=unknown과 빈 support_ref_ids로 보존한다.
- status: legacy_unknown | source_linked | model_checked | human_reviewed | stale. 이는 provenance 상태이며 기존 업무 status와 별개다.
- model_checked는 검증기·프롬프트·원문 판·해당 문장 fingerprint와 판정을 함께 보존한 경우에만 붙인다. QA에서 답 문장이 통과했다고 원래 저장 항목을 model_checked로 갱신하지 않는다.
- verification은 null 또는 {kind, run_id, reviewer_id, prompt_version, model, source_hashes, content_fingerprint, verdict, reviewed_at}다. kind=model이면 기존 runs의 판정과 버전이 필요하며 reviewer_id=null이다. kind=human이면 명시적인 검토 기록과 reviewer_id가 필요하고 prompt_version/model은 null이다. 이 설계는 새 사람 검토 UI를 추가하지 않으며 기존 일반 revision만으로 verification을 만들어 내지 않는다.
- human_reviewed는 명시적인 의미 검토 기록이 있을 때만 가능하다. 현재 탭의 일반 편집·저장 승인은 여기에 해당하지 않는다.
- agreement_basis는 null 또는 {origin, support_ref_ids, revision_id}다. origin은 source_reported | human_set. 전자는 해당 항목의 합의 구절 참조, 후자는 명시적으로 maturity를 바꾼 revision이 필요하다. source_reported는 모델 분류와 원문 연결의 기록이며 의미 판정의 독립성 표시는 아니다.
- 사람이 고친 내용은 기존 보호 정책을 유지한다. 내용·성숙도가 달라지면 이전 fingerprint의 semantic 연결을 stale로 처리하고 사용자 revision을 별도 출처로 제시한다. 사람 수정값을 옛 Notion 원문이 말한 것처럼 인용하지 않는다.
- supplementary는 content_units를 합치되 기존 합의 상태를 새 내용 전체에 전파하지 않는다. 현재 항목 전체의 단일 maturity로 설명할 수 없으면 표시용 상태는 unknown, 내용 단위별 원래 상태는 보존한다. 근거 없이 과거 단위의 text나 state를 새 단위에 복사하지 않는다.
- same 판정도 새 근거가 있으면 추가한다. 현재 unchanged 조기 반환 때문에 근거·provenance 갱신을 건너뛰는 경로를 보완한다.

#### 5.7 QaDraftV2와 ReviewV1

~~~json
{
  "schema_version": 2,
  "intent": "answer",
  "claims": [
    {"id": "c1", "text": "A안이 제안됐으며 비용 검토 의견이 남아 있습니다.", "mode": "synthesis", "support_ids": ["e1", "e2"], "facets": ["modality"], "item_ids": ["k1"]}
  ],
  "paragraphs": [["c1"]],
  "unknowns": [{"code": "state_unconfirmed", "item_ids": ["k1"]}],
  "work_status_refs": [],
  "need": [],
  "next": [{"type": "ask", "item_id": "k1"}]
}
~~~

claims에는 §5.4의 Claim을 최대 8개 넣는다. paragraphs는 최대 3개이며 claims ID만 참조한다. 예시의 e1/e2/k1은 해당 요청 입력에 존재해야 한다. intent는 answer | coordination. work_status_refs는 입력 facts의 GitHub ID만 사용한다. next는 최대 2개, type은 ask | agenda, item_id는 입력 항목 ID 또는 null이다.

별도 자유 answer, 자유 unknown 문장, 사실을 포함하는 next 문장을 두지 않는다. 기존 API 결과의 text/links/buttons/scope/result는 어댑터가 유지한다. 사용자가 보는 내용은 연결된 요약 문장과 코드가 만든 범위·상태·미확인 안내다.

unknowns.code의 허용값:

state_unconfirmed | no_support_in_scope | source_missing | context_incomplete | github_unavailable | verification_unavailable.

unknowns는 모델의 요청 목록일 뿐이다. 코드는 각각 resolve_state=unknown, 표시 가능한 근거 없음, 원문 연결 실패, partial/예산 누락, 실제 GitHub 조회 실패, 리뷰 실패·미검토라는 조건을 확인해 표시한다. 모델이 없다고 해도 실제 실패 안내는 코드가 추가한다. 자료 간 모순이라는 의미 판단은 자유 unknown 문구로 우회하지 않고, 양쪽 근거를 갖춘 Claim으로 표현한다.

기존 need의 {type:item_source | github, id:string} 형태와 최대 5개 제한은 유지한다. id는 첫 입력에 노출된 항목 또는 이미 조회한 GitHub 참조여야 한다. 초안 문장이 아직 지원 근거를 받지 못한 경우 support_ids=[]를 허용하되 해당 item_source need와 item_ids가 일치해야 한다. 검토 완료 전에는 절대 표시하지 않는다.

~~~json
{
  "schema_version": 1,
  "verdicts": [
    {
      "claim_id": "c1",
      "verdict": "entailed",
      "support_ids": ["e1", "e2"],
      "error_types": []
    }
  ]
}
~~~

- verdict: entailed | contradicted | not_supported | ambiguous.
- error_types: entity | quantity | polarity | modality | attribution | time_scope | causality | priority | status | evidence_gap.
- 검토 입력은 질문, 초안 문장, 원문 근거·구조화 사실·조회 한계다. 코드가 claim별 eligible_support_ids를 구성한다. 이는 초안의 원래 support, 해당 item_ids에 허용된 need 결과, 같은 해석된 부분의 문맥 근거의 합집합이다. 다른 항목·미해석 원문을 추가하지 않는다. possible counterevidence도 같은 범위 안에서 포함한다.
- entailed의 support_ids는 해당 claim의 eligible_support_ids 중 실제 검토 입력에 들어간 ID를 1~4개 선택한다. need로 추가된 근거로 인용을 보완할 수 있지만 claim.text는 바꾸지 않는다. 그 밖의 ID·빈 근거로 entailed를 반환하면 계약 위반이다. 다른 verdict의 support_ids는 빈 배열일 수 있다.
- 원문이 복수 출처에 흩어져 있으면 합쳐서 함의를 판단한다. 한 근거만으로 지지되지 않는다는 이유만으로 올바른 종합을 거절하지 않는다.
- 새 claim·수정 text·intent·need는 허용하지 않는다. 모든 제출 claim에 정확히 하나의 verdict가 필요하다. 중복·누락·알 수 없는 ID는 계약 위반이다.
- entailed도 모델 판단이다. 코드 참조 검사와 상태 정책을 다시 통과해야 표시할 수 있다. 반대 근거가 함께 존재하면 현재 확정 사실로 통과시키지 않고 해당 출처들의 불일치를 보존한다.

#### 5.8 메뉴와 관계 판정 계약

메뉴 1은 {schema_version:2, claims:[Claim], paragraphs:[[claim_id]], unknowns:[Unknown]}를 반환한다. claims 최대 10개, paragraphs 2~5개이며 입력이 적으면 1개도 허용한다. 내용이 없으면 두 배열을 비우고 unknowns를 반환한다. 기존 paragraphs[].text/sources는 새 claims에서 어댑터가 조립한다.

메뉴 2는 {schema_version:2, claims:[Claim], overview:[claim_id], lines:[{item_id,note_claim_id}], unknowns:[Unknown]}를 반환한다. overview는 최대 3개 claim, note_claim_id는 null 또는 claims의 ID다. 표시할 item_id는 코드가 만든 구역 내 ID여야 한다. 전체 claims 최대 12개, 입력 예산 때문에 줄어든 항목은 lines에도 쓸 수 없다. 순서·설명은 모델이 정할 수 있으나 구역·상태는 바꾸지 못한다.

메뉴 2의 한 줄 본문은 연결된 Claim을 사용한다. 근거를 복구하지 못한 과거 title을 확정적 요약처럼 앞에 붙이지 않는다. note_claim_id=null이면 코드의 중립적인 “항목 상세 보기”와 상태·링크만 표시한다. QA 버튼·안건 초안도 같은 원칙을 사용한다. 원래 title은 상세 화면에서 “저장된 항목명”으로 열람할 수 있으나 새 답변의 근거가 되지 않는다. 저장 title 자체를 자동 정정하지 않는다.

관계 판정은 기존 change_type/target_id/affected_task_ids/decidable/rationale에 schema_version=2와 support_ids를 추가한다. support_ids는 새 항목과 대상 항목의 evidence를 가리킨다. proposed는 target_id=null, 다른 관계는 실제 후보 target_id 필수다. rationale은 내부 판단 설명이며 사용자 사실 문장으로 바로 출력하지 않는다.

기존 kind·target 제약을 유지한다. 제목과 본문이 같은 unchanged 규칙 경로에도 상태·근거 변화는 별도로 반영한다. proposed를 제목 충돌만으로 supplementary로 바꾸는 core 후처리와 apply._judgment의 RULE_ONLY_KINDS 제목 일치 병합을 제거한다. 실제 근거를 갖춘 관계 판정 없이 같은 제목의 두 내용을 합치지 않는다. 규칙 전용 경로에 새 LLM 호출을 추가하지 않는다. 고유키 제약 때문에 저장할 수 없다면 중복 후보 오류를 기록하고 별도 확인 대상으로 남기며 자동 의미 병합하지 않는다.

자동 superseding은 새 항목의 agreement_basis, 대상 항목, 명시적 변경을 뒷받침하는 support_ids가 모두 있어야 한다. 누락 상태에서 그 관계가 반환되면 ContractError(reason=missing_state_support)로 반영을 중단하고 기존 runs 재처리 정책을 적용한다. draft/recommended/discussed가 기존 안과 다른 경우에는 처음부터 alternative로 판단하도록 한다. missing을 임의의 alternative나 needs_discussion으로 바꾸지 않는다. resolving도 기존 질문과 답의 support_ids가 없으면 같은 오류 경로다.

§5의 JSON은 형식 설명용이며 ID·해시는 자리표시자다. evidence/evidence_refs의 실제 원소는 각 정의에 따라 채워야 하므로 이 예시 자체를 참조 무결성 검증용 픽스처로 쓰지 않는다. 역할별 계약에서 열거한 키는 모두 필요하다. claim/unknown/evidence/fact는 공통 정의를 참조하며, JSON Schema는 정적 구조 검사, 런타임 검사는 실제 ID·판·상태·배열 상한을 담당한다.

### 6. 실행 흐름과 호출 예산

#### 6.1 QA: 생성한 문장을 검토한 뒤 조립

~~~text
gather saved knowledge and original evidence -> fit full request budget
if no usable content evidence/facts:             # scope metadata alone does not count
    return code-rendered scope + unknown (0 calls)
draft = call("qa", QaInputV2)                    # logical call 1
validate shape, IDs and code-owned state rules
if draft.intent == coordination:
    return existing Phase 6 coordination path     # logical call 2
    # B의 별도 원문 대조와 함께 쓰는 방식은 미결정
extra = validate_need(draft.need)                # local saved sources/known GitHub only
review_input = pack(draft.claims, initial evidence + extra)
if no reviewable claims:
    return scope + unknown + bounded source excerpts
verdicts = call("qa-review", review_input)        # logical call 2
accepted = entailed original claims AND code-valid
return render(original accepted text, provenance, scope, unknowns)
~~~

- 1차에서 support가 부족한 문장도 검증된 need로 얻은 근거가 충분하면 2차에서 통과시킬 수 있다.
- 두 번째 호출은 고쳐 쓰지 않는다. 검토 후 다시 요약하면 새 오류가 생기고 세 번째 호출이 필요해지기 때문이다. 새 근거가 초안에 전혀 없는 답을 제공하면 이번에는 근거 일부와 미확인 안내를 제시한다. 이 손실은 answerability/coverage 평가에 포함한다.
- 문장은 주체가 드러나는 독립된 문장으로 생성하도록 지시한다. 한 paragraph의 문장 중 하나가 탈락하면 해당 paragraph 전체를 제외해 근거·조건의 일부만 남는 것을 막는다. 다른 paragraph는 유지한다. 코드가 남은 문장을 새로 고쳐 쓰지 않으며, 이 보수적 탈락의 포괄성 손실도 평가한다.
- verifier 실패·한도 초과·계약 위반이면 초안을 그대로 노출하지 않는다. 확인된 구조화 사실, 제한된 원문 인용, 조회 범위를 반환한다.
- 두 번째 호출의 형식 오류를 재시도하지 않는다. 실패도 logical_calls에 포함한다.
- 예상 변화: 실질적인 내용 답변은 기존 1회가 가능하던 경우도 2회가 된다. 이는 계획의 1회 기본 분기를 변경하는 명시적인 설계 결정이며 상한 2회는 유지한다.
- 현재 Phase 6 분기는 QA 1회와 조율 1회를 사용한다. 여기에 별도 QA 검토까지 더하면 총 3회가 되므로 B 착수 시 통합 설계 결정이 필요하다. 이 명세는 그 추가 호출을 허용하지 않는다.

#### 6.2 추출·관계·산출물

1. 부분 원문을 EvidenceUnit으로 구성하고 ExtractV2를 1회 생성한다. 모델이 요약·종합할 수 있도록 주제와 상태·불확실성 보존 규칙을 준다.
2. 필드 참조와 정책 검사 뒤 semantic envelope를 포함해 기존 반영 경로를 사용한다. source_linked와 의미 검증 완료를 혼동하지 않는다.
3. reconcile의 기존 0~1회 제한을 유지한다. 호출할 때 새 항목과 기존 후보의 요약뿐 아니라 근거 구간·상태 출처도 전달한다. 비교 후보가 없다고 별도 의미 검증 호출을 추가하지 않는다.
4. superseding은 새 합의 근거와 명시적 변경 근거가 모두 필요하다. maturity=None은 허용하지 않는다. 부족하면 §5.8의 오류 경로로 반영을 멈추고 기존 결정을 유지한다.
5. resolving은 기존 질문을 실제로 해소하는 근거가 필요하다. 주제가 같다는 이유만으로 해결하지 않는다. 관계의 증거 ID·판을 candidate JSON에 보존하며 필수 근거 누락은 §5.8을 따른다.
6. artifact는 summary_units와 근거를 보존해 조립한다. 부분 중 상태가 unknown인 것을 제외하고 나머지 agreed만으로 문서 전체를 합의로 만들지 않는다.

관계 판정 모델이 원문 의미를 잘못 판단할 가능성은 남는다. 참조·상태 조건 위반은 코드로 막고, 관계 의미는 별도 사람 정답 사례로 검증한다.

#### 6.3 메뉴 1·2: 정상 1회 유지

- 메뉴 1은 문서 요약과 그 원문을 함께 입력받아 Claim 배열과 문단 순서를 만든다. team_info도 출처 단위로 취급한다. 최신 문서 정렬은 검색 우선순위이며 기존 결정 폐기 근거가 아니다.
- 메뉴 2의 overview와 note도 Claim을 사용한다. 목록 구역·상태·GitHub 상태는 공통 함수가 정한다. “왜 중요한가”를 원문 없이 만들어 내지 않는다.
- title, 담당자·일정, 관계 개수, 버튼/안건 문구도 표시 검사를 거친다. 과거 담당자·일정 값의 근거가 없으면 “저장값, 원문 근거 미확인”으로 구분하며 원문이 확인한 사실처럼 표시하지 않는다. 과거 관계 개수는 “기록된 연결”의 수이지 의미가 검증된 관계 수가 아니다.
- 프로젝트 정보가 없어 예전 project-info-raw 경로로 갈 때도 동일 계약과 근거 검증을 적용한다. 모델 출력의 자유 스트리밍을 먼저 사용자에게 노출하지 않고, JSON 완성과 검증 뒤 표시한다.
- 메뉴 첫 호출이 실패한 뒤 예전 요약기를 다시 부르지 않는다. 메뉴 단위 call budget=1을 main에서 소유하고 fallback은 코드의 근거 목록만 표시한다.
- 이 경로는 독립적인 온라인 semantic review가 없다. source_linked 결과로 취급하며 §10 평가를 통과하기 전 품질 개선 완료로 판정하지 않는다.

#### 6.4 한도·실패 처리 표

| 경로 | 정상 논리 호출 | 실패 처리 |
| --- | --- | --- |
| interpret | 기존처럼 조각당 1회 | 기존 background 재처리 상한 유지. 의미 검토 호출을 새로 추가하지 않음 |
| reconcile | 항목당 0~1회 | 기존 처리 오류 기록·다음 주기. 자동 사실 확정으로 대체하지 않음 |
| 메뉴 1·2 | 메뉴 전체 0~1회 | 구조화 실패 후 코드 fallback. 2차 요약 금지 |
| QA 근거 없음 | 0회 | 범위+미확인 |
| QA 내용 답변 | 2회 이내 | 검토 불가 시 근거/미확인 fallback |
| QA coordination(Phase 6) | QA 1회 + 조율 1회 | 조율의 need_method 무시. B 원문 대조를 추가하는 방법은 미결정 |

Groq→Cerebras의 기존 429 전환은 동일 논리 호출의 공급자 재전송으로 계산하되 provider_attempts도 별도 기록한다. 성공한 답을 바꾸기 위한 재호출을 공급자 전환으로 숨기지 않는다. 두 공급자 모두 같은 기반 모델이면 독립 검증으로 계산하지 않는다.

### 7. 예산·출력·Structured Output

- 입력 예산: interpret 4,000, reconcile 2,000, 메뉴 각각 4,000, QA 각 호출 5,000 추정 토큰. 시스템 지침·JSON Schema·질문·원문·초안까지 포함한다. 실제 usage도 별도 기록한다.
- 기존 출력 한도를 유지하되 새 JSON 메타데이터를 포함해 적합성을 측정한다. 잘린 응답은 실패이며 유효 JSON으로 추정 복구하지 않는다.
- 배열 상한은 채워야 할 개수가 아니다. guidance.max_claims와 추출 최대 개수는 역할별 상한 안에서 출력 예산에 맞게 낮출 수 있다. 누락된 핵심 정보를 성공으로 숨기지 않고 포괄성 평가에 포함한다.
- 검사를 통과한 claim.text를 렌더러의 NOTE_CHARS 등으로 다시 자르지 않는다. 길이 위반은 생성 계약 위반으로 다루고 대체 경로를 사용한다. 근거 구절도 조건·부정이 잘린 채 사실 답변으로 출력하지 않는다.
- QA는 최대 8개 항목·8개 문장, 문장당 최대 4개 근거를 시작값으로 사용한다. 근거는 필요한 문단 단위로 넣는다. 목표 문장 수 때문에 원문을 의미가 바뀌게 자르지 않는다.
- 예산 초과 시 낮은 검색 순위의 항목과 그 문장·근거 묶음을 함께 뺀다. 대체/해결/충돌 비교의 두 증거 중 하나만 제거하면 그 관계 주장을 허용하지 않는다.
- 2차 입력에 들어가지 못한 문장은 unreviewed로 남겨 표시에서 제외한다. 초기 모델이 본 범위, 검토기가 본 범위, 최종 표시 범위를 각각 기록한다.
- pack 실패는 입력 부족으로 종료한다. QA 중 새 Notion 조회, 재추출, 새 검색 영역 확장은 하지 않는다.

공급자 적용:

1. _llm_call에 response_schema 인수를 추가하고 qa, qa-review, interpret, project-info, project-leading, reconcile의 역할별 schema를 연결한다.
2. 공식 문서에서 지원되는 정확한 provider/model 쌍에만 json_schema/strict를 설정한다. 설정 파일·토큰·모델을 임의로 교체하지 않는다.
3. 미지원 모델은 요청 전에 선택한 json_object + 동일 런타임 검증 경로를 사용한다. 사용자 호출 도중 400을 받고 느슨한 형식으로 재전송하는 숨은 재시도는 하지 않는다.
4. provider schema는 형식 제한이다. ID가 실제 입력에 있었는지, 인용 판이 맞는지, 합의 추론이 정당한지는 애플리케이션이 별도로 다룬다.
5. 메뉴/QA는 retry_invalid_json=false. 배경 처리의 기존 재시도와 사용자 호출을 구분한다.

### 8. 데이터 호환성·캐시·관측

#### 8.1 구데이터

- part_extracts의 과거 list, 현재 {items, artifact}, 새 schema_version=2를 읽는 어댑터를 둔다. 새 버전 생성 출력에는 새 계약을 엄격하게 요구한다.
- semantic이 없는 항목은 legacy_unknown으로 읽는다. 기존 title/detail/status/maturity 값을 삭제·수정하지 않는다. 표시용 상태는 원문 연결과 명시적 사용자 revision을 사용해 보수적으로 결정한다.
- 기존 evidence의 source_id·excerpt와 변경 후보를 따라 원문을 찾는다. 연결을 복구할 수 있으면 요청 안에서 EvidenceUnit을 만든다. 연결이 불명확하면 오래된 요약을 정답 근거로 대신 사용하지 않는다.
- 새 의미 메타데이터가 본문 fingerprint와 다르면 stale로 처리한다. 구버전 코드가 새 JSON 키를 유지하더라도 그 검증 상태가 오래된 내용을 인증하지 못하게 한다.
- 구클라이언트/탭의 기존 응답 필드는 유지한다. 새 support/semantic 정보는 선택 필드로 추가하고 기존 상태 enum은 확장하지 않는다.
- 새 코드는 구데이터를 읽을 수 있지만, 구서버로 되돌리면 새 의미 정책까지 유지되는 것은 아니다. 구버전 서버와 새 서버의 동시 쓰기를 지원한다고 가정하지 않는다.

#### 8.2 저장·정정

- _as_core_item, _insert_candidate, _clean_optional_attributes, _create_item/_write_item에서 semantic을 명시적으로 보존·무효화한다. 임의의 모델 키를 attributes에 통째로 복사하지 않는다.
- provenance/근거 변경도 revision과 색인 갱신 경로를 통과한다. apply의 직접 attributes UPDATE가 새 근거 이력을 누락하지 않도록 하나의 트랜잭션으로 정리한다.
- 새 guard나 프롬프트를 배포했다고 기존 잘못된 요약이 정정된 것은 아니다. 원문 기반 후보 정정은 복사본에서 차이를 검토한 후 별도 승인된 작업으로 수행한다.
- 사람 수정·삭제·되돌리기를 자동 재해석으로 덮어쓰지 않는다. 불명확한 old/new 항목은 제목만으로 병합하지 않는다.
- 이 설계 구현 과정에서 실제 DB를 자동 마이그레이션·일괄 재추출하지 않는다.

#### 8.3 캐시

- interpret 캐시 판에는 확장 지침 해시, wire schema 해시, guard_version을 포함한다. 기존 (content_hash, prompt_version) 키 구조를 유지하고 effective prompt_version에 이를 반영한다.
- 메뉴 캐시는 입력 근거 판·claims·상태 해석·schema·guard 버전이 모두 같을 때만 재사용한다. 출처 판이나 사람 revision이 바뀌면 무효다.
- QA 응답 캐시는 만들지 않는다. 같은 초안/근거를 다시 검증하더라도 과거 리뷰 결과를 새로운 사용자 질문의 답으로 자동 재사용하지 않는다.
- guard 변경으로 캐시를 무효화하는 것과 기존 지식 레코드를 수정하는 것은 별개다. 재처리량·일일 토큰 예산을 기록한다.

#### 8.4 실행 추적

기존 runs의 stage=qa/lead/interpret/reconcile 결과 JSON을 사용한다. QA에는 새 request_id를 부여하고 같은 scope 내에서도 idempotency로 응답을 재사용하지 않는다.

필수 기록: 코드 revision 또는 작업 트리 fingerprint, prompt/schema/guard 버전, 모델·공급자, 입력 원문 판, 실제 축약 후 payload, raw structured output, 검증 결과, 탈락 이유, 최종 표시 문장 ID, logical_calls/provider_attempts, 입력·출력 토큰, 지연.

추적은 앱의 로컬 저장소에만 남긴다. 저장 필드는 allowlist로 제한하며 설정 전체, 토큰, Authorization, 서명된 다운로드 URL은 기록하지 않는다. 원문이 포함된 trace·DB·백업·실데이터 평가는 Git에 넣지 않는다. 상세 원문 trace가 없는 실행은 “재현 불가”로 표시하고 해시만으로 전체 재현이 된다고 주장하지 않는다.

### 9. 수정 대상·구현 순서

아래는 B의 후속 구현 작업 목록이다. A에서 구현한 저장 성숙도 공통 함수·브리핑/QA 입력·표시를 기반으로 확장하며, 아래의 새 근거·검증 파이프라인은 아직 구현하지 않았다.

| 순서 | 대상 | 구현 계약 |
| --- | --- | --- |
| 1 | 새 worklog/grounding.py, semantic_contracts.py | EvidenceUnit, Claim, 상태 함수, budget packing, 코드 validator, 역할별 schemas |
| 2 | parts.py, prompts/interpret.md, common/evidence.md, common/states.md | ExtractV2, 필드별 근거, maturity 누락 유지, 요약 문장 근거, 과거 캐시 decoder |
| 3 | apply.py, core.py, lineage.py | semantic 보존, 합의 근거·관계 근거, same/supplement 이력, 자동 대체 조건, artifact 문장별 lineage |
| 4 | board.py | 문자열 인용 helper를 유지하는 호환 wrapper와 구조화 evidence helper. 사람 수정 후 stale 처리 |
| 5 | leading.py, prompts/project-info.md, project-leading.md | 원문 포함 입력, Claim 출력, 공통 상태, 최신성 우선의 과잉 의미 제거, 요약 캐시 |
| 6 | qa.py, prompts/qa.md, 새 prompts/qa-review.md | 1차 초안·2차 판정, need 근거 추가, 문장별 필터, answer/unknown/next 우회 차단 |
| 7 | worklog_bridge.py, main.py, prompts.py 연계 | schema 요청·역할별 재시도, 전체 호출 계수기, 메뉴 1 raw fallback 통합·검증 전 스트리밍 차단, effective cache version |
| 8 | runs.py 및 검증 도구/테스트 | provenance trace, 기존 20문항 보존, 독립 정답 세트와 모델 평가 |

핵심 함수의 제안 인터페이스:

~~~python
load_support(connection, scope, item_ids) -> EvidencePack
segment_source(snapshot, part) -> list[EvidenceUnit]
resolve_state(item, support, revision) -> ResolvedState
validate_claims(claims, pack, phase="draft|final") -> ValidationResult
pack_input(role, payload, prompt, schema, budget_tokens) -> PackedInput
validate_review(review, submitted_claims, shown_pack) -> ReviewResult
render_claims(claims, layout, pack, state, unknowns) -> RenderedAnswer
~~~

DB 접근과 생성 호출은 현재 오케스트레이터가 소유한다. grounding의 검사·조립 함수는 입력값만으로 결정되는 순수 함수를 우선한다. 공통 함수가 Notion·LLM·쓰기 작업을 몰래 수행하지 않게 한다.

### 10. 검증 기준과 평가 독립성

아래 수치는 **제안하는 출시 기준**이다. 현재 달성한 실측값이 아니다. 코드 테스트 통과와 의미 정확도 통과를 별도로 보고한다.

#### 10.1 데이터 구성

| 세트 | 수량·용도 | 정답 출처 |
| --- | --- | --- |
| 기존 실데이터 20문항 | 고정 회귀·진단. 2·3·15·16·17 및 다른 부재 단정 포함 | 원문에서 사람이 다시 작성. DB 자동 기대 근거는 비교 자료로만 보존 |
| 통제 사례 40개 | 개발용 양성/음성 쌍. 명시된 인과·중요도·합의와 없는 경우를 각각 포함 | 합성 원문과 허용·금지 명제를 사람이 먼저 확정 |
| 보류 세트 20개 | 프롬프트 선정 후 한 번 평가하는 holdout | 개발 세트와 원문·주제·표현 템플릿을 분리하고 사람이 독립 작성 |

실제 원문은 로컬 평가 자료로 유지한다. Git 픽스처에는 업무 내용을 복사하지 않은 합성 사례만 둔다. 기존 문항의 제목이 잘못된 전제를 주입하는 문제를 보기 위해 중립적인 질문 변형도 평가하되, 같은 원문 변형을 독립 holdout으로 세지 않는다.

#### 10.2 정답 작성과 누출 차단

1. 정답 작성자는 생성 답을 보기 전에 원문에서 허용 주장, 금지 주장, 상태, 불확실성, 필수 근거 구간을 작성한다.
2. 모델의 저장 요약을 원문 대신 정답으로 쓰지 않는다. 생성기가 스스로 나눈 claims만 채점하지 않고 최종 표시 텍스트를 사람이 다시 주장 단위로 나눈다.
3. 같은 모델의 리뷰 결과를 최종 정답으로 사용하지 않는다. reviewer는 시스템/모델 이름을 가리고 원문과 답을 비교한다.
4. 출시 평가 중 오류 5건·상태/인과관계 중요 사례·holdout은 두 사람이 독립 판정하고 차이를 조정한다. 수행 전까지 사람 판정은 미완료다.
5. 만델라 감사 관점에서 기존 평가의 shared hallucination/tautology, 질문 제목의 frame injection 위험을 제거한다. 새 자동 평가기도 자체 설계 점수만으로 타당성을 인증하지 않는다.

#### 10.3 지표와 출시 기준

| 지표 | 정의·기준 |
| --- | --- |
| 근거 무결성 | 잘못된 ID·다른 팀·다른 원문 판·입력에서 빠진 근거의 수락 0건 |
| 핵심 오류 재발 | 기존 5건과 합의/부재/인과/중요도 반례에서 근거 없는 단정 0건 |
| 주장 정밀도 | 사람이 분해한 최종 원자적 주장 중 원문으로 지지되는 비율 ≥98%. 100개 이상 주장으로 평가하며 빈 응답은 정밀도 N/A |
| 핵심 정보 포괄성 | 답할 수 있는 질문에서 정답의 필수 사실을 전달한 비율 ≥85%. 검색·packing 누락도 end-to-end 실패에 포함 |
| 종합 성공률 | 복수 근거가 필요한 지정 사례의 ≥80%에서 두 근거를 정확히 연결한 유용한 요약. 원문 나열만 하면 종합 성공으로 세지 않음 |
| 과잉 미확인 | 근거가 충분한 사례에서 전면 인용/답변 포기 비율 ≤10%. 판정 오거절과 검색 실패를 함께 집계 |
| 인용 정확성·포괄성 | 각 인용이 문장 전체를 실제로 지지하는지와 모든 사실 문장에 근거가 있는지 별도 측정. 표시 참조 누락은 0건 |
| 상태·불확실성 | 알려짐/미확인/명시적 미합의/가안/합의·시점·발언자 혼동 0건(중요 사례) |
| 검토기 품질 | 별도 오류 주입 사례 50개 이상에서 unsupported를 entailed로 통과시킨 비율 ≤5%. 오거절률도 보고 |
| 가독성·질문 적합성 | 사람 5점 척도 평균 ≥4. 짧거나 회피적인 답으로 정밀도만 올리는 것을 허용하지 않음 |
| 호출·예산 | 모든 분기에서 §6.4 상한. QA Notion 호출 0. 실제 전달 payload가 추정 예산 이내 |
| 비용·지연 | 동일 복사본·질문에서 현재 코드와 비교. QA p95 목표 ≤기준선 2.5배. 초과 시 원인과 결과를 보고하고 제한을 조용히 늘리지 않음 |

지표는 QA·추출 필드·artifact 요약·메뉴 1·메뉴 2별로 따로 집계한다. QA 검토가 저장 오류를 가렸어도 추출 정확성이 통과한 것으로 세지 않는다. 작은 세트의 0오류를 일반적인 무오류 확률로 해석하지 말고 분모·실패 수·불확실성을 보고한다.

#### 10.4 자동 검증과 모델 평가

- 단위: 상태 truth table, 근거 판·범위, null 처리, 유효 ID에 거짓 claim을 붙인 공격적 입력, schema·call count·fallback·budget. 원문 정규화가 제목·표의 주체·부정·조건·발언자 경계를 보존하는 합성 사례도 포함한다. 코드 참조 검사가 임의의 거짓 문장 의미까지 판별한다는 테스트 기대값을 두지 않고, 그 검출은 별도 모델·사람 평가로 확인한다.
- 통합: 합성 원문 → 추출 응답 → 실제 normalize/apply → DB 복사본 → search/gather → draft/review → 최종 표시. 추출 또는 답변 한 함수만 직접 조작해 전체 경로가 검증됐다고 하지 않는다.
- 호환: 과거 list/dict 캐시, semantic 없는 항목, 기존 인간 수정, source gone/이동, 잘린 근거, 같은 제목, superseded/resolved의 양방향 근거.
- 변형: 부정·시점·주체·수량·조건·합의 표현을 한 번에 하나만 바꿔 답이 필요한 만큼만 달라지는지 확인.
- 양성: 원문에 명시된 인과·중요도·합의가 정당하게 유지되는지 확인. “없음/때문에/주요” 같은 문자열을 모두 지워 통과하는 구현은 실패.
- 문장 탈락 뒤 본문·overview·note·제목·unknowns·버튼/안건 내용에 같은 잘못된 주장이 남는지 검사.
- QA verifier는 일부 초안 문장만 보고 통과하지 못한다. 문장 전체·모든 관련 문맥·입력에서 빠진 근거를 검사하는 계약 테스트를 둔다.
- 동일 데이터에서 A=현재 코드, B=인용 중심 대체, C=근거 연결 생성만, D=채택안(C+QA 검토)을 비교한다. D가 사실성과 유용성 기준을 함께 충족해야 한다.
- 고정 모델·프롬프트·입력에서 3회 생성 결과를 모두 기록한다. 가장 좋은 결과만 선택하지 않는다. 공급자 전환 결과는 분리 집계한다.
- NLI/SummaC 계열과 QA 기반 평가의 보완성, MiniCheck의 한국어 적합성은 오프라인 후보 평가로 남긴다. 점수를 사람 판정과 교정하기 전에 런타임 hard gate로 넣지 않는다.

### 11. 트레이드오프와 남는 한계

| 선택 | 얻는 것 | 비용·남는 한계 |
| --- | --- | --- |
| 요약과 원문을 함께 제공 | 재서술·종합을 유지하며 요약 오류의 반복을 줄임 | 문맥 예산 때문에 들어가는 항목 수가 줄 수 있음. 포괄성 측정 필요 |
| QA 초안+판정 | 새 인과·중요도·부재 단정을 직접 원문과 대조 | 기존 1회 답변보다 비용·지연 증가. 같은 모델의 오류 상관은 남음 |
| 판정 후 재생성 금지 | 검토되지 않은 수정 문장 노출 방지 | 새 근거로 전혀 새로운 답이 가능해진 경우 이번 응답의 충분성이 낮아질 수 있음 |
| 추출·메뉴 1회 유지 | 기존 운영 예산·구조 유지 | 매 실행의 독립 의미 검증 없음. 실측 품질이 부족하면 해당 경로를 완료로 선언할 수 없음 |
| 구데이터 읽기 어댑터 | 즉시 DB 변환 없이 적용 가능 | 과거 요약 오류 자체는 남음. 근거 복구 불가 시 미확인 증가 |
| 결정 정책 공통화 | 화면 간 상태 모순과 null 오판 감소 | 예전 “결정됨” 항목 일부가 “합의 여부 미확인”으로 보일 수 있음 |
| 로컬 검증 모델 보류 | 새로운 운영 의존성 최소화 | 전 단계 자동 의미 검증은 후속 교정·도입 판단 필요 |

전체 품질이 기준에 못 미치면 “Structured Output을 썼으니 정합성이 확보됐다”거나 “두 번 같은 모델이 동의했다”는 이유로 승인하지 않는다. 이 명세의 구현 완료와 Phase 5 실데이터 사람 검토 완료도 각각 확인한다.

### 12. 후속 구현 착수 및 완료 조건

착수 시 이 문서의 QA 2차 호출 역할 변경, 새 wire 계약, 메뉴 실패 후 재호출 금지, legacy 상태 보수화가 plan.md의 기존 세부 동작을 대체하는 변경임을 구현 작업에 명시한다. 기존 코드·DB·실행 서버가 이미 이 설계를 따른다고 해석하지 않는다.

완료에는 다음이 모두 필요하다.

1. §9의 실제 경로와 fallback까지 동일한 근거·상태 계약을 사용한다.
2. 새로운 앱 코드에 대한 단위·통합·관련 회귀와 §10의 의미 평가가 통과한다.
3. 기존 사람 검토 20문항을 보존하고, 정정된 원문 기준으로 사람이 판정한다.
4. 미충족 항목, 사람 판정 여부, 실행 서버 반영 여부, 설정·외부 연동 검증 범위를 각각 보고한다.
5. 실제 DB 정정·서버 반영은 별도 승인 범위에서 수행한다. B에는 추가 조율 구현과 Git 푸시가 포함되지 않는다.

B의 초기 설계 작성 때에는 구현·모델 평가를 수행하지 않았다. 이번 A 수정과 회귀 결과는 상단 범위 및 progress.md에서 관리하며, B의 의미 평가·사람 판정은 미완료다.
