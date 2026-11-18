# 05. 외부 API Tool 명세와 결제 장애 시나리오

결제 서비스에서 502 오류가 급증하면 **GitHub Deployments**로 롤백 후보를 찾고, **Grafana Cloud Metrics**로 후보 버전의 오류율을 확인하며, **Google Calendar FreeBusy**로 작업 가능한 시간대를 계산한다. 이 세 Tool을 Python 플래너의 LangGraph 실행 단계에서 호출한다. 로그와 런북은 기존 Spring MCP 도구에서 조회한다.

## 1. 공통 계약

- `serviceName`은 등록된 서비스 식별자다. 플래너 설정에서 서비스별 GitHub 저장소와 배포 환경, Grafana 지표 범위, 변경 금지 캘린더를 찾는다.
- 시각은 RFC 3339 형식으로 시간대 오프셋과 함께 전달한다. 시연 화면에서는 한국 시간(KST)으로 표시한다.
- 성공 응답에는 `ok: true`와 `data`를, 실패 응답에는 `ok: false`와 `error.code`, `error.message`를 반환한다.
- 공통 실패 코드는 `INVALID_INPUT`, `AUTH_FAILED`, `RATE_LIMITED`, `UPSTREAM_ERROR`다. 각 Tool은 조회 결과가 없거나 근거가 부족할 때 아래에서 정의한 코드를 추가로 반환한다. 로깅 Agent에는 Tool 이름, 입력의 식별자, 결과 또는 오류 코드, 소요 시간을 남긴다.

## 2. 후보 조회 — GitHub Deployments

**Tool 이름:** `list_rollback_candidates`

**사용 시점:** 장애가 발생한 환경에서 최근에 성공한 배포와 그보다 이전에 성공한 롤백 후보를 찾을 때 호출한다.

| 구분 | 필드 | 의미 |
| --- | --- | --- |
| 입력 | `serviceName: string` | GitHub 저장소를 찾는 서비스 식별자 |
| 입력 | `incidentStartedAt: datetime` | 성공 배포를 찾는 기준 시각. 요청이나 장애 정보에서 확인할 수 없으면 사용자에게 묻는다. |
| 입력 | `environment: string?` | 배포 환경. 생략하면 서비스에 설정한 기본 환경을 사용한다. |
| 출력 | `referenceDeployment: Deployment` | 장애 기준 시각까지 성공 상태가 가장 최근에 기록된 배포. 지표 비교 기준으로 사용한다. |
| 출력 | `candidates: Deployment[]` | 기준 배포보다 앞선 성공 이력 중 SHA가 다른 후보. 같은 SHA는 가장 최근 성공 기록만 남긴다. |

`Deployment`에는 `deploymentId: integer`, `gitSha: string`, `ref: string`, `succeededAt: datetime`이 들어간다. 배포 요청 생성일과 성공 기록 시각은 구분한다. GitHub가 제공하는 상태 이력에서 `state = success`인 항목의 시각을 사용한다. 후보가 없으면 `NO_RESULT`를 반환한다.

