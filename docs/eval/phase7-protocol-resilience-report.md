# Phase 7 · 3대 프로토콜(AG-UI · A2A · MCP) 실패·복구 계약 검증 리포트

> **목적:** 연결 단절, Supervisor 강제 종료, 타임아웃, 취소, 중복 요청, 권한 경계 침범 상황에서 **화면 상태 · A2A 작업 · MCP 접근 · Spring 승인 원장**이 어긋나지 않는지 확인합니다. 아래 §1–§4는 격리 실스택에서 측정한 결과이고, §6은 프로세스 내 대역을 쓰는 자동화 테스트입니다.

## 0. 측정 조건

- **원시 결과:** [`results/phase7-live-stack.json`](results/phase7-live-stack.json) (기록 시각 `2026-11-26T06:20:07+00:00`, 커밋 `5f2af2f`)
- **실제 구성:** MySQL 8.0, Redis 7, Spring Boot, `supervisor`, `log_agent`, `remediation_agent`, Caddy (Docker Compose, 빈 볼륨에서 시작). Docker `29.6.2 / 16 CPU / 16621654016 bytes`, 클라이언트는 `Windows-11-10.0.26200-SP0`의 Python 3.12.10.
- **검증 클라이언트 라이브러리:** `a2a-sdk` 1.3.1, `mcp` 2.3.0, `ag-ui-protocol` 1.0.0, `langgraph` 1.2.20
- **대역:** GitHub Deployments·Grafana Prometheus는 고정 응답 서버, 변경 금지 일정은 빈 로컬 달력입니다. 판정기는 `rule_based`(LLM 호출 없음)이고 롤백 실행은 시뮬레이션입니다.
- **검증 전용 데이터:** 데모 시드만으로는 규칙 기반 판정이 `BLOCKED`로 끝납니다. 그래서 검증 장애마다 배포 인과를 명시한 로그 1건을, 격리 DB에 검증용 런북 1건을 추가했습니다. 이 측정은 **판정 정확도가 아니라 프로토콜·원장 일관성**을 대상으로 합니다.

## 1. AG-UI · 중단과 재개

각 지점을 10회 반복하고 `supervisor` 직접 연결과 Caddy 경유를 번갈아 사용했습니다. 앞 절반은 클라이언트 SSE 연결만 끊고, 뒤 절반은 `docker kill`(`SIGKILL`)로 Supervisor를 강제 종료한 뒤 다시 기동했습니다. 실행이 진행 중인 지점에서는 스트림이 열린 상태에서 종료했습니다.

| 중단 지점 | 횟수 (강제 종료) | 복구 방법과 확인한 원장 상태 | 결과 |
| :--- | :---: | :--- | :---: |
| 근거 조사 중 (첫 A2A 진행 이벤트) | 10 (5) | 중단 직후 계획·승인 0건 → `resume` 후 계획 1·승인 1·실행 0, 화면 `AWAITING_APPROVAL`. 세션 로그의 완료된 조사 호출 1회 | 10/10 |
| 승인 대기 중 | 10 (5) | `restore` 후 같은 승인 객체, 원장 변화 없음, 조사 재호출 0건 | 10/10 |
| 승인 후, 원장 커밋 전에 중단 | 3 (0) | `restore` 시 `AWAITING_EXECUTION_RESULT` → 같은 승인 재시도 3회 → `RESOLVED`, 실행 1·포스트모템 1 | 3/3 |
| 원장 커밋 후, 응답 전에 중단 | 7 (5) | `restore`만으로 `RESOLVED`, 실행 1·포스트모템 1 | 7/7 |
| 명시적 취소 (조사 중 `explicitCancel`) | 5 (0) | 화면 `CANCELLED`, 계획·승인·실행 0건 | 5/5 |

