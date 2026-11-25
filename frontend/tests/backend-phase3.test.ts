import assert from "node:assert/strict";
import test from "node:test";
import { normalizeKstTimestamp, timeLabel } from "../app/ag-ui-contract";
import {
  fetchIncidentDetailFromBackend,
  fetchIncidentsFromBackend,
  hydrateSnapshotFromDetail,
  toIncidentListItem,
  type BackendIncidentDetail,
} from "../app/backend-api";
import { demoPlan } from "../app/demo";
import { createInitialState, workspaceReducer } from "../app/workspace-state";

const sampleDetail: BackendIncidentDetail = {
  incident: {
    id: 1,
    incidentCode: "INC-2026-001",
    title: "결제 서비스 DB 커넥션 풀 고갈",
    serviceName: "payment-service",
    severity: "SEV1",
    status: "WAITING_APPROVAL",
    summary: "배포 직후 HikariPool 대기 시간 초과가 증가했습니다.",
    createdAt: "2026-11-18T14:06:00",
    updatedAt: "2026-11-18T14:07:00",
  },
  plans: [
    {
      id: 1,
      planId: "PLAN-2026-001",
      incidentCode: "INC-2026-001",
      steps: [
        {
          stepNo: 1,
          description: "로그와 런북 근거를 확인한다.",
          condition: "커넥션 누수 징후가 확인됨",
          actionCommand: null,
          evidenceChunkIds: ["LOG-104"],
        },
        {
          stepNo: 2,
          description: "결제 서비스를 직전 안정 버전으로 롤백한다.",
          condition: "운영자가 승인함",
          actionCommand: "kubectl rollout undo deployment/payment-service",
          evidenceChunkIds: ["LOG-104", "RUNBOOK-01"],
        },
      ],
      verificationCriteria: "오류율과 DB 커넥션 대기 시간이 정상 범위로 돌아온다.",
      createdBy: "supervisor-agent",
      createdAt: "2026-11-18T14:07:00",
    },
  ],
  approvals: [
    {
      id: 1,
      approvalId: "APR-2026-001",
      incidentCode: "INC-2026-001",
      planId: "PLAN-2026-001",
      stepNo: 2,
      actionCommand: "kubectl rollout undo deployment/payment-service",
      proposedBy: "supervisor-agent",
      approvedBy: null,
      decision: "PENDING",
      createdAt: "2026-11-18T14:07:00",
      decidedAt: null,
    },
  ],
  executions: [],
  postMortem: null,
};

test("hydrateSnapshotFromDetail maps PENDING, APPROVED(resumable), FAILED, and RESOLVED states accurately", () => {
  const pendingSnap = hydrateSnapshotFromDetail(sampleDetail);
  assert.equal(pendingSnap.workflowStatus, "AWAITING_APPROVAL");
  assert.equal(pendingSnap.approval?.approvalId, "APR-2026-001");
  assert.equal(pendingSnap.approval?.stepNo, 2);
  assert.equal(pendingSnap.plan?.planId, "PLAN-2026-001");

  const approvedNoExec: BackendIncidentDetail = {
    ...sampleDetail,
    approvals: [{ ...sampleDetail.approvals[0], decision: "APPROVED", approvedBy: "oncall-lead" }],
  };
  assert.equal(hydrateSnapshotFromDetail(approvedNoExec).workflowStatus, "AWAITING_EXECUTION_RESULT");

  const failedExec: BackendIncidentDetail = {
    ...approvedNoExec,
    executions: [
      {
        approvalId: "APR-2026-001",
        actionCommand: "kubectl rollout undo deployment/payment-service",
        executedBy: "remediation-agent",
        executionStatus: "FAILED",
        executionOutput: "timeout",
        verificationSummary: null,
        executedAt: "2026-11-18T14:09:00",
      },
    ],
  };
  assert.equal(hydrateSnapshotFromDetail(failedExec).workflowStatus, "EXECUTION_FAILED");

  const resolvedDetail: BackendIncidentDetail = {
    ...approvedNoExec,
    incident: { ...sampleDetail.incident, status: "RESOLVED" },
    executions: [
      {
        approvalId: "APR-2026-001",
        actionCommand: "kubectl rollout undo deployment/payment-service",
        executedBy: "remediation-agent",
        executionStatus: "SUCCESS",
        executionOutput: "rolled back",
        verificationSummary: "결제 오류율 0.2%로 정상화됨",
        executedAt: "2026-11-18T14:10:00",
      },
    ],
    postMortem: {
      incidentCode: "INC-2026-001",
      rootCause: "신규 배포 버전의 커넥션 누수",
      timelineMarkdown: "- 14:04 502 급증\n- 14:10 롤백 완료",
      remediationSummary: "v41 롤백 완료",
      preventionItems: "커넥션 풀 누수 알림 임계치 추가",
      createdAt: "2026-11-18T14:10:05",
    },
  };
  assert.equal(hydrateSnapshotFromDetail(resolvedDetail).workflowStatus, "RESOLVED");
});

