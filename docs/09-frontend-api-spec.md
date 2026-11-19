# 09. 프론트엔드 API 명세

브라우저는 AG-UI로 복구 계획을 요청하고 운영자 결정을 전달한다. Next.js 서버는 Spring REST에서 장애와 저장된 실행 결과를 조회한다. Spring 조회는 현재 구현을 기준으로 하며, AG-UI와 Supervisor 보조 API는 프론트엔드·Agent 공통 계약으로 확정한다. [프론트엔드 구현 계획](08-frontend-phase-plan.md) · [Agent 구현 계획](07-agent-phase-plan.md)

## 1. 호출 경로

| 호출 주체 | 메서드와 경로 | 응답 |
| --- | --- | --- |
| 브라우저 → Caddy → Supervisor | `POST /api/ag-ui` | AG-UI SSE 스트림 |
| 브라우저 → Caddy → Supervisor | `GET /api/ag-ui/sessions`, `GET /api/ag-ui/sessions/{sessionId}`, `DELETE /api/ag-ui/sessions/{sessionId}` | 세션 실행 로그 목록·상세 조회 및 삭제 (수정 API 미제공) |
| 브라우저 → Caddy → Supervisor | `GET/POST /api/ag-ui/plans`, `PATCH/DELETE /api/ag-ui/plans/{planId}` | 플래너 작업 계획 관리 (동일 날짜 시간 겹침 시 경고 포함) |
| Next.js 서버 → Spring | `GET /api/incidents` | 장애 목록 JSON |
| Next.js 서버 → Spring | `GET /api/incidents/{incidentCode}` | 장애, 계획, 승인, 실행, 포스트모템 JSON |

