# Phase 7 · 3대 프로토콜(AG-UI · A2A · MCP) 실패·복구 계약 검증 리포트

> **목적:** 연결 단절, 프로세스 재시작, 타임아웃, 중복 요청, 권한 경계 침범 상황에서도 **AG-UI 상태 · A2A 작업 · MCP 근거 · Spring 승인 원장**이 어긋나지 않음을 자동화 계약 테스트로 실증한 기록입니다.

---

## 1. AG-UI · 영속 LangGraph 체크포인트 재개 검증 (3개 지점 × 10회 = 30회)

- **구현:** `agent/src/supervisor/checkpointer.py`(`RedisCheckpointSaver`, `BaseCheckpointSaver`), `supervisor/graph.py`(`execute_log` → `execute_remediation` 체크포인트 경계 분리), `supervisor/services.py`(`resume`, `explicitCancel`, `checkpoint_sync` 순번 동기화)

| 중단 지점 | 반복 횟수 | 완료 단계 불필요 재호출 | 실행·포스트모템 중복 | UI–Spring 상태 불일치 | 검증 결과 |
| :--- | :---: | :---: | :---: | :---: | :---: |
| **지점 1:** 근거 수집(`execute_log`) 완료 후 중단 → 새 프로세스 재개 | 10회 | **0건** (`log_agent` 1회만 호출) | **0건** | **0건** | ✅ PASS |
| **지점 2:** 승인 대기(`AWAITING_APPROVAL`) 중 재시작 → `restore=True` | 10회 | **0건** (중복 계획·승인 0) | **0건** | **0건** | ✅ PASS |
| **지점 3:** 완료 저장 후 응답 전 단절 → 동일 승인 재시도 | 10회 | **0건** | **0건** (멱등 재응답) | **0건** (`RESOLVED` 일치) | ✅ PASS |

- **추가 불변조건:** `explicitCancel=True`는 체크포인트를 삭제하고 `CANCELLED`로 전이하는 반면, 단순 SSE 단절은 체크포인트를 보존해 재개 가능하게 유지하며 `RUN_FINISHED`는 업무 `RESOLVED`와 분리 유지됩니다.

---

## 2. A2A · 표준 스트리밍(`streaming=True`) 및 5개 계약 시나리오

- **구현:** `agent/src/log_agent/main.py`(`AgentCapabilities(streaming=True)`), `log_agent/routers.py`(`Task` 초기화 → `TaskStatusUpdateEvent` → `TaskArtifactUpdateEvent` → `TaskStatusUpdateEvent(COMPLETED)`, `idempotencyKey` 중복 차단, `cancel` 지원), `common/a2a_utils.py`(`send_a2a_stream`, `cancel_a2a_task`)

| # | 계약 시나리오 | 검증 항목 | 결과 |
| :---: | :--- | :--- | :---: |
| 1 | **정상 스트리밍** | `SendStreamingMessage` 진행 이벤트(`searching_index` → `pulling_chunks`)가 최종 산출물보다 선행 도착(`first_progress_ms ≤ final_artifact_ms`) 및 첫 이벤트 `p95` 계측 | ✅ PASS |
| 2 | **타임아웃** | 느린 워커 호출 시 `timeout_seconds` 초과로 `TimeoutError` 안전 종료 | ✅ PASS |
| 3 | **취소** | 진행 중 스트리밍 태스크 취소(`CancelledError` / `CancelTask`) 시 안전 정리 | ✅ PASS |
| 4 | **중복 요청** | 동일 `idempotencyKey` 3회 전송 시 실제 DR-DCI 실행 1회 (**중복 작업 0건**) | ✅ PASS |
| 5 | **워커 교체** | `keyword` 워커 → `agentic` 워커로 URL만 교체해 Supervisor 코드 무수정 호환 | ✅ PASS |

---

## 3. MCP · 버전 있는 읽기 전용 리소스 및 4개 부정 계약 (허용 0건)

- **구현:**
  - Spring Backend: `application.yml`(`capabilities.resource: true`), `IncidentMcpConfig.java`(`incident://evidence-summary/v1` `SyncResourceSpecification`), `InternalRoleContext.java`, `InternalTokenFilter.java`(조사 워커 REST 쓰기 `403` 차단), `IncidentMcpTools.java`(조사 워커 `completeIncidentWithPostMortem` 거부)
  - Agent Client: `common/backend_client.py`(`list_mcp_resources`, `read_mcp_resource`, `X-Agent-Role` 헤더 전달 + `agent_role="investigator"` 쓰기 이중 차단)

| 구분 | 계약 / 시나리오 | 기대 응답 | 허용 건수 |
| :--- | :--- | :---: | :---: |
| **긍정 계약** | `list_mcp_resources` & `read_mcp_resource("incident://evidence-summary/v1")` | `schemaVersion="v1"`, `readOnly=true` | 정상 동작 |
| **부정 계약 1** | 잘못된/미설정 내부 토큰 (`X-Internal-Token`) | `401 Unauthorized` | **0건** |
| **부정 계약 2** | 타 장애 범위 청크 접근 (`pullCorpusChunk`) | `404 Not Found` / 거부 | **0건** |
| **부정 계약 3** | 조사 워커(`X-Agent-Role: investigator`)의 쓰기·완료 시도 (MCP & REST) | `403 Forbidden` / 도구 거부 | **0건** |
| **부정 계약 4** | 미승인(`PENDING`) 승인 ID로 완료 시도 (`completeIncidentWithPostMortem`) | `409 Conflict` | **0건** |

---

## 4. 공통 종단 트레이스 ID 연결

- `snapshot.trace`에 `runId` / `traceId` (AG-UI) → `a2aTaskIds` (A2A) → `evidenceChunkIds` (MCP) → `approvalId` (Spring 원장)를 연결해 장애 1건의 전 구간 감사 추적을 보장합니다.
- **재현 커맨드 (PowerShell 5.1):**
  ```powershell
  cd agent
  python -m pytest tests/test_phase7_protocol_resilience.py -v
  ```