test("BACKEND_DETAIL_SYNCED preserves AG-UI workWindow while attaching executions and postMortem", () => {
  const item = toIncidentListItem(sampleDetail.incident);
  let state = createInitialState(item, sampleDetail);

  state = workspaceReducer(state, {
    type: "STATE_SNAPSHOT",
    snapshot: {
      incidentCode: "INC-2026-001",
      workflowStatus: "AWAITING_APPROVAL",
      plan: { ...demoPlan, planId: "PLAN-2026-001" },
      approval: { approvalId: "APR-2026-001", planId: "PLAN-2026-001", stepNo: 2, decision: "PENDING" },
      replanHistory: [],
    },
  });

  const resolvedDetail: BackendIncidentDetail = {
    ...sampleDetail,
    incident: { ...sampleDetail.incident, status: "RESOLVED" },
    approvals: [{ ...sampleDetail.approvals[0], decision: "APPROVED", approvedBy: "oncall-lead" }],
    executions: [
      {
        approvalId: "APR-2026-001",
        actionCommand: "kubectl rollout undo deployment/payment-service",
        executedBy: "remediation-agent",
        executionStatus: "SUCCESS",
        executionOutput: "rolled back",
        verificationSummary: "정상화 확인",
        executedAt: "2026-11-18T14:10:00",
      },
    ],
    postMortem: {
      incidentCode: "INC-2026-001",
      rootCause: "커넥션 누수",
      timelineMarkdown: "- 14:10 완료",
      remediationSummary: "롤백 완료",
      preventionItems: "모니터링 강화",
      createdAt: "2026-11-18T14:10:05",
    },
  };

  state = workspaceReducer(state, { type: "BACKEND_DETAIL_SYNCED", detail: resolvedDetail });
  assert.equal(state.snapshot.workflowStatus, "RESOLVED");
  assert.equal(state.snapshot.plan?.workWindow?.start, demoPlan.workWindow?.start);
  assert.equal(state.executions.length, 1);
  assert.equal(state.postMortem?.rootCause, "커넥션 누수");
});

