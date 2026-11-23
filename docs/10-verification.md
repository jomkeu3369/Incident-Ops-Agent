# 10. 검증 결과서

**Incident-Ops-Agent**의 대상 도메인, 기능별 구현 위치와 검증 근거, 대표 시나리오 5종, Planner·Worker 분리 효과, 자동화 테스트 현황, 그리고 아직 검증하지 못한 범위를 정리합니다.

> 학습 과제 명세(`F3001~F3023`, `NF3001~NF3007`) 기준으로 작성했던 원본 매핑표는 [archive/requirements-mapping.md](archive/requirements-mapping.md)에 보관되어 있습니다. 이 문서는 그 번호 체계 대신 기능 이름을 기준으로 합니다.

---

## 1. 대상 도메인과 안전 제어

* **도메인:** 실시간 서비스 장애 대응 및 롤백 작업 창 플래너 (`payment-service` 502 오류·DB 커넥션 풀 고갈 시나리오)
* **외부 API 3종:**
  1. GitHub Deployments API — 현재 기준 배포와 이전 롤백 후보 커밋 SHA 목록 조회
  2. Grafana Cloud Prometheus API — 동일 관측 구간에서 기준 배포와 후보 버전의 HTTP 502 비율 조회
  3. Google Calendar FreeBusy API — 허용 작업 범위 내 변경 금지 구간 조회 (로컬 JSON 어댑터로 교체 가능)
* **Tool 결과를 제약 조건과 비교해야 답할 수 있는 대표 요청:**
  1. *"이전 버전의 502 비율이 1% 미만이면 롤백을 제안하고 14:28~14:50 사이에서 5분 작업 창을 찾아줘."* → LLM만으로는 실제 배포 SHA, 502 비율, 변경 금지 일정을 알 수 없음.
  2. *"변경 금지 시간과 겹치면 기존 조회 결과를 재사용해서 다음 가능한 5분 작업 창으로 다시 잡아줘."* → 14:28~14:33 충돌 시 14:40~14:45로 결정론적 재계획.
  3. *"같은 조건에서 작업 시간만 10분으로 바꿔서 다시 확인해줘."* → 대화 맥락 유지 및 변경 조건만 갱신.
* **저장·조회·수정·삭제 결과물:** 플래너 작업 계획(`PLAN-*`, 날짜·시작/종료 시각·조치 명령·비고, 같은 날짜 시간 겹침 경고 포함) 및 Spring 승인·실행·포스트모템 원장.
* **위험 도메인 안전 제어:** 운영 환경 롤백은 고위험 조치이므로 에이전트가 임의 실행하지 않고 AG-UI 인터럽트 + Spring 승인 원장(`APPROVED` 검증, 미승인 시 `409 Conflict`)을 통과해야만 실행·완료됩니다.

---

## 2. 기능별 구현 위치와 검증 근거

### 2.1 외부 API Tool

| 기능 | 구현 위치 | 검증 근거 |
| :--- | :--- | :--- |
| Tool 명세 정의·등록 | `agent/src/common/tools.py`, `backend/.../IncidentMcpTools.java` | `test_phase2a_tools_and_decisions.py` |
| 롤백 후보 조회 | `ExternalToolsClient.list_rollback_candidates` | `test_phase2a_tools_and_decisions.py` |
| 후보 지표 조회 | `ExternalToolsClient.get_release_metrics` (`INSUFFICIENT_DATA` 구분) | `test_phase2a_tools_and_decisions.py` |
| 변경 금지 시간 조회 | `ExternalToolsClient.get_change_freeze_windows` | `test_phase2a_tools_and_decisions.py` |
| 교체형 캘린더 어댑터 | `agent/src/common/calendar_adapter.py` (`GoogleCalendarFreezeAdapter` ↔ `LocalFreezeCalendarAdapter`, `mtime_ns` 자동 재로딩) | `test_phase3_advanced.py` |

### 2.2 계획·실행·판정

| 기능 | 구현 위치 | 검증 근거 |
| :--- | :--- | :--- |
| 요청 분석 | `agent/src/supervisor/graph.py` (`analyze_node`, `_extract_constraints`) | `test_phase2c`, `test_phase2d` |
| 작업 계획 수립 | `graph.py` (`plan_node`, 일반 대화 시 Tool 생략) | `test_phase2d` 시나리오 5 |
| Tool 호출·결과 통합 | `graph.py` (`execute_node`), `remediation_agent/services.py` | `test_phase2b`, `test_phase2d` 시나리오 4 |
| 제약 적합성 판단 | `agent/src/common/planner_math.py`, `agent/src/common/decisions.py` | `test_phase2a` |
| 재계획 | `evaluate_node`, `_call_or_reuse` 캐시 재사용, 상한 3회 | `test_phase2d` 시나리오 2·3 |
| 대화 맥락 유지 | `SupervisorRedisStore` (`memory:{thread_id}`), 조건 부분 갱신 | `test_phase2c`, `test_phase2d` |
| 최종 응답 생성 | `respond_node` (근거 청크 ID, 제외 후보 사유, 승인 안내) | `test_phase2c`, `test_phase2d` |
| Planner·Worker 분리 | `agent/src/common/worker_executor.py` (`SplitWorkerExecutor`) | `test_phase3_advanced.py` |

