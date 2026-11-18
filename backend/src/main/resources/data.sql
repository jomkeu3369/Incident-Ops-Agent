INSERT INTO incidents (incident_code, title, service_name, severity, status, summary, created_at, updated_at)
VALUES (
    'INC-2026-001',
    '결제 API 502 Bad Gateway 급증 및 응답 지연',
    'payment-service',
    'SEV1',
    'OPEN',
    '14:00 배포 직후 /api/v1/payments 엔드포인트에서 502 에러율 42% 급증 및 p99 지연 30초 초과',
    '2026-11-18 14:02:00',
    '2026-11-18 14:02:00'
);

INSERT INTO remediation_approvals (approval_id, incident_code, action_command, proposed_by, approved_by, decision, created_at, decided_at)
VALUES (
    'APR-2026-001',
    'INC-2026-001',
    'kubectl rollout undo deployment/payment-service',
    'supervisor-agent',
    'oncall-lead',
    'APPROVED',
    '2026-11-18 14:05:00',
    '2026-11-18 14:06:30'
);

INSERT INTO corpus_chunks (chunk_id, corpus_type, service_name, title, keywords, content, sequence_order) VALUES
('LOG-101', 'LOG', 'payment-service', '13:50-13:55 정상 트래픽 및 헬스체크 로그', 'healthcheck,200,normal',
 '2026-11-18 13:50:12 INFO [http-nio-8080-exec-1] GET /actuator/health 200 OK (4ms)\n2026-11-18 13:52:40 INFO [http-nio-8080-exec-3] POST /api/v1/payments txId=TX9001 200 OK (48ms)', 1),

('LOG-102', 'LOG', 'payment-service', '13:56-13:59 배포 롤아웃 시작 이벤트', 'deploy,rollout,v2.4.1',
 '2026-11-18 13:56:05 INFO [k8s-agent] Rolling update started for deployment/payment-service image=payment-service:v2.4.1\n2026-11-18 13:58:19 INFO [main] Started PaymentApplication in 6.412 seconds', 2),

('LOG-103', 'LOG', 'payment-service', '14:00-14:01 외부 PG사 웹훅 수신 로그', 'webhook,pg,callback',
 '2026-11-18 14:00:11 INFO [http-nio-8080-exec-7] POST /api/v1/webhooks/pg-confirm 200 OK (31ms)\n2026-11-18 14:00:49 WARN [http-nio-8080-exec-9] Slow external rate query took 820ms', 3),

('LOG-104', 'LOG', 'payment-service', '14:01-14:02 HikariPool 커넥션 타임아웃 급증', 'HikariPool,Connection,timeout,30000ms,SQLTransientConnectionException',
 '2026-11-18 14:01:33 ERROR [http-nio-8080-exec-12] c.z.h.p.HikariPool : HikariPool-1 - Connection is not available, request timed out after 30000ms (total=20, active=20, idle=0, waiting=47)\n2026-11-18 14:01:34 ERROR [http-nio-8080-exec-14] java.sql.SQLTransientConnectionException: HikariPool-1 - Connection is not available, request timed out after 30000ms', 4),

('LOG-105', 'LOG', 'payment-service', '14:02 커넥션 누수 감지 스택트레이스 (v2.4.1)', 'leak,Connection,PaymentLedgerService,v2.4.1',
 '2026-11-18 14:02:05 WARN [HikariPool-1 housekeeper] c.z.h.p.ProxyLeakTask : Connection leak detection triggered for conn0 on thread http-nio-8080-exec-4, stack trace follows\njava.lang.Exception: Apparent connection leak detected\n\tat com.incidentops.payment.service.PaymentLedgerService.recordAsyncAudit(PaymentLedgerService.java:118)', 5),

('LOG-106', 'LOG', 'api-gateway', '14:02-14:03 게이트웨이 502 Upstream 응답 기록', '502,Bad Gateway,upstream',
 '2026-11-18 14:02:18 ERROR [caddy] upstream http://payment-service:8080 returned 502 Bad Gateway after 30005ms\n2026-11-18 14:02:45 ERROR [caddy] upstream http://payment-service:8080 returned 502 Bad Gateway after 30002ms', 6),

('RUNBOOK-01', 'RUNBOOK', 'payment-service', 'DB 커넥션 풀 고갈 및 누수 긴급 복구 런북', 'HikariPool,leak,rollback,kubectl',
 '1. 증상: HikariPool active=total, idle=0 상태가 지속되며 SQLTransientConnectionException 발생\n2. 원인 판별: ProxyLeakTask 로그가 최근 배포 직후 발생했다면 신규 버전의 트랜잭션/커넥션 미반환 버그임\n3. 표준 조치: 즉시 이전 안정 버전으로 롤백 실행 -> kubectl rollout undo deployment/payment-service\n4. 검증: 롤백 후 2분 내 HikariPool active 커넥션이 5 이하로 감소하고 502 에러율이 0% 로 복귀하는지 확인', 1),

('RUNBOOK-02', 'RUNBOOK', 'payment-service', '단순 트래픽 폭증 시 HikariPool 증설 가이드', 'traffic,spike,maximum-pool-size',
 '1. 조건: 커넥션 누수(ProxyLeakTask)가 없고 순수 TPS 증가로 인한 대기열 발생 시에만 적용\n2. 조치: DB max_connections 여유분 확인 후 maximum-pool-size 를 20에서 40으로 상향 조정', 2);