# 03. Phase 1 구현 체크리스트 및 완료 기준

## Phase 1A: REST API · MyBatis · DB 구현 및 검증
- [ ] `backend/build.gradle` 및 `application.yml` 구성 (Java 21, Spring Boot `3.4.5`, MyBatis, MySQL 8.0, Actuator)
- [ ] `schema.sql` 작성 (5개 테이블: `incidents`, `corpus_chunks`, `remediation_approvals`, `remediation_executions`, `post_mortems`)
- [ ] `data.sql` 시드 데이터 작성:
  - 장애 1건: `INC-2026-001` (`payment-service`, `SEV1`, `OPEN`)
  - 승인 원장 1건: `APR-2026-001` (`INC-2026-001`, `APPROVED`, `oncall-lead`, `kubectl rollout undo deployment/payment-service`)
  - `LOG` 청크 6건 (`LOG-101` ~ `LOG-106`): 노이즈 로그 사이에 `LOG-104`, `LOG-105` 에 `HikariPool-1 - Connection is not available, request timed out after 30000ms` 및 커넥션 누수 단서 배치
  - `RUNBOOK` 청크 2건 (`RUNBOOK-01`, `RUNBOOK-02`): DB 커넥션 풀 고갈 대응 롤백 절차 포함
- [ ] MyBatis Mapper 및 `IncidentService` (`approvalId` 검증, 멱등성, `@Transactional` 3단계 원자성) 구현
- [ ] REST Controller 및 `@RestControllerAdvice` 구현
- [ ] **Phase 1A 검증 통과:**
  1. `MockMvc` 테스트: `POST /api/incidents` (`201`), 미존재 `GET /api/incidents/INC-9999` (`404`), 미승인 `approvalId` 완료 요청 거부 (`409`)
  2. 트랜잭션 테스트: 3번째 DB 업데이트 예외 시 앞선 2개 INSERT 롤백 확인 및 동일 `approvalId` 재호출 멱등성 확인

## Phase 1B: Spring AI MCP Server 부착 및 실통신 검증
- [ ] Spring AI `1.0.0` BOM 및 `spring-ai-starter-mcp-server-webmvc` 추가 (HTTP+SSE `/sse`, `/mcp/message` 설정)
- [ ] `IncidentService` 와 `CorpusMapper` 를 위임 호출하는 MCP `@Tool` 4종 등록
- [ ] `backend/Dockerfile` 작성 (Gradle 멀티 스테이지 빌드 + 런타임 `wget` 포함)
- [ ] **Phase 1B 검증 통과:**
  1. 백엔드 기동 후 Python MCP 클라이언트(`mcp.client.sse.sse_client`)로 `http://localhost:8080/sse` 접속
  2. `tools/list` 로 4개 도구 스키마 조회 확인
  3. `searchCorpusIndex("LOG", "HikariPool")` → `pullCorpusChunk("LOG-104")` → `completeIncidentWithPostMortem` 호출 및 MySQL 반영 확인