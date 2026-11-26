# Phase 7 · 3대 프로토콜(AG-UI · A2A · MCP) 실패·복구 계약 검증 리포트

> **목적:** 연결 단절, Supervisor 강제 종료, 타임아웃, 취소, 중복 요청, 권한 경계 침범 상황에서 **화면 상태 · A2A 작업 · MCP 접근 · Spring 승인 원장**이 어긋나지 않는지 확인합니다. §1–§5는 격리 실스택에서 실행한 결과이고 원시 파일을 함께 둡니다. §6은 프로세스 내 대역을 쓰는 자동화 테스트입니다.

## 0. 측정 조건

- **원시 결과:** [`results/phase7-live-stack.json`](results/phase7-live-stack.json) (기록 시각 `2026-11-26T07:00:58+00:00`, 커밋 `8bb874f`)
- **실제 구성:** MySQL 8.0, Redis 7, Spring Boot, `supervisor`, `log_agent`, `remediation_agent`, Caddy (Docker Compose, 빈 볼륨에서 시작). Docker `29.6.2 / 16 CPU / 16621654016 bytes`, 클라이언트는 `Windows-11-10.0.26200-SP0`의 Python 3.12.10.
- **검증 클라이언트 라이브러리:** `a2a-sdk` 1.3.1, `mcp` 2.3.0, `ag-ui-protocol` 1.0.0, `langgraph` 1.2.20
- **대역:** GitHub Deployments·Grafana Prometheus는 고정 응답 서버, 변경 금지 일정은 빈 로컬 달력입니다. 판정기는 `rule_based`(LLM 호출 없음)이고 롤백 실행은 시뮬레이션입니다.
- **접속 재시도와 폐기한 실행:** 측정한 PC는 loopback 접속이 간헐적으로 `WinError 10014`로 실패합니다. 검증 클라이언트는 접속 수립 실패만 재시도하며, 서버에 요청이 도달한 뒤의 오류는 재시도하지 않습니다(원시 결과의 `clientConnectRetries`). 재시도를 넣기 전의 실행 1회는 접속 오류로 2개 항목(MCP Caddy 경로, 조사 중 중단 1회)이 실패해 폐기하고 처음부터 다시 측정했습니다.
- **검증 전용 데이터:** 데모 시드만으로는 규칙 기반 판정이 `BLOCKED`로 끝납니다. 그래서 검증 장애마다 배포 인과를 명시한 로그 1건을, 격리 DB에 검증용 런북 1건을 추가했습니다. 이 측정은 **판정 정확도가 아니라 프로토콜·원장 일관성**을 대상으로 합니다.

## 1. AG-UI · 중단과 재개

각 지점을 10회 반복하고 `supervisor` 직접 연결과 Caddy 경유를 번갈아 사용했습니다. 모든 회차는 SSE 스트림을 읽는 도중에 중단합니다. 앞 절반은 클라이언트 연결만 끊고, 뒤 절반은 연결이 열린 상태에서 `docker kill`(`SIGKILL`)로 Supervisor를 강제 종료한 뒤 다시 기동했습니다.

| 중단 지점 | 횟수 (강제 종료) | 복구 방법과 확인한 상태 | 결과 |
| :--- | :---: | :--- | :---: |
| 근거 조사 중 (첫 A2A 진행 이벤트) | 10 (5) | 중단 뒤 계획·승인 0건 → `resume` 후 계획 1·승인 1·실행 0, 화면 `AWAITING_APPROVAL`, 완료된 조사 호출 1회 | 10/10 |
| 승인 대기 (승인 대기 상태를 받은 직후, 실행 종료 이벤트 전) | 10 (5) | `restore` 후 같은 승인 객체, 원장 변화 없음, 조사 재호출 없음 | 10/10 |
| 승인 후, 원장 커밋 전 | 6 (3) | 중단 시점 실행·포스트모템 0행 → `restore` 시 `AWAITING_EXECUTION_RESULT` → 같은 승인 재시도 → `RESOLVED`, 각 1행 | 6/6 |
| 원장 커밋 후, 응답을 읽기 전 | 4 (2) | 중단 시점 실행·포스트모템 1행 → 재시도 없이 `restore`만으로 `RESOLVED` | 4/4 |
| 명시적 취소 (조사 중 `explicitCancel`) | 5 (0) | 화면 `CANCELLED`, 계획·승인·실행 0건 | 5/5 |

| 로드맵 완료 기준 | 관측 | 측정 범위 |
| :--- | :---: | :--- |
| 완료 단계의 불필요한 재호출 0건 | 0건 | 승인 대기·완료 지점 20회에서 복구 전후의 완료된 **조사 호출** 수 비교. 복구 계획 워커 호출 수는 세지 않음 |
| 승인당 실행·포스트모템 중복 0건 | 0건 | 완료 지점 10회 모두 해결 뒤 같은 승인을 한 번 더 전송(HTTP 409 10회)하고 DB 행 수 확인. DB의 유일 제약도 중복 행을 막음 |
| 화면–Spring 원장 상태 불일치 0건 | 0건 | 위 30회의 최종 화면 상태와 원장 상태 |