test("server-only backend fetch sends X-Internal-Token and maps 401/404 cleanly", async () => {
  const prevUrl = process.env.BACKEND_INTERNAL_URL;
  const prevToken = process.env.INTERNAL_SERVICE_TOKEN;
  process.env.BACKEND_INTERNAL_URL = "http://backend.internal:8080";
  process.env.INTERNAL_SERVICE_TOKEN = "super-secret-internal-token";

  try {
    let seenToken = "";
    const okFetch: typeof fetch = async (_input, init) => {
      const headers = new Headers(init?.headers);
      seenToken = headers.get("X-Internal-Token") ?? "";
      return new Response(JSON.stringify([sampleDetail.incident]), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    };
    const listRes = await fetchIncidentsFromBackend(okFetch);
    assert.equal(listRes.ok, true);
    assert.equal(seenToken, "super-secret-internal-token");

    const unauthFetch: typeof fetch = async () => new Response("Unauthorized", { status: 401 });
    const unauthRes = await fetchIncidentsFromBackend(unauthFetch);
    assert.equal(unauthRes.ok, false);
    if (!unauthRes.ok) {
      assert.equal(unauthRes.status, 401);
      assert.doesNotMatch(unauthRes.message, /super-secret-internal-token/);
    }

    const notFoundFetch: typeof fetch = async () => new Response("Not Found", { status: 404 });
    const notFoundRes = await fetchIncidentDetailFromBackend("INC-9999", notFoundFetch);
    assert.equal(notFoundRes.ok, false);
    if (!notFoundRes.ok) {
      assert.equal(notFoundRes.status, 404);
      assert.match(notFoundRes.message, /찾을 수 없습니다/);
    }
  } finally {
    if (prevUrl === undefined) delete process.env.BACKEND_INTERNAL_URL;
    else process.env.BACKEND_INTERNAL_URL = prevUrl;
    if (prevToken === undefined) delete process.env.INTERNAL_SERVICE_TOKEN;
    else process.env.INTERNAL_SERVICE_TOKEN = prevToken;
  }
});

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

test("SSR and BFF proxy serve Spring incident detail without leaking INTERNAL_SERVICE_TOKEN", async (t) => {
  const baseUrl = process.env.TEST_AG_UI_BASE_URL;
  const secretToken = process.env.TEST_SECRET_TOKEN;
  if (!baseUrl || !secretToken) {
    t.skip("Set TEST_AG_UI_BASE_URL and TEST_SECRET_TOKEN to run SSR/BFF integration test");
    return;
  }

  const incidentCode = process.env.TEST_INCIDENT_CODE || "INC-2026-001";
  const expectedTitle = process.env.TEST_EXPECTED_TITLE || "결제 API 502 Bad Gateway 급증 및 응답 지연";
  const expectedRootCause = process.env.TEST_EXPECTED_POSTMORTEM_ROOT_CAUSE;
  const secretPattern = new RegExp(escapeRegExp(secretToken));

  const pageRes = await fetch(`${baseUrl}/?incident=${encodeURIComponent(incidentCode)}&mode=live`);
  assert.equal(pageRes.status, 200);
  const html = await pageRes.text();
  assert.match(html, new RegExp(escapeRegExp(incidentCode)));
  assert.match(html, new RegExp(escapeRegExp(expectedTitle)));
  assert.doesNotMatch(html, secretPattern);

  const bffRes = await fetch(`${baseUrl}/bff/incidents/${encodeURIComponent(incidentCode)}`);
  assert.equal(bffRes.status, 200);
  const bffText = await bffRes.text();
  assert.doesNotMatch(bffText, secretPattern);
  const bffJson = JSON.parse(bffText) as BackendIncidentDetail;
  assert.equal(bffJson.incident.incidentCode, incidentCode);
  assert.equal(bffJson.incident.title, expectedTitle);
  assert.ok(bffJson.plans.length >= 1, "expected at least one seeded remediation plan");
  assert.ok(bffJson.approvals.length >= 1, "expected at least one seeded approval");
  if (expectedRootCause) {
    assert.equal(bffJson.postMortem?.rootCause, expectedRootCause);
  }

  const notFoundBff = await fetch(`${baseUrl}/bff/incidents/INC-9999`);
  assert.equal(notFoundBff.status, 404);
});

test("restoring a stale AWAITING_APPROVAL thread preserves persisted Spring decision", () => {
  const decidedDetail: BackendIncidentDetail = {
    ...sampleDetail,
    incident: { ...sampleDetail.incident, status: "RESOLVED" },
    approvals: [{ ...sampleDetail.approvals[0], decision: "APPROVED", approvedBy: "oncall-lead" }],
  };
  let state = createInitialState(toIncidentListItem(decidedDetail.incident), decidedDetail);
  assert.equal(state.snapshot.workflowStatus, "RESOLVED");

  state = workspaceReducer(state, { type: "RESTORE_STARTED" });
  state = workspaceReducer(state, {
    type: "STATE_SNAPSHOT",
    snapshot: {
      incidentCode: "INC-2026-001",
      workflowStatus: "AWAITING_APPROVAL",
      plan: { ...demoPlan, planId: "PLAN-2026-001" },
      approval: { approvalId: "APR-2026-001", planId: "PLAN-2026-001", stepNo: 2, decision: "PENDING" },
      replanHistory: [],
    },
  });
  state = workspaceReducer(state, { type: "RUN_FINISHED" });

  assert.equal(state.snapshot.workflowStatus, "RESOLVED");
  assert.equal(state.snapshot.approval?.decision, "APPROVED");
  assert.equal(state.snapshot.plan?.workWindow?.start, demoPlan.workWindow?.start);
});

test("BACKEND_DETAIL_SYNCED retains live BLOCKED outcome when Spring has no new approval", () => {
  const openDetail: BackendIncidentDetail = {
    ...sampleDetail,
    incident: { ...sampleDetail.incident, status: "OPEN" },
    plans: [],
    approvals: [],
  };
  let state = createInitialState(toIncidentListItem(openDetail.incident), openDetail);

  state = workspaceReducer(state, {
    type: "STATE_SNAPSHOT",
    snapshot: {
      incidentCode: "INC-2026-001",
      workflowStatus: "BLOCKED",
      plan: { ...demoPlan, planId: "PLAN-REVISED-NO-ROLLBACK" },
      approval: null,
      replanHistory: [],
    },
  });
  assert.equal(state.snapshot.workflowStatus, "BLOCKED");

  state = workspaceReducer(state, { type: "BACKEND_DETAIL_SYNCED", detail: openDetail });
  assert.equal(state.snapshot.workflowStatus, "BLOCKED");
  assert.equal(state.snapshot.plan?.planId, "PLAN-REVISED-NO-ROLLBACK");
});

test("normalizeKstTimestamp and timeLabel treat offset-less Spring LocalDateTime as KST", () => {
  assert.equal(normalizeKstTimestamp("2026-11-18T14:06:00"), "2026-11-18T14:06:00+09:00");
  assert.equal(normalizeKstTimestamp("2026-11-18T14:06:00.123"), "2026-11-18T14:06:00.123+09:00");
  assert.equal(normalizeKstTimestamp("2026-11-18T05:06:00Z"), "2026-11-18T05:06:00Z");
  assert.equal(timeLabel("2026-11-18T14:06:00"), "14:06");
  assert.equal(timeLabel("2026-11-18T05:06:00Z"), "14:06");
});

test("hydrateSnapshotFromDetail selects the newest plan (plans[0]) when approvals are empty", () => {
  const detailNewestFirst: BackendIncidentDetail = {
    ...sampleDetail,
    incident: { ...sampleDetail.incident, status: "OPEN" },
    plans: [
      { ...sampleDetail.plans[0], id: 2, planId: "PLAN-2026-002", createdAt: "2026-11-18T14:15:00" },
      { ...sampleDetail.plans[0], id: 1, planId: "PLAN-2026-001", createdAt: "2026-11-18T14:07:00" },
    ],
    approvals: [],
  };

  const snap = hydrateSnapshotFromDetail(detailNewestFirst);
  assert.equal(snap.workflowStatus, "IDLE");
  assert.equal(snap.plan?.planId, "PLAN-2026-002");
});