호출 경로는 `GET /repos/{owner}/{repo}/deployments?environment={environment}`와 `GET /repos/{owner}/{repo}/deployments/{deployment_id}/statuses`다. 비공개 저장소는 `Deployments: read` 권한을 사용한다. [배포 목록](https://docs.github.com/en/rest/deployments/deployments#list-deployments) · [배포 상태](https://docs.github.com/en/rest/deployments/statuses#list-deployment-statuses) · [호출 한도](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api)

## 3. 상세 조회 — Grafana Cloud Metrics

**Tool 이름:** `get_release_metrics`

**사용 시점:** 기준 배포와 롤백 후보의 502 비율을 같은 관측 구간에서 비교할 때 호출한다.

| 구분 | 필드 | 의미 |
| --- | --- | --- |
| 입력 | `serviceName: string` | 조회할 서비스 |
| 입력 | `gitSha: string` | 후보 조회에서 받은 커밋 SHA |
| 입력 | `from: datetime`, `to: datetime` | 관측 구간의 시작과 끝 |
| 출력 | `gitSha: string`, `from: datetime`, `to: datetime` | 지표를 조회한 버전과 시간 범위 |
| 출력 | `totalRequests: number`, `http502Requests: number` | 관측 구간의 전체 요청 수와 502 응답 수 추정치 |
| 출력 | `http502RatePercent: number` | `http502Requests / totalRequests × 100` |

Grafana Cloud의 Prometheus 조회 엔드포인트에 `GET /api/v1/query`를 사용한다. `to`를 평가 시각으로 지정하고 `to - from` 길이의 카운터 증가량을 기준으로 두 건수를 계산한다. 지표에는 GitHub와 같은 SHA를 `git_sha` 라벨로 기록한다. 502 카운터는 오류가 없는 버전도 0부터 수집한다. 필요한 시계열이 빠져 있거나 요청 수가 0이면 `INSUFFICIENT_DATA`를 반환해 오류율 0%와 구분한다.

Grafana Cloud의 지표 인스턴스 ID와 `metrics:read` 권한의 토큰을 사용한다. [Grafana 조회 API](https://grafana.com/docs/grafana-cloud/send-data/metrics/metrics-prometheus/query-http-api/) · [카운터 증가량 계산](https://prometheus.io/docs/prometheus/latest/querying/functions/#increase) · [사용 한도](https://grafana.com/docs/grafana-cloud/platform/pricing-and-usage/usage-limits/)

## 4. 보조 정보 조회 — Google Calendar FreeBusy

**Tool 이름:** `get_change_freeze_windows`

**사용 시점:** 사용자가 허용한 시간 범위에서 복구 작업과 겹치는 변경 금지 시간이 있는지 확인할 때 호출한다.

| 구분 | 필드 | 의미 |
| --- | --- | --- |
| 입력 | `serviceName: string` | 변경 금지 전용 캘린더를 찾는 서비스 식별자 |
| 입력 | `windowStart: datetime`, `windowEnd: datetime` | 사용자가 허용한 작업 시간 범위 |
| 출력 | `freezeWindows: { start: datetime, end: datetime }[]` | 전용 캘린더에서 바쁨으로 표시된 변경 금지 구간. 시작 시각은 포함하고 종료 시각은 제외한다. |

`POST /calendar/v3/freeBusy`에 `timeMin`, `timeMax`, `items[].id`를 전달하고, 대상 캘린더의 `busy[]`를 읽는다. 조회가 성공했고 금지 일정이 없으면 빈 배열을 반환한다. 캘린더별 `errors[]`가 있으면 공통 실패 코드로 반환해 조회 성공과 구분한다. FreeBusy는 일정의 바쁨 여부만 알려주므로, 이 Tool에는 팀이 변경 금지 구간만 등록한 전용 캘린더를 연결한다.

해당 캘린더에 접근할 수 있는 계정의 FreeBusy 조회 권한을 사용한다. [FreeBusy API](https://developers.google.com/workspace/calendar/api/v3/reference/freebusy/query) · [OAuth 권한](https://developers.google.com/workspace/calendar/api/auth) · [호출 한도](https://developers.google.com/workspace/calendar/api/guides/quota)

## 5. 결제 장애 시나리오와 재계획

아래 값은 시연용 예시다. 장애 시각은 14:04이고, 작업 시간과 관측 시각은 같은 날 KST 기준이다.

> 결제 서비스에서 502가 급증했어. 이전 버전의 502 비율이 1% 미만이면 롤백을 제안해줘. 14:28~14:50 사이에서 변경 금지 시간과 겹치지 않는 5분짜리 작업 창을 찾아줘.

| 단계 | Tool 호출과 결과 | 결과가 다음 단계에 쓰이는 방식 |
| --- | --- | --- |
| 1. 후보 찾기 | GitHub에서 14:00에 성공 상태가 기록된 기준 배포 `v42`와 이전 성공 배포 `v41`을 찾는다. | 두 배포의 `gitSha`를 상세 지표 조회에 넘긴다. |
| 2. 후보 검증 | Grafana에서 두 버전 모두 트래픽을 받은 14:05~14:10을 조회한다. `v42`는 1,000건 중 180건이 502(18%), `v41`은 1,000건 중 3건이 502(0.3%)다. | 코드에서 `0.3% < 1%`를 확인해 `v41`을 후보로 선택한다. |
| 3. 시간 확인 | Calendar에서 허용 범위 14:28~14:50을 조회한다. 변경 금지 구간은 14:30~14:40이다. | 초안의 14:28~14:33이 금지 구간과 겹친다고 판정한다. |
| 4. 재계획 | 수집한 결과를 재사용해 남은 시간대를 계산한다. 14:28~14:30은 2분, 14:40~14:50은 10분이다. | 처음으로 5분을 확보할 수 있는 **14:40~14:45**를 새 작업 창으로 잡고, 변경 이유를 실행 이벤트에 남긴다. |

플래너는 **`v41` 롤백 제안, 14:40~14:45 작업 창, 후보 버전 502 비율 0.3%**를 근거와 함께 운영자에게 제시한다. 시간 계산과 오류율 기준 비교는 코드에서 수행하고, [판정 API](04-domain-planner-and-decisions.md#3-mvp의-판정-기준과-분기)는 로그·런북이 롤백 제안을 뒷받침하는지 검토한다. 운영자가 AG-UI에서 최종 승인한다.

조건을 만족하는 시간대가 없으면 남아 있는 시간대를 알려주고 허용 범위를 조정할지 묻는다. 지표나 일정 조회가 실패한 경우에는 확인할 수 없었던 조건과 실패 이유를 표시한다.

## 6. 첫 연동에서 확인할 것

- 세 API를 각각 실제 호출하고, 성공·실패 응답이 Tool 계약대로 변환되는지 확인한다.
- 테스트 배포의 SHA가 Grafana 지표의 `git_sha`와 일치하는지 확인한다. 동일 SHA의 지표를 찾을 수 없으면 후보를 검증할 수 없다.
- 변경 금지 일정이 있는 경우와 없는 경우를 비교한다. 시간 충돌로 재계획할 때 앞서 받은 API 결과를 다시 사용하고, Tool 호출 이력에 중복 조회가 없는지 확인한다.