### 2.3 관측·로그·계획 관리

| 기능 | 구현 위치 | 검증 근거 |
| :--- | :--- | :--- |
| 실행 이벤트 발행 | `agent/src/common/event_log.py` (`EventBus`, 구독자 비의존) | `test_phase2c` |
| 실행 로그 저장 | `LoggingSubAgent` (LLM 0회, 세션 단위 Redis 원본 저장) | `test_phase2c` |
| 실행 로그 조회·삭제 | `GET/DELETE /api/ag-ui/sessions*` (수정 API 없음), `sessions-panel.tsx` | `test_phase2c`, `admin-phase4.test.ts` |
| 이상 징후 판단 | `agent/src/common/anomaly.py`, `GET /api/ag-ui/sessions/{id}/anomalies` (규칙 3종, `UNDETERMINED`, 이상 건에만 LLM 3문장 진단) | `test_phase3_advanced.py` |
| 계획 저장·조회 | `GET/POST /api/ag-ui/plans` (날짜 포함·시간 겹침 경고), `plans-panel.tsx` | `test_phase2c`, `admin-phase4.test.ts` |
| 계획 수정·삭제 | `PATCH/DELETE /api/ag-ui/plans/{plan_id}`, `plans-panel.tsx` | `test_phase2c`, `admin-phase4.test.ts` |

### 2.4 서비스 구성·화면

| 기능 | 구현 위치 | 검증 근거 |
| :--- | :--- | :--- |
| 백엔드 API 서버 | FastAPI 3종(`supervisor`, `log_agent`, `remediation_agent`) + Spring Boot | `test_bootstrap_security_di.py`, Maven 23건 |
| 프론트엔드 화면 | `frontend/app/workspace.tsx`, `plans-panel.tsx`, `sessions-panel.tsx` | `ag-ui-phase2`, `backend-phase3`, `admin-phase4` |
| 실행 과정·상태 표시 | 단계·Tool 이력·재계획 구분·로딩·오류 안내·실행 재개·포스트모템 | `ag-ui-phase2.test.ts`, `backend-phase3.test.ts` |
| 모듈 분리 | `backend/`, `agent/src/{common,log_agent,remediation_agent,supervisor}`, `frontend/app/` | 저장소 구조 |

### 2.5 안전·비기능

| 기능 | 구현 위치 및 핵심 동작 | 검증 근거 |
| :--- | :--- | :--- |
| API 키 관리 | `.env` + `SecretStr` + Next.js 서버 런타임 환경변수 전용 | `test_bootstrap_security_di.py` |
| 화면 노출 제한 | 예외 핸들러 마스킹, SSR HTML 및 BFF JSON 토큰 미포함 검증 | `test_phase2d`, `backend-phase3.test.ts` |
| Tool 실패 처리 | 공통 `{ok: false, error}` 반환, 1종 실패 시 서비스 미중단 | `test_phase2d` 시나리오 4 |
| 재계획 상한 | `MAX_REPLAN_ATTEMPTS=3` 초과 시 중단 및 사유 응답 | `test_phase2d` 시나리오 3 |
| 응답 시간 | 스트리밍 SSE 즉시 전송 및 Tool 캐시 재사용으로 중복 호출 제거 | `test_phase2c`, `test_phase2d` |
| 외부 API 출처·이용 조건 | `README.md` §5, `docs/05-external-api-tools.md` | 문서 확인 |

> **범위 밖:** 벡터 임베딩 기반 RAG 검색 Tool은 적용하지 않았습니다. 대신 DR-DCI 메타데이터 색인 기반 선별 인출을 사용합니다.

---

## 3. 대표 시나리오 5종 검증 기록

