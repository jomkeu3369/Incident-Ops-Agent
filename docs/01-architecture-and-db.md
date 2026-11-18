# 01. 아키텍처 계약 및 데이터베이스 스키마 명세

## 1. 기술 스택 및 호환성 계약
기존 루트 인프라 설정(`.env.example`, `docker-compose.yaml`, `docker-compose-infra.yaml`, `infra/Caddyfile`)과 정확히 일치해야 한다[cite: 1].

전체 구성과 결제 장애 처리 흐름은 [스택 다이어그램](06-stack-diagram.md)에서 확인한다.

* **Runtime & Framework:** Java 21, Spring Boot `3.4.5`, Maven (`backend/pom.xml`)
* **Persistence:** MyBatis (`mybatis-spring-boot-starter`), **MySQL 8.0** (`incident_ops`)[cite: 1]
* **MCP Server:** Spring AI `1.0.0` BOM (`spring-ai-starter-mcp-server-webmvc`)
* **도메인 플래너:** DR-DCI로 모은 근거를 바탕으로 복구 계획을 작성하고, 판정 API로 조치의 근거를 검토한 뒤 운영자에게 승인을 요청한다. [플래너 흐름과 판정 기준](04-domain-planner-and-decisions.md)
* **MCP 전송 규약 (HTTP+SSE 고정):**
  * `spring.ai.mcp.server.type=SYNC`
  * `spring.ai.mcp.server.sse-endpoint=/sse`[cite: 1]
  * `spring.ai.mcp.server.sse-message-endpoint=/mcp/message`
* **인프라 엔드포인트:**
  * 서버 포트: `8080` (`SERVER_PORT`)[cite: 1]
  * 헬스체크: `/actuator/health` (`docker-compose.yaml` 헬스체크 대상)[cite: 1]
  * 내부 서비스 토큰 헤더: `X-Internal-Token` (`INTERNAL_SERVICE_TOKEN` 환경변수 검증, 로컬 개발 프로파일에서는 비활성화 가능하며 `/actuator/health` 는 항상 허용)[cite: 1]

---

## 2. MySQL 8.0 테이블 설계 (`schema.sql`)

편집 가능한 ERD는 [db.vuerd.json](ERD/db.vuerd.json)에 둔다. 외래 키의 삭제·변경 정책은 `RESTRICT`로 지정한다.

### 2.1 `incidents` (장애 티켓)
| 컬럼명 | 타입 | 제약조건 | 설명 |
| :--- | :--- | :--- | :--- |
| `id` | `BIGINT` | PK, AUTO_INCREMENT | 내부 식별자 |
| `incident_code` | `VARCHAR(32)` | UNIQUE, NOT NULL | 비즈니스 식별자 (예: `INC-2026-001`) |
| `title` | `VARCHAR(200)` | NOT NULL | 장애 제목 |
| `service_name` | `VARCHAR(100)` | NOT NULL | 대상 서비스명 (예: `payment-service`) |
| `severity` | `VARCHAR(16)` | NOT NULL | `SEV1`, `SEV2`, `SEV3` |
| `status` | `VARCHAR(32)` | NOT NULL | `OPEN`, `ANALYZING`, `WAITING_APPROVAL`, `RESOLVED` |
| `summary` | `TEXT` | NULL | 장애 요약 |
| `created_at` | `DATETIME` | NOT NULL | 생성 일시 |
| `updated_at` | `DATETIME` | NOT NULL | 수정 일시 |

### 2.2 `corpus_chunks` (DR-DCI 대상 로그 및 런북 코퍼스)
| 컬럼명 | 타입 | 제약조건 | 설명 |
| :--- | :--- | :--- | :--- |
| `id` | `BIGINT` | PK, AUTO_INCREMENT | 내부 식별자 |
| `chunk_id` | `VARCHAR(32)` | UNIQUE, NOT NULL | 청크 식별자 (예: `LOG-101`, `RUNBOOK-01`) |
| `corpus_type` | `VARCHAR(16)` | NOT NULL | `LOG` 또는 `RUNBOOK` |
| `incident_code` | `VARCHAR(32)` | NULL, FK → `incidents.incident_code` | 로그는 소속 장애 필수, 런북은 NULL |
| `service_name` | `VARCHAR(100)` | NOT NULL | 관련 서비스명 |
| `title` | `VARCHAR(200)` | NOT NULL | 청크 제목 / 구간 요약 |
| `keywords` | `VARCHAR(255)` | NULL | 검색용 키워드 |
| `content` | `MEDIUMTEXT` | NOT NULL | 실제 로그 또는 매뉴얼 본문 |
| `sequence_order` | `INT` | NOT NULL | 정렬 순서 |

로그의 `service_name`은 소속 장애의 서비스명과 일치해야 한다. 런북은 서비스 단위로 재사용한다. 계획에서 인용한 근거를 보존할 수 있도록 청크의 본문을 교체할 때는 새 `chunk_id`를 발급한다.

