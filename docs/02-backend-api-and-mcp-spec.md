# 02. 백엔드 REST API 및 MCP 도구 명세

## 1. 계층 통합 원칙
* REST Controller와 Spring AI MCP `@Tool` 메서드는 비즈니스 로직을 중복 구현하지 않고 **동일한 `IncidentService` 와 `CorpusMapper` 를 호출** 한다.
* 총괄 에이전트는 [복구 계획의 근거를 판정](04-domain-planner-and-decisions.md#3-mvp의-판정-기준과-분기)해 승인 요청 여부를 정한다. 백엔드 승인 원장에는 운영자의 승인·거절을 기록한다.
* 예외 응답은 `@RestControllerAdvice` 로 통일한다:
  * 미존재 리소스: `404 Not Found`
  * 요청 유효성 오류: `400 Bad Request`
  * 미승인 상태·명령어 불일치·상태 전이 위반: `409 Conflict`

---

## 2. REST API 명세 (`/api/incidents`)

### 2.1 장애 목록 조회
* **Endpoint:** `GET /api/incidents`
* **Response:** `200 OK` — `IncidentSummaryResponse[]`

### 2.2 장애 단건 상세 조회
* **Endpoint:** `GET /api/incidents/{incidentCode}`
* **Response:** `200 OK` — `IncidentDetailResponse` (존재하지 않으면 `404 Not Found`)
* **계획 조회:** 운영자가 검토할 수 있도록 저장된 계획의 ID, 단계, 근거 청크 ID와 복구 확인 기준을 상세 응답에 포함한다.

### 2.3 신규 장애 등록
* **Endpoint:** `POST /api/incidents`
* **Request Body:**
  ```json
  {
    "incidentCode": "INC-2026-002",
    "title": "주문 서비스 DB 응답 지연",
    "serviceName": "order-service",
    "severity": "SEV2",
    "summary": "p99 latency 4.5s 초과"
  }
  ```
* **Response:** `201 Created` (`Location: /api/incidents/{incidentCode}`)

### 2.4 복구 계획 등록
* **Endpoint:** `POST /api/incidents/{incidentCode}/plans`
* **Request Body:** `planId`, `createdBy`, `steps`, `verificationCriteria`
* **동작:** 장애에 계획을 연결하고 단계 번호, 승인 대상 조치 수, 근거의 조회 범위를 검증한다. 상세 형식은 [복구 계획 스키마](01-architecture-and-db.md#23-remediation_plans-복구-계획)를 따른다.
* **Response:** `201 Created` — 저장된 계획

### 2.5 복구 작업 승인 요청 생성 (HITL 대기 등록)
* **Endpoint:** `POST /api/incidents/{incidentCode}/approvals`
* **Request Body:**
  ```json
  {
    "approvalId": "APR-2026-001",
    "planId": "PLAN-2026-001",
    "stepNo": 2,
    "proposedBy": "supervisor-agent"
  }
  ```
* **동작:** 트랜잭션에서 장애 행을 먼저 잠근다. 계획이 경로의 장애에 속하고 선택한 단계에 `actionCommand`가 있는지 검증한다. 장애가 종료되었거나 결과가 없는 승인(`PENDING` 또는 `APPROVED`)이 남아 있으면 `409 Conflict`를 반환한다. 그 외에는 명령어를 승인 레코드에 복사해 `PENDING`으로 저장하고 장애를 `WAITING_APPROVAL`로 변경한다.
* **Response:** `201 Created`

### 2.6 운영자 승인 / 거절 처리
* **Endpoint:** `PATCH /api/incidents/{incidentCode}/approvals/{approvalId}`
* **Request Body:**
  ```json
  {
    "approvedBy": "oncall-engineer",
    "decision": "APPROVED"
  }
  ```
* **Response:** `200 OK`

### 2.7 복구 완료 및 포스트모템 확정
* **Endpoint:** `POST /api/incidents/{incidentCode}/complete`
* **Request Body:**
  ```json
  {
    "approvalId": "APR-2026-001",
    "actionCommand": "kubectl rollout undo deployment/payment-service",
    "executedBy": "remediation-agent",
    "executionStatus": "SUCCESS",
    "executionOutput": "deployment.apps/payment-service rolled back",
    "verificationSummary": "복구 확인 기준을 충족함. 오류율과 DB 커넥션 대기 시간이 정상 범위로 돌아옴",
    "rootCause": "신규 배포 버전의 커넥션 누수로 인한 HikariPool 고갈",
    "timelineMarkdown": "- 14:00 502 에러 급증\n- 14:05 롤백 승인 및 복구 완료",
    "preventionItems": "커넥션 풀 누수 감지 알림 임계치 설정 및 통합 테스트 보강"
  }
  ```
* **Response:** `200 OK`

### 2.8 포스트모템 조회
* **Endpoint:** `GET /api/incidents/{incidentCode}/postmortem`
* **Response:** `200 OK` (미생성 시 `404 Not Found`)

---

## 3. 핵심 비즈니스 규칙: `IncidentService.completeIncidentWithPostMortem`

1. **승인 원장 서버 측 검증 (불일치 시 `409 Conflict`):**
   * 승인 요청과 완료가 같은 장애에 대해 동시에 상태를 바꾸지 않도록 트랜잭션에서 장애 행을 먼저 잠근다.
   * `remediation_approvals` 에 `approvalId` 가 존재해야 한다.
   * `decision` 이 반드시 `APPROVED` 여야 한다 (`PENDING`, `REJECTED` 불가).
   * 승인에 연결된 계획의 `incident_code`와 요청의 `incidentCode`가 일치해야 한다.
   * 승인 레코드의 `action_command` 와 요청된 `actionCommand` 가 일치해야 한다.
2. **멱등성과 상태 검증:**
   * 해당 `approvalId`의 실행 결과가 있으면 그대로 반환하고, 새 결과를 처리할 때 장애가 이미 `RESOLVED`이면 `409 Conflict`를 반환한다.
3. **실행 결과 분기 및 단일 `@Transactional` 원자성:**
   * `executionStatus == FAILED`인 경우: 실패 로그만 기록한다. 실제 재시도에는 새 승인이 필요하다.
   * `executionStatus == SUCCESS`인 경우: 호출자가 계획의 복구 확인 기준 충족 여부를 확인하고 `verificationSummary`에 결과를 전달해야 한다. 아래 3개 작업을 **하나의 `@Transactional`** 안에서 실행하며, 중간 실패 시 전체 롤백된다.
     1. `remediation_executions` INSERT
     2. `post_mortems` INSERT
     3. `incidents.status` 를 `RESOLVED` 로 UPDATE

---

## 4. Spring AI MCP Server `@Tool` 명세 (`com.incidentops.backend.mcp`)

| 도구명 | 입력 파라미터 | 반환값 및 동작 |
| :--- | :--- | :--- |
| `searchCorpusIndex` | `incidentCode`, `corpusType` (`LOG` \| `RUNBOOK`), `keyword` | `CorpusMapper.searchMetadata` 호출. 해당 장애의 로그 또는 해당 서비스의 런북 메타데이터 반환 |
| `pullCorpusChunk` | `incidentCode`, `chunkId` | `CorpusMapper.findByChunkId` 호출. 범위에 포함된 청크의 메타데이터와 `content` 원문 반환 |
| `getIncidentDetail` | `incidentCode` | `IncidentService.getIncidentDetail` 호출. 장애 상세 정보 반환 |
| `completeIncidentWithPostMortem` | `approvalId`, `incidentCode`, `actionCommand`, `executedBy`, `executionStatus`, `executionOutput`, `verificationSummary`, `rootCause`, `timelineMarkdown`, `preventionItems` | `IncidentService.completeIncidentWithPostMortem` 호출. 승인과 성공 요청의 복구 확인 결과를 검사하고 장애 상태와 포스트모템을 반영 |