- **커밋 전 상태를 만든 방법:** 원장 완료 쓰기는 복구 워커의 응답을 받은 Supervisor가 합니다. `remediation_agent`를 일시 정지(`docker pause`)한 상태에서 중단하고, 중단 뒤에 정지를 풀었습니다.
- **재기동:** 강제 종료 15회 중 8회는 `docker start` 뒤 응답이 없어 컨테이너를 다시 만들었습니다(이 PC의 Docker 포트 게시 실패 대응, 원시 결과의 `supervisorRecreated`). 상태는 Redis와 원장에서 복원합니다.
- **한계:** 강제 종료한 것은 `supervisor`뿐이며 워커·Spring·Redis 장애는 포함하지 않습니다. 중단 시점은 이벤트 경계에 고정돼 있어 완료 쓰기 트랜잭션 도중의 종료를 겨냥하지 않았습니다. 실행은 한 번에 하나씩 순차로 수행해 동시성·부하는 확인하지 않았습니다.

## 2. A2A · 작업 수명주기와 위임

워커 교체의 마지막 항목을 빼면 `log_agent`에 직접 보낸 요청입니다(조사 워커 읽기 전용 토큰 사용).

| 계약 | 관측 | 결과 |
| :--- | :--- | :---: |
| 정상 스트리밍 | 20회 모두 진행 이벤트가 최종 산출물보다 먼저 도착. 첫 진행 이벤트 `p50 / p95 / p99` = 13.13 / 16.16 / 17.69 ms, 최종 산출물 262.58 / 317.65 / 320.81 ms | PASS |
| 타임아웃 | 클라이언트 제한 0.001초에서 `TimeoutError`. 클라이언트의 시간 초과 처리만 확인하며 워커 쪽 동작은 확인하지 않음 | PASS |
| 취소 | 진행 중 `CancelTask` 10회: `canceled` 10회 | PASS |
| 중복 요청 | 같은 `idempotencyKey` 재요청이 `deduplicated_hit`로 같은 결과 반환. 동시 2건은 모두 성공했으나 실행이 1회였는지는 실스택에서 확인하지 않음(§6) | PASS |
| 워커 교체 | 같은 계약의 `agentic` 워커 직접 호출(4청크 인출). 원래 워커를 일시 정지한 상태에서 Supervisor의 `LOG_AGENT_A2A_URL`만 바꿔 `AWAITING_APPROVAL` 도달 | PASS |

지연은 한 PC의 loopback에서 클라이언트가 잰 값입니다. 표본이 20개라 `p99`는 사실상 최댓값에 가깝고, 운영 환경 지연으로 해석할 수 없습니다.

## 3. MCP · 도구·리소스·권한

서버와 분리된 Python MCP 클라이언트가 Spring `/sse`에 접속했습니다. 리소스 `incident://evidence-summary/v1`의 장애 항목 필드는 `incidentCode`, `serviceName`, `severity`, `status`, `title`입니다. 거부 판정은 자기 장애 청크 인출이 성공하는 대조군과 서버의 거부 사유를 함께 확인합니다.

| 경로 | 도구 목록 | 리소스·색인·자기 청크 조회 | 잘못된 토큰 | 타 장애 청크 | 조사 워커 쓰기 (MCP / 문자셋 우회 / REST) | 미승인 완료 | 허용된 실행 |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| Spring 직접 | 4종 | 성공 | 401 | 거부 | 403 / 403 / 403 | 거부 | 0건 |
| Caddy 경유 | 4종 | 성공 | 401 | 거부 | 403 / 403 / 403 | 거부 | 0건 |

- 타 장애 청크 거부 사유: `해당 장애에서 조회할 수 있는 청크를 찾지 못했습니다.`
- 미승인 완료 거부 사유: `승인(APPROVED)되지 않은 요청입니다. 현재 상태: PENDING`
- 조사 워커에게도 `tools/list`에는 `completeIncidentWithPostMortem`이 보이며, 호출 시점에 `403`으로 거부됩니다. 내부 토큰은 사용자 인증을 대신하지 않습니다.

## 4. 이번 실증에서 발견해 고친 결함

1. **조사 에이전트가 핵심 로그를 놓침:** 장애 제목에서 뽑은 키워드(`502`, `Bad`, `Gateway` 등)에 로그가 하나라도 걸리면 전체 색인으로 확장하지 않아, 데모 장애에서 `HikariPool` 타임아웃·커넥션 누수 로그를 가져오지 못했고 재계획도 시작되지 않았습니다. 키워드에 걸린 로그가 인출 예산 이하면 전체 색인으로 확장하도록 고치고, 키워드 필터를 따르는 대역으로 회귀 테스트를 추가했습니다. 기존 테스트의 대역은 키워드를 무시해 이 결함을 드러내지 못했습니다.
   - 60건 평가의 `keyword_dr_dci`는 사례 단위로 회수율이 낮아지거나 정답이 오답으로 바뀐 경우가 없고 7건이 정답으로 바뀌었습니다.
   - **부작용:** 인출 예산이 1이고 런북이 있으면 로그를 뽑지 않는데도 색인 조회가 1회 늘 수 있습니다. 데모 장애의 첫 조사에서는 로그가 슬롯을 채워, 이전에 인출하던 반대 조건 런북 `RUNBOOK-02`가 빠집니다. 점수가 높은 비적중 로그가 키워드 적중 로그를 밀어낼 가능성도 코드상 남아 있습니다.
