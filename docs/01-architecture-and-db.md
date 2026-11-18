# 01. 아키텍처 계약 및 데이터베이스 스키마 명세

## 1. 기술 스택 및 호환성 계약
기존 루트 인프라 설정(`.env.example`, `docker-compose.yaml`, `docker-compose-infra.yaml`, `infra/Caddyfile`)과 정확히 일치해야 한다[cite: 1].

* **Runtime & Framework:** Java 21, Spring Boot `3.4.5`, Gradle Groovy
* **Persistence:** MyBatis (`mybatis-spring-boot-starter`), **MySQL 8.0** (`incident_ops`)[cite: 1]
* **MCP Server:** Spring AI `1.0.0` BOM (`spring-ai-starter-mcp-server-webmvc`)
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
| `service_name` | `VARCHAR(100)` | NOT NULL | 관련 서비스명 |
| `title` | `VARCHAR(200)` | NOT NULL | 청크 제목 / 구간 요약 |
| `keywords` | `VARCHAR(255)` | NULL | 검색용 키워드 |
| `content` | `MEDIUMTEXT` | NOT NULL | 실제 로그 또는 매뉴얼 본문 |
| `sequence_order` | `INT` | NOT NULL | 정렬 순서 |

### 2.3 `remediation_approvals` (HITL 승인 원장)
| 컬럼명 | 타입 | 제약조건 | 설명 |
| :--- | :--- | :--- | :--- |
| `id` | `BIGINT` | PK, AUTO_INCREMENT | 내부 식별자 |
| `approval_id` | `VARCHAR(64)` | UNIQUE, NOT NULL | 승인 요청 식별자 (예: `APR-2026-001`) |
| `incident_code` | `VARCHAR(32)` | NOT NULL | 대상 장애 코드 |
| `action_command` | `TEXT` | NOT NULL | 승인 요청된 복구 명령어 |
| `proposed_by` | `VARCHAR(100)` | NOT NULL | 제안 주체 (예: `supervisor-agent`) |
| `approved_by` | `VARCHAR(100)` | NULL | 승인/거절 처리자 |
| `decision` | `VARCHAR(16)` | NOT NULL | `PENDING`, `APPROVED`, `REJECTED` |
| `created_at` | `DATETIME` | NOT NULL | 요청 일시 |
| `decided_at` | `DATETIME` | NULL | 승인/거절 결정 일시 |

### 2.4 `remediation_executions` (복구 실행 및 멱등성 감사 로그)
| 컬럼명 | 타입 | 제약조건 | 설명 |
| :--- | :--- | :--- | :--- |
| `id` | `BIGINT` | PK, AUTO_INCREMENT | 내부 식별자 |
| `approval_id` | `VARCHAR(64)` | UNIQUE, NOT NULL | 멱등성 보장 키 (`remediation_approvals.approval_id`) |
| `incident_code` | `VARCHAR(32)` | NOT NULL | 대상 장애 코드 |
| `action_command` | `TEXT` | NOT NULL | 실제 실행된 명령어 |
| `executed_by` | `VARCHAR(100)` | NOT NULL | 실행 주체 |
| `execution_status` | `VARCHAR(16)` | NOT NULL | `SUCCESS` 또는 `FAILED` |
| `execution_output` | `TEXT` | NULL | 실행 결과 로그 |
| `executed_at` | `DATETIME` | NOT NULL | 실행 일시 |

### 2.5 `post_mortems` (사후 장애 분석 리포트)
| 컬럼명 | 타입 | 제약조건 | 설명 |
| :--- | :--- | :--- | :--- |
| `id` | `BIGINT` | PK, AUTO_INCREMENT | 내부 식별자 |
| `incident_code` | `VARCHAR(32)` | UNIQUE, NOT NULL | 대상 장애 코드 |
| `approval_id` | `VARCHAR(64)` | UNIQUE, NOT NULL | 연결된 승인 ID |
| `root_cause` | `TEXT` | NOT NULL | 근본 원인(RCA) 요약 |
| `timeline_markdown` | `TEXT` | NOT NULL | 장애 대응 타임라인 (Markdown) |
| `remediation_summary` | `TEXT` | NOT NULL | 수행된 복구 조치 요약 |
| `prevention_items` | `TEXT` | NOT NULL | 재발 방지 대책 |
| `created_at` | `DATETIME` | NOT NULL | 생성 일시 |

---

## 3. MyBatis Mapper 핵심 규칙
* **`CorpusMapper.searchMetadata(corpusType, keyword)`:** DR-DCI 원칙에 따라 `SELECT` 절에서 `content` 컬럼을 절대 조회하지 않고, `chunk_id`, `corpus_type`, `service_name`, `title`, `keywords`, `CHAR_LENGTH(content) AS content_length` 만 반환한다.
* **`CorpusMapper.findByChunkId(chunkId)`:** 단건 `chunkId` 에 해당하는 메타데이터와 `content` 원문을 반환한다.