| 로드맵 완료 기준 | 관측 | 측정 범위 |
| :--- | :---: | :--- |
| 완료 단계의 불필요한 재호출 0건 | 0건 | 승인 대기 지점의 조사 호출 수로 확인. 완료 지점에서는 조사 호출 수를 따로 세지 않음 |
| 승인당 실행·포스트모템 중복 0건 | 0건 | 완료 지점 10회의 원장 행 수 |
| 화면–Spring 원장 상태 불일치 0건 | 0건 | 위 30회의 최종 화면 상태와 원장 상태 |

**한계:** 완료 지점에서 강제 종료한 5회는 `docker kill`이 처리되는 동안 원장 커밋이 끝나 모두 커밋 후 상태였습니다. 커밋 전 상태는 연결 단절 3회로만 확인했고, 커밋 전 강제 종료는 실스택에서 확인하지 못했습니다. 강제 종료한 것은 `supervisor`뿐이며 워커·Spring·Redis 장애는 포함하지 않습니다. 중단 시점은 이벤트 경계에 고정돼 있어 DB 트랜잭션 도중의 종료를 겨냥하지 않았습니다. 실행은 한 번에 하나씩 순차로 수행해 동시성·부하는 확인하지 않았습니다.

## 2. A2A · 작업 수명주기와 위임

`log_agent`에 직접 보낸 요청입니다(조사 워커 읽기 전용 토큰 사용).

| 계약 | 관측 | 결과 |
| :--- | :--- | :---: |
| 정상 스트리밍 | 20회 모두 진행 이벤트가 최종 산출물보다 먼저 도착. 첫 진행 이벤트 `p50 / p95 / p99` = 14.17 / 17.17 / 17.2 ms, 최종 산출물 249.3 / 286.75 / 300.57 ms | PASS |
| 타임아웃 | 클라이언트 제한 0.001초에서 `TimeoutError` | PASS |
| 취소 | 진행 중 `CancelTask` 10회: `canceled` 10회 | PASS |
| 중복 요청 | 같은 `idempotencyKey` 재요청이 `deduplicated_hit`로 같은 결과 반환, 동시 2건 모두 성공 | PASS |
| 워커 교체 | 같은 계약의 `agentic` 워커로 직접 호출(4청크 인출)하고, Supervisor의 `LOG_AGENT_A2A_URL`만 바꿔 `AWAITING_APPROVAL` 도달 | PASS |

지연은 한 PC의 loopback에서 클라이언트가 잰 값입니다. 표본이 20개라 `p99`는 사실상 최댓값에 가깝고, 운영 환경 지연으로 해석할 수 없습니다. 중복 요청의 워커 실행 횟수를 MCP 호출 수로 세는 확인은 §6의 자동화 테스트에 있습니다.

## 3. MCP · 도구·리소스·권한

서버와 분리된 Python MCP 클라이언트가 Spring `/sse`에 접속했습니다. 리소스 `incident://evidence-summary/v1`의 장애 항목 필드는 `incidentCode`, `serviceName`, `severity`, `status`, `title`입니다.

| 경로 | 도구 목록 | 리소스·청크 조회 | 잘못된 토큰 | 타 장애 청크 | 조사 워커 쓰기 (MCP / 문자셋 우회 / REST) | 미승인 완료 | 허용된 실행 |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| Spring 직접 | 4종 | 조회 성공 | 401 | 거부 | 403 / 403 / 403 | 거부 | 0건 |
| Caddy 경유 | 4종 | 조회 성공 | 401 | 거부 | 403 / 403 / 403 | 거부 | 0건 |

조사 워커에게도 `tools/list`에는 `completeIncidentWithPostMortem`이 보이며, 호출 시점에 `403`으로 거부됩니다. 내부 토큰은 사용자 인증을 대신하지 않습니다.

## 4. 이번 실증에서 발견해 고친 결함