2. **빈 DB에서 MySQL 준비 판정이 앞섬:** `mysqladmin ping -h localhost`는 최초 초기화용 임시 서버에도 성공합니다. 초기화 중 비교한 95회 중 10회는 소켓 확인만 성공하고 TCP는 실패했습니다([원시 기록](results/phase7-mysql-healthcheck-probe.txt)). 헬스체크를 TCP로 바꾸고 README의 인프라 기동에 `--wait`를 추가했습니다.

**남은 한계:** 데모 시드 장애(`INC-2026-001`)를 `rule_based`로 실행하면 근거를 모두 모아도 `BLOCKED`입니다. 시드 런북 문구와 원시 로그가 규칙의 롤백 허용·배포 인과 조건을 충족하지 않기 때문입니다(수정 과정의 수동 확인, 원시 파일 없음). 기본 설정인 `decisions_api`로 이 흐름이 승인 대기까지 가는지는 실스택에서 확인하지 않았습니다.

## 5. 스모크와 프런트 통합 테스트

- `scripts/smoke-test.ps1` (빈 볼륨으로 시작한 DB, Caddy 경유, Windows PowerShell 5.1): 25개 확인 항목 모두 통과 ([출력](results/phase7-smoke.log))
- `frontend` `npm test` (`TEST_AG_UI_BASE_URL`을 Caddy로, 완료 픽스처는 §1에서 해결된 장애): 22건 중 22건 통과, 건너뜀 0건 ([요약](results/phase7-frontend-test.log)). 실서버 통합 4건 중 3건은 Next의 모의 경로(`/bff/mock-ag-ui`), 1건은 실제 Spring SSR/BFF 대상입니다.

## 6. 자동화 계약 테스트 (대역 사용)

`agent` `pytest` 150건 통과(Linux Python 3.12). `test_phase7_protocol_resilience.py`와 `test_supervisor_state_recovery.py`는 인메모리 Redis와 대역 백엔드로 중단 지점·경합·원장 복구를 검사하고, 중복 요청의 실행 횟수를 MCP 호출 수로 확인합니다. `test_a2a_supervisor_wiring.py`는 loopback HTTP로 A2A 연결을 검사합니다.

## 7. 종단 트레이스

완료된 검증 실행 하나를 `restore`로 다시 읽은 `snapshot.trace`입니다([원시 파일](results/phase7-trace.json)). AG-UI 실행 ID, A2A 작업 참조(조사 워커의 작업 ID와 복구 계획 워커의 요청 키), MCP 근거 청크 ID, Spring 승인 ID가 한 객체에 연결됩니다.

```json
{
  "runId": "lv-p3-26160058-0-plan",
  "traceId": "tr-lv-p3-26160058-0-plan",
  "a2aTaskIds": [
    "51854f79-577d-4326-bf81-dc7641bebcd8",
    "a2a-rem-lv-p3-26160058-0-plan-r0"
  ],
  "evidenceChunkIds": [
    "LOG-LV-160058007",
    "LOG-106-160058007",
    "LOG-102-160058007",
    "LOG-104-160058007",
    "RUNBOOK-LV-01"
  ],
  "approvalId": "APR-INC-LV-26160058-007-C55342E8"
}
```

## 8. 재현 (PowerShell 5.1)

Docker Compose v5.3.1에서 실행했습니다. override의 `!override`·`!reset` 태그에는 Compose 2.24.4 이상이 필요합니다.

```powershell
# verify.env.example을 저장소 밖에 복사해 토큰 두 값을 서로 다른 문자열로 채웁니다.
$c = "-p", "iopsv", "--env-file", "<verify.env>", "-f", "docker-compose-infra.yaml", "-f", "docker-compose.yaml", "-f", "scripts/live-verify/docker-compose.verify.yaml"
docker compose @c up -d --build --wait
.\scripts\smoke-test.ps1 -BaseUrl http://127.0.0.1:18088 -InternalToken <공유 토큰>
python -m pip install -e "./agent[dev,eval]"
python scripts/live-verify/live_verify.py --env-file <verify.env> --output docs/eval/results/phase7-live-stack.json --repeats 10

# 프런트 실서버 통합 테스트 (<해결된 장애>는 결과 JSON의 during_completion 장애 코드)
cd frontend
npm ci
$env:TEST_AG_UI_BASE_URL = "http://127.0.0.1:18088"; $env:TEST_SECRET_TOKEN = "<공유 토큰>"; $env:TEST_INCIDENT_CODE = "<해결된 장애>"
npm test
```