### 2.3 `remediation_plans` (복구 계획)
| 컬럼명 | 타입 | 제약조건 | 설명 |
| :--- | :--- | :--- | :--- |
| `id` | `BIGINT` | PK, AUTO_INCREMENT | 내부 식별자 |
| `plan_id` | `VARCHAR(64)` | UNIQUE, NOT NULL | 운영자가 검토하는 계획의 식별자 |
| `incident_code` | `VARCHAR(32)` | NOT NULL, FK → `incidents.incident_code` | 대상 장애 코드 |
| `steps` | `JSON` | NOT NULL | 단계 순서, 진행 조건, 조치, 근거 청크 ID |
| `verification_criteria` | `TEXT` | NOT NULL | 복구 여부를 판단할 기준 |
| `created_by` | `VARCHAR(100)` | NOT NULL | 계획 제안 주체 |
| `created_at` | `DATETIME` | NOT NULL | 계획 생성 일시 |

`steps`는 배열이며 각 단계는 `stepNo`, `description`, `condition`, `evidenceChunkIds`를 가진다. MVP에서는 최대 한 단계에 `actionCommand`를 넣고 그 단계만 승인할 수 있다. 저장 시 단계 번호가 중복되지 않는지, 근거가 해당 장애의 로그 또는 해당 서비스의 런북인지 확인한다. 저장된 계획의 수정본은 새 `plan_id`로 생성하며 수정본의 조치는 별도 승인을 받는다.

### 2.4 `remediation_approvals` (HITL 승인 원장)
| 컬럼명 | 타입 | 제약조건 | 설명 |
| :--- | :--- | :--- | :--- |
| `id` | `BIGINT` | PK, AUTO_INCREMENT | 내부 식별자 |
| `approval_id` | `VARCHAR(64)` | UNIQUE, NOT NULL | 승인 요청 식별자 (예: `APR-2026-001`) |
| `plan_id` | `VARCHAR(64)` | NOT NULL, FK → `remediation_plans.plan_id` | 운영자가 검토할 계획 |
| `step_no` | `INT` | NOT NULL | 계획의 승인 대상 단계 번호 |
| `action_command` | `TEXT` | NOT NULL | 승인 요청 시 복사한 해당 단계의 명령어 |
| `proposed_by` | `VARCHAR(100)` | NOT NULL | 제안 주체 (예: `supervisor-agent`) |
| `approved_by` | `VARCHAR(100)` | NULL | 승인/거절 처리자 |
| `decision` | `VARCHAR(16)` | NOT NULL | `PENDING`, `APPROVED`, `REJECTED` |
| `created_at` | `DATETIME` | NOT NULL | 요청 일시 |
| `decided_at` | `DATETIME` | NULL | 승인/거절 결정 일시 |

### 2.5 `remediation_executions` (복구 실행 및 멱등성 감사 로그)
| 컬럼명 | 타입 | 제약조건 | 설명 |
| :--- | :--- | :--- | :--- |
| `id` | `BIGINT` | PK, AUTO_INCREMENT | 내부 식별자 |
| `approval_id` | `VARCHAR(64)` | UNIQUE, NOT NULL, FK → `remediation_approvals.approval_id` | 승인당 실행 결과는 최대 1건 |
| `action_command` | `TEXT` | NOT NULL | 실제 실행된 명령어 |
| `executed_by` | `VARCHAR(100)` | NOT NULL | 실행 주체 |
| `execution_status` | `VARCHAR(16)` | NOT NULL | `SUCCESS` 또는 `FAILED` |
| `execution_output` | `TEXT` | NULL | 실행 결과 로그 |
| `verification_summary` | `TEXT` | NULL | 계획의 복구 확인 기준을 검사한 결과. 성공 시 필수 |
| `executed_at` | `DATETIME` | NOT NULL | 실행 일시 |

실행과 승인의 대상 장애는 연결된 복구 계획에서 조회한다. 실패를 기록한 승인 ID로 같은 결과를 재전송할 수 있으며, 실제 조치를 다시 시도하려면 새 승인 ID가 필요하다.

### 2.6 `post_mortems` (사후 장애 분석 리포트)
| 컬럼명 | 타입 | 제약조건 | 설명 |
| :--- | :--- | :--- | :--- |
| `id` | `BIGINT` | PK, AUTO_INCREMENT | 내부 식별자 |
| `incident_code` | `VARCHAR(32)` | UNIQUE, NOT NULL, FK → `incidents.incident_code` | 장애당 포스트모템은 최대 1건 |
| `root_cause` | `TEXT` | NOT NULL | 근본 원인(RCA) 요약 |
| `timeline_markdown` | `TEXT` | NOT NULL | 장애 대응 타임라인 (Markdown) |
| `remediation_summary` | `TEXT` | NOT NULL | 수행된 복구 조치 요약 |
| `prevention_items` | `TEXT` | NOT NULL | 재발 방지 대책 |
| `created_at` | `DATETIME` | NOT NULL | 생성 일시 |

---

## 3. MyBatis Mapper 핵심 규칙
* **`CorpusMapper.searchMetadata(incidentCode, corpusType, keyword)`:** 대상 장애에 속한 로그 또는 대상 장애의 서비스에 속한 런북을 검색한다. `chunk_id`, `corpus_type`, `service_name`, `title`, `keywords`, `CHAR_LENGTH(content) AS content_length`만 반환한다.
* **`CorpusMapper.findByChunkId(incidentCode, chunkId)`:** 같은 범위 규칙을 적용해 단건의 메타데이터와 `content` 원문을 반환한다.