1. **조사 에이전트가 핵심 로그를 놓침:** 장애 제목에서 뽑은 키워드(`502`, `Bad`, `Gateway` 등)에 로그가 하나라도 걸리면 전체 색인으로 확장하지 않아, 데모 장애에서 `HikariPool` 타임아웃·커넥션 누수 로그를 가져오지 못했고 재계획도 시작되지 않았습니다. 키워드에 걸린 로그가 인출 예산보다 적으면 전체 색인으로 확장하도록 고치고, 키워드 필터를 따르는 대역으로 회귀 테스트를 추가했습니다. 기존 테스트의 대역은 키워드를 무시해 이 결함을 드러내지 못했습니다.
2. **빈 DB에서 MySQL 준비 판정이 앞섬:** `mysqladmin ping -h localhost`는 최초 초기화용 임시 서버에도 성공합니다. 초기화 중 촘촘히 비교한 88회 중 10회는 소켓 확인만 성공하고 TCP는 실패했습니다. 헬스체크를 TCP로 바꾸고 README의 인프라 기동에 `--wait`를 추가했습니다.

**남은 한계:** 데모 시드 장애(`INC-2026-001`)를 `rule_based`로 실행하면 근거를 모두 모아도 `BLOCKED`입니다. 시드 런북 문구와 원시 로그가 규칙의 롤백 허용·배포 인과 조건을 충족하지 않기 때문입니다. 기본 설정인 `decisions_api`로 이 흐름이 승인 대기까지 가는지는 실스택에서 확인하지 않았습니다.

## 5. 스모크와 프런트 통합 테스트

- 빈 볼륨에서 서비스를 순서대로 기동해 백엔드 재시작 0회
- `scripts/smoke-test.ps1` (빈 DB, Caddy 경유, Windows PowerShell 5.1): 25개 확인 항목 모두 통과
- `frontend` `npm test` (`TEST_AG_UI_BASE_URL`을 Caddy로, 완료 픽스처는 §1에서 해결된 장애): 22건 중 22건 통과, 건너뜀 0건. 실서버 통합 4건 중 3건은 Next의 모의 경로(`/bff/mock-ag-ui`), 1건은 실제 Spring SSR/BFF 대상입니다.

## 6. 자동화 계약 테스트 (대역 사용)

`agent` `pytest` 150건 통과(Linux Python 3.12). `test_phase7_protocol_resilience.py`와 `test_supervisor_state_recovery.py`는 인메모리 Redis와 대역 백엔드로 중단 지점·경합·원장 복구를 검사하고, `test_a2a_supervisor_wiring.py`는 loopback HTTP로 A2A 연결을 검사합니다.

## 7. 종단 트레이스

완료된 검증 실행 하나를 `restore`로 다시 읽은 `snapshot.trace`입니다. AG-UI 실행 ID, A2A 작업 참조(조사 워커의 작업 ID와 복구 계획 워커의 요청 키), MCP 근거 청크 ID, Spring 승인 ID가 한 객체에 연결됩니다.

```json
{
  "runId": "lv-p3-26152007-0-plan",
  "traceId": "tr-lv-p3-26152007-0-plan",
  "a2aTaskIds": [
    "1bbe1a3b-dbd3-472b-b662-dd6ebdb4de07",
    "a2a-rem-lv-p3-26152007-0-plan-r0"
  ],
  "evidenceChunkIds": [
    "LOG-LV-152007007",
    "LOG-106-152007007",
    "LOG-102-152007007",
    "LOG-104-152007007",
    "RUNBOOK-LV-01"
  ],
  "approvalId": "APR-INC-LV-26152007-007-E6719B4A"
}
```

## 8. 재현 (PowerShell 5.1)

```powershell
# verify.env.example을 저장소 밖에 복사해 토큰 두 값을 채웁니다.
$c = "-p", "iopsv", "--env-file", "<verify.env>", "-f", "docker-compose-infra.yaml", "-f", "docker-compose.yaml", "-f", "scripts/live-verify/docker-compose.verify.yaml"
docker compose @c up -d --build --wait
.\scripts\smoke-test.ps1 -BaseUrl http://127.0.0.1:18088 -InternalToken <공유 토큰>
python -m pip install -e "./agent[dev,eval]"
python scripts/live-verify/live_verify.py --env-file <verify.env> --output docs/eval/results/phase7-live-stack.json --repeats 10
```