`POST /api/ag-ui`에 `Content-Type: application/json`과 `Accept: text/event-stream`을 사용한다. 브라우저는 Caddy의 같은 출처에서 `@ag-ui/client`의 `HttpAgent`로 호출한다. Supervisor 보조 API(`/api/ag-ui/sessions*`, `/api/ag-ui/plans*`)는 기존 Caddy의 `/api/ag-ui*` 라우팅을 그대로 사용한다. Next.js 서버의 Spring 내부 주소는 Compose 기준 `http://backend:8080`이며 `X-Internal-Token`을 서버 런타임 환경변수에서 읽어 보낸다. 브라우저 요청과 클라이언트에 전달하는 데이터에서는 내부 토큰을 제외한다. [Caddy 설정](../infra/Caddyfile) · [AG-UI HttpAgent](https://docs.ag-ui.com/sdk/js/client/http-agent)

## 2. AG-UI 요청

표준 `RunAgentInput`에서 `threadId`는 대화를 식별하고 `runId`는 해당 요청을 식별한다. 같은 장애 대화에서 `threadId`를 유지하고 새 요청마다 `runId`를 발급한다. 장애를 전환하면 선택한 장애에 연결된 대화를 사용한다. [RunAgentInput 표준 필드](https://docs.ag-ui.com/sdk/js/core/types)

| `forwardedProps` 필드 | 형식 | 사용 규칙 |
| --- | --- | --- |
| `incidentCode` | 문자열, 필수 | 선택한 장애의 식별자 |
| `incidentStartedAt` | RFC 3339 문자열, 선택 | 배포 후보 비교에 필요한 장애 시작 시각. 모르면 사용자에게 물으며 `createdAt`과 구분한다. |
| `approvalDecision` | 객체, 선택 | `approvalId`와 `decision`을 전달한다. `decision`은 `APPROVED` 또는 `REJECTED`다. |
| `restore` | `true`, 선택 | 같은 대화의 상태를 읽는 요청. 승인 결정과 함께 보낼 수 없다. |

### 2.1 계획 요청

`messages`에 사용자 메시지를 담고, AG-UI SDK(`RunAgentInput`) 호환을 위해 `state`는 기본 `{}`를 포함한다. 아래 예시의 ID는 모의 검증용이다.

```json
{
  "threadId": "thread-009",
  "runId": "run-009-1",
  "state": {},
  "messages": [
    {
      "id": "msg-009-1",
      "role": "user",
      "content": "이전 버전의 502 비율이 1% 미만이면 롤백을 제안하고 14:28~14:50 사이에서 5분 작업 창을 찾아줘."
    }
  ],
  "tools": [],
  "context": [],
  "forwardedProps": {
    "incidentCode": "INC-2026-009",
    "incidentStartedAt": "2026-11-18T14:04:00+09:00"
  }
}
```

### 2.2 승인 또는 반려

승인·반려 버튼은 최신 스냅샷이 `AWAITING_APPROVAL`이고 `approval.decision`이 `PENDING`일 때 활성화한다. 제출 중에는 두 버튼을 잠근다.

- **운영자 신원(`approvedBy`) 결정:** 클라이언트 바디의 임의 문자열은 신뢰하지 않는다. Supervisor가 서버 측 인증 컨텍스트(초기 내부 시연에서는 환경변수 `DEFAULT_OPERATOR_ID`, 외부 공개 시 인증 미들웨어/신뢰 프록시 헤더)에서 `approvedBy`를 결정해 Spring의 `PATCH /api/incidents/{incidentCode}/approvals/{approvalId}`에 전달한다. [Spring 승인 API](02-backend-api-and-mcp-spec.md#26-운영자-승인--거절-처리)

```json
{
  "threadId": "thread-009",
  "runId": "run-009-2",
  "state": {},
  "messages": [],
  "tools": [],
  "context": [],
  "forwardedProps": {
    "incidentCode": "INC-2026-009",
    "approvalDecision": {
      "approvalId": "APR-2026-009",
      "decision": "APPROVED"
    }
  }
}
```

- **승인(`APPROVED`) 후 실행·완료 흐름:** Spring 승인 저장이 끝나면 `AWAITING_EXECUTION_RESULT` 스냅샷을 보낸 뒤, Supervisor가 `remediation-agent`(A2A)로 승인된 조치를 실행(내부 시연에서는 시뮬레이션 실행 + 복구 확인 기준 검증)하고 Spring MCP `completeIncidentWithPostMortem`으로 실행 결과와 포스트모템을 반영한다. 성공 시 `RESOLVED`, 실행 실패 시 `EXECUTION_FAILED` 스냅샷을 발행하고 `RUN_FINISHED`로 종료한다.
- **반려(`REJECTED`) 흐름:** 조치를 실행하지 않고 `REJECTED` 스냅샷과 안내 메시지를 보낸 뒤 `RUN_FINISHED`로 종료한다.
- 승인 제출 결과를 받기 전에 연결이 끊기거나 이미 처리된 승인 충돌(`409`)이 발생하면 Spring 상세를 다시 조회해 화면을 갱신한다.

### 2.3 다시 열기와 연결 복원

Spring 상세를 먼저 조회해 저장된 계획·승인·실행 상태를 복원한다. 현재 탭에서 새로고침할 때는 장애별 `threadId`를 `sessionStorage`에서 읽는다. 등록된 대화가 있으면 같은 `threadId`, 새 `runId`, `"state": {}`, 빈 `messages`와 `forwardedProps: { "incidentCode": "INC-2026-009", "restore": true }`를 보낸다. 복원 응답은 `STATE_SNAPSHOT`(최신 `plan`, `approval`, `workflowStatus`, `replanHistory`)과 `MESSAGES_SNAPSHOT`(보존된 대화 목록)을 반환하고 종료한다. 전체 단계·Tool 이력은 `GET /api/ag-ui/sessions/{sessionId}`로 조회한다.

## 3. SSE 이벤트

각 SSE 프레임의 `data:`에 AG-UI 이벤트 JSON을 담는다. 화면은 현재 요청의 `threadId`와 `runId`에 해당하는 스트림을 반영한다. 표준 이벤트의 필드 이름과 구조는 SDK 계약을 사용한다. [AG-UI 이벤트 명세](https://docs.ag-ui.com/concepts/events)

| 이벤트 | 화면 처리 |
| --- | --- |
| `RUN_STARTED` | 해당 요청의 진행 표시를 시작한다. |
| `TEXT_MESSAGE_START`, `TEXT_MESSAGE_CONTENT`, `TEXT_MESSAGE_END` | `messageId`를 기준으로 대화 본문을 갱신한다. |
| `STEP_STARTED`, `STEP_FINISHED` | 분석·계획·판정 등의 진행 상태를 기록한다. |
| `TOOL_CALL_START`, `TOOL_CALL_ARGS`, `TOOL_CALL_END`, `TOOL_CALL_RESULT` | `toolCallId`를 기준으로 작업과 결과 요약을 묶는다. |
| `STATE_SNAPSHOT` | 계획과 승인 상태를 스냅샷으로 교체한다. |
| `MESSAGES_SNAPSHOT` | 복원 시 보존된 대화 목록으로 교체한다. |
| `CUSTOM`의 `replan` | 재계획 이유와 이전·새 작업 시간을 작업 기록에 표시한다. |
| `RUN_FINISHED` | 현재 요청의 진행 표시를 끝낸다. 승인 대기와 장애 해결 여부는 스냅샷과 Spring 데이터로 판단한다. |
| `RUN_ERROR` | 사용자에게 오류 메시지를 표시하고 진행을 종료한다. 승인 제출 중이었다면 Spring 상세를 갱신한다. |

### 3.1 상태 스냅샷과 재계획 예시

`snapshot`에는 `incidentCode`, `workflowStatus`, `plan`, `approval`, `replanHistory`를 포함한다. 계획이나 승인이 없으면 해당 필드는 `null`, 재계획이 없으면 `replanHistory`는 `[]`이다. 작업 시간과 근거 요약은 Agent가 제공하며, Spring이 저장한 계획 단계의 `evidenceChunkIds`와 같은 청크 ID를 사용한다. 업무 상태는 `IDLE`, `PLANNING`, `AWAITING_APPROVAL`, `AWAITING_EXECUTION_RESULT`, `REJECTED`, `RESOLVED`, `EXECUTION_FAILED`, `BLOCKED`를 사용한다. `BLOCKED`는 근거·시간 제약 등으로 제안을 확정할 수 없는 상태다.

```text
data: {"type":"RUN_STARTED","threadId":"thread-009","runId":"run-009-1"}

data: {"type":"CUSTOM","name":"replan","value":{"reason":"변경 금지 시간과 겹칩니다.","previousWindow":{"start":"2026-11-18T14:28:00+09:00","end":"2026-11-18T14:33:00+09:00"},"nextWindow":{"start":"2026-11-18T14:40:00+09:00","end":"2026-11-18T14:45:00+09:00"}}}

data: {"type":"STATE_SNAPSHOT","snapshot":{"incidentCode":"INC-2026-009","workflowStatus":"AWAITING_APPROVAL","plan":{"planId":"PLAN-2026-009","workWindow":{"start":"2026-11-18T14:40:00+09:00","end":"2026-11-18T14:45:00+09:00"},"steps":[{"stepNo":1,"description":"결제 서비스 롤백 조치를 검토한다.","condition":"후보 버전의 502 비율이 1% 미만이다.","actionCommand":"kubectl rollout undo deployment/payment-service","evidenceChunkIds":["LOG-901","RUNBOOK-01"]}],"evidence":[{"chunkId":"LOG-901","summary":"배포 직후 커넥션 대기 시간 초과가 늘었다."}],"verificationCriteria":"결제 오류율과 커넥션 대기 시간이 정상으로 돌아온다."},"approval":{"approvalId":"APR-2026-009","planId":"PLAN-2026-009","stepNo":1,"decision":"PENDING"},"replanHistory":[{"reason":"변경 금지 시간과 겹칩니다.","previousWindow":{"start":"2026-11-18T14:28:00+09:00","end":"2026-11-18T14:33:00+09:00"},"nextWindow":{"start":"2026-11-18T14:40:00+09:00","end":"2026-11-18T14:45:00+09:00"}}]}}

data: {"type":"RUN_FINISHED","threadId":"thread-009","runId":"run-009-1"}
```

시각은 RFC 3339 오프셋을 유지하고 시연 화면에서는 KST로 표시한다. 승인 대상은 `approval.planId`와 `approval.stepNo`에 일치하는 단계로 표시한다. Tool 요약과 근거 요약은 화면에 표시할 정보만 담는다.

## 4. Spring 조회와 오류 처리

| API | 프론트에서 사용하는 응답 |
| --- | --- |
| `GET /api/incidents` | `200`과 장애 배열. 목록에서 `incidentCode`, `title`, `serviceName`, `severity`, `status`를 표시한다. 빈 배열은 장애가 없는 상태다. |
| `GET /api/incidents/{incidentCode}` | `200`과 `{ incident, plans, approvals, executions, postMortem }`. 장애 미존재 시 `404`다. |

상세 화면은 `plans[].planId`와 `approvals[].planId`로 계획과 승인을 연결하고, `approvalId`로 승인과 실행을 연결한다. 기존 이력은 `planId`와 `stepNo`가 비어 있을 수 있다. `postMortem`이 `null`이면 작성된 포스트모템이 없는 상태다. 실행 결과에는 `executionStatus`와 `verificationSummary`를 사용한다. 작업 시간과 근거 요약은 AG-UI 스냅샷에서 제공한다. [Spring REST 및 MCP 명세](02-backend-api-and-mcp-spec.md)

계획을 만들기 전 장애 상세 응답 예시다.

```json
{
  "incident": {
    "id": 9,
    "incidentCode": "INC-2026-009",
    "title": "결제 서비스 502 오류 증가",
    "serviceName": "payment-service",
    "severity": "SEV1",
    "status": "OPEN",
    "summary": "결제 요청 오류율이 상승했다.",
    "createdAt": "2026-11-18T14:06:00",
    "updatedAt": "2026-11-18T14:06:00"
  },
  "plans": [],
  "approvals": [],
  "executions": [],
  "postMortem": null
}
```

- Spring이 `401`을 반환하면 서버 연결 오류로 표시한다. `404`는 선택한 장애를 찾을 수 없다는 안내를 표시한다.
- Supervisor는 스트림을 열었다면 HTTP `200`과 `text/event-stream`으로 응답한다. 스트림을 열기 전 요청 스키마 오류(FastAPI `RequestValidationError` 포함)와 잘못된 요청은 HTTP `400`, 이미 처리된 승인 등의 충돌은 HTTP `409`로 통일한다. 스트림을 연 뒤에는 `RUN_ERROR`의 `message`와 선택 필드인 `code`로 오류를 전달한다. 승인 상태 충돌이나 전송 중단 뒤에는 상세를 조회해 현재 상태에 맞춰 버튼을 표시한다.
- 화면과 SSE에는 사용자에게 필요한 오류 메시지를 전달한다. 내부 토큰, 외부 API 자격증명, 서버 스택 트레이스는 서버에서 관리한다.