| # | 시나리오 | 기대 동작 | 실제 검증 결과 | 로그 샘플 / 테스트 |
| :---: | :--- | :--- | :--- | :--- |
| 1 | **제약 한 번에 충족** | 변경 금지 구간 없음 → 14:28–14:33 초안 확정, `SUPPORTED` 판정, `AWAITING_APPROVAL` 등록 | 기대와 일치 (재계획 0회, 계획·승인 원장 등록 완료) | [session-log-normal.json](../agent/samples/session-log-normal.json), `test_phase2d` |
| 2 | **시간 충돌 재계획** | 14:28–14:33이 14:30–14:40 금지 구간과 충돌 → 캐시 재사용(중복 Tool 호출 0건) 후 14:40–14:45로 재계획 | 기대와 일치 (`CUSTOM replan` 발행, 외부 Tool 중복 호출 0건) | [session-log-replan.json](../agent/samples/session-log-replan.json), `test_phase2d` |
| 3 | **재계획 상한(3회) 도달** | `NEED_MORE_EVIDENCE` 반복 시 3회 한도에서 중단하고 `BLOCKED`와 사유 응답 | 기대와 일치 (무한 루프 차단, 사유 안내 확인) | `test_phase2d` |
| 4 | **Tool 1종 실패** | Grafana 또는 Calendar 호출 실패 시 예외 미전파, 서비스 정상 응답 및 사유 안내 | 기대와 일치 (API 키·스택트레이스 미노출, `BLOCKED` 안전 안내) | `test_phase2d` |
| 5 | **Tool 불필요 요청** | `"안녕하세요, 어떤 기능을 지원하나요?"` → `general_chat` 분류, 외부 Tool·A2A 호출 0건 | 기대와 일치 (즉시 기능 안내 응답, Tool 호출 0회) | `test_phase2d` |

---

## 4. Planner·Worker 분리 효과 (정상 3단계 기준)

| 지표 | 통합형 (Monolithic) | Planner·Worker 분리형 (`SplitWorkerExecutor`) | 차이 |
| :--- | :---: | :---: | :---: |
| **총 LLM 호출 수** (3단계 정상 경로) | 4회 (계획 1 + 단계별 3) | **1회** (Planner 1 + Worker 0) | 75.0% 감소 |
| **Worker LLM 호출 수 (정상 경로)** | 3회 | **0회** | 결정론적 직접 실행 |
| **비수정 실패 (`AUTH_FAILED`) 시 Worker LLM** | 재시도 판단 호출 | **0회** (즉시 중단) | 불필요한 호출 차단 |
| **수정 가능 실패 (`NO_RESULT`) 시 고정 인자 보존** | 프롬프트 의존(변조 위험) | **코드 강제 보존** (`fixed_args` 덮어쓰기) | 서비스명·장애코드 불변 보장 |

> [!NOTE]
> 이 표의 통합형 수치는 통합형 구현을 실제로 같은 입력으로 실행해 측정한 값이 아니라, 호출 구조에서 계산한 값입니다. 실측 비교는 [README 로드맵](../README.md#로드맵-계획)의 평가 하네스 이후에 대체할 예정입니다.

---

## 5. 자동화 테스트

문서화 시점에 기록한 결과이며, 이 문서를 개정하면서 다시 실행한 값은 아닙니다.

* **Backend (`backend/`):** Maven 테스트 **23건** (REST, MyBatis 트랜잭션·멱등성, 승인 검증, MCP 4종)
* **Agent (`agent/`):** `pytest agent/tests` **55건** (DI·보안 4, Tool·Decisions 10, MCP·A2A 4, AG-UI·로깅 2, 시나리오 5종·비기능 11, Redis 복원력·동시성·보안 14, 심화·회귀 10)
* **Frontend (`frontend/`):** `tsc --noEmit`, `next build`, `npm test` **16건** (AG-UI `HttpAgent` 실 SSE, Spring SSR/BFF·토큰 미노출, KST 정규화, 계획 CRUD·겹침 경고, 실행 로그·이상 징후 분석)
* **합계:** 94건

---

## 6. 아직 검증하지 못한 범위

| 항목 | 현재 상태 |
| :--- | :--- |
| 근거 판정 정확도 | 합성 사례 3종 중 2종만 실행(`docs/04` 참고). 정답 라벨이 있는 평가 세트와 위험 오판율(근거 부족인데 `SUPPORTED`) 측정은 아직 없음 |
| 실제 복구 실행 | 승인 후 실행은 시뮬레이션이며 실제 클러스터 명령·복구 지표 확인은 검증하지 않음 |
| 시나리오 다양성 | `payment-service` 502·커넥션 풀 고갈 1종 |
| 외부 API 실연동 | 테스트는 대부분 모의 응답 기반. 실제 GitHub·Grafana·Calendar 호출은 환경변수 설정 후 수동 확인 |
| 사용자 인증·승인자 신원 | 서비스 간 내부 토큰만 있으며 승인자 신원 검증은 없음 |
| 배포·CI | 로컬 Docker Compose 기준. CI와 공개 배포 없음 |

위 항목의 개선 계획은 [README의 현재 범위와 로드맵](../README.md#3-현재-범위와-로드맵)에 있습니다.
