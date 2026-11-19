# 03. Phase 1 구현 체크리스트 및 완료 기준

## Phase 1A: REST API · MyBatis · DB 구현 및 검증
- [ ] `backend/pom.xml` 및 `application.yml` 구성 (Java 21, Spring Boot `3.4.5`, MyBatis, MySQL 8.0, Actuator)
- [ ] Flyway 마이그레이션 적용 확인 (6개 테이블: `incidents`, `corpus_chunks`, `remediation_plans`, `remediation_approvals`, `remediation_executions`, `post_mortems`)
- [ ] `demo` 프로필에서 시드 데이터 확인:
  - 장애 1건: `INC-2026-001` (`payment-service`, `SEV1`, `OPEN`)
  - `LOG` 청크 6건 (`LOG-101` ~ `LOG-106`)을 `INC-2026-001`에 연결한다. `LOG-104`, `LOG-105`에 커넥션 대기 시간 초과와 누수 단서를 둔다.
  - `RUNBOOK` 청크 2건 (`RUNBOOK-01`, `RUNBOOK-02`): DB 커넥션 풀 고갈 대응 롤백 절차 포함
  - 계획 1건: `PLAN-2026-001`을 `INC-2026-001`에 연결한다. 단계 2에 롤백 조치와 근거 청크 ID를 저장하고 오류율과 커넥션 대기 시간의 정상화를 복구 기준으로 둔다.
  - 승인 원장 1건: `APR-2026-001` (`PLAN-2026-001`, 단계 2, `APPROVED`, `oncall-lead`)
- [ ] MyBatis Mapper 및 `IncidentService` (`approvalId` 검증, 멱등성, `@Transactional` 3단계 원자성) 구현
- [ ] REST Controller 및 `@RestControllerAdvice` 구현
- [ ] **Phase 1A 검증 통과:**
  1. `MockMvc` 테스트: `POST /api/incidents` (`201`), 미존재 `GET /api/incidents/INC-9999` (`404`), 미승인 `approvalId` 완료 요청 거부 (`409`)
  2. 트랜잭션 테스트: 3번째 DB 업데이트 예외 시 앞선 2개 INSERT 롤백 확인 및 동일 `approvalId` 재호출 멱등성 확인
  3. 계획·승인 검증: 다른 장애의 로그나 계획을 연결할 수 없고, 계획에 없는 단계는 승인할 수 없음. 성공 완료 요청에 복구 확인 결과가 필요하며 실패를 기록한 조치를 재시도할 때는 새 승인이 필요함.

## Phase 1B: Spring AI MCP Server 부착 및 실통신 검증
- [ ] Spring AI `1.0.0` BOM 및 `spring-ai-starter-mcp-server-webmvc` 추가 (HTTP+SSE `/sse`, `/mcp/message` 설정)
- [ ] `IncidentService` 와 `CorpusMapper` 를 위임 호출하는 MCP `@Tool` 4종 등록
- [ ] `backend/Dockerfile` 작성 (Maven 멀티 스테이지 빌드 + 런타임 `wget` 포함)
- [ ] **Phase 1B 검증 통과:**
  1. 백엔드 기동 후 Python MCP 클라이언트(`mcp.client.sse.sse_client`)로 `http://localhost:8080/sse` 접속
  2. `tools/list` 로 4개 도구 스키마 조회 확인
  3. `searchCorpusIndex("INC-2026-001", "LOG", "HikariPool")` → `pullCorpusChunk("INC-2026-001", "LOG-104")` → `completeIncidentWithPostMortem`에 승인 ID와 복구 확인 결과 전달 및 MySQL 반영 확인

## 복구 플래너 연동 검증

- [ ] [외부 API Tool 3종](05-external-api-tools.md)을 실제 호출해 입출력과 실패 응답을 확인한다.
- [ ] [결제 장애 시나리오](05-external-api-tools.md#5-결제-장애-시나리오와-재계획)에서 GitHub 배포 SHA로 지표를 조회하고, 변경 금지 시간과 겹치면 기존 결과를 재사용해 작업 창을 다시 잡는지 확인한다.
- [ ] [판정 실험](04-domain-planner-and-decisions.md#4-실험에서-확인할-것)의 근거 충분·부족·충돌 사례에 기대 판정을 정하고 실제 API 결과와 비교한다.
- [ ] 근거가 부족하면 범위 내 추가 조회, 근거와 충돌하면 계획 수정이 이루어지는지 확인한다.
- [ ] 판정에서 근거가 충분하면 AG-UI에 계획과 청크 ID를 표시한다. 조치 결과 반영 시 백엔드가 운영자의 승인을 검증하는지 확인한다.
