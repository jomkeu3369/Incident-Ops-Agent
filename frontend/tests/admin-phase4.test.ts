import assert from "node:assert/strict";
import test from "node:test";
import { dateLabelKst, timeLabel } from "../app/ag-ui-contract";
import {
  analyzeLogSessionAnomalies,
  createManagedPlan,
  deleteLogSession,
  deleteManagedPlan,
  getLogSessionDetail,
  listLogSessions,
  listManagedPlans,
  resolveAdminBaseUrl,
  updateManagedPlan,
} from "../app/ops-admin-client";

test("resolveAdminBaseUrl maps mock and live modes cleanly", () => {
  assert.equal(resolveAdminBaseUrl("mock"), "/bff/mock-ag-ui");
  assert.equal(resolveAdminBaseUrl("live"), "/api/ag-ui");
});

test("dateLabelKst derives KST calendar date consistently across midnight boundaries", () => {
  assert.equal(dateLabelKst("2026-11-18T16:00:00Z"), "2026-11-19");
  assert.equal(timeLabel("2026-11-18T16:00:00Z"), "01:00");
  assert.equal(dateLabelKst("2026-11-18T14:40:00+09:00"), "2026-11-18");
});

test("Managed plans CRUD and same-date overlap warning work end-to-end (F3015, F3016)", async (t) => {
  const baseUrl = process.env.TEST_AG_UI_BASE_URL;
  if (!baseUrl) {
    t.skip("Set TEST_AG_UI_BASE_URL to run live admin API integration test");
    return;
  }
  const adminBase = `${baseUrl}/bff/mock-ag-ui`;

  // 1) Create plan overlapping seeded 2026-11-18 14:40-14:45 -> saved WITH overlap warning
  const created = await createManagedPlan(adminBase, {
    incidentCode: "INC-2026-009",
    title: "추가 결제 롤백 점검",
    serviceName: "payment-service",
    planDate: "2026-11-18",
    startTime: "14:42",
    endTime: "14:50",
    actionCommand: "kubectl rollout status deployment/payment-service",
    notes: "겹침 경고 검증",
  });
  assert.equal(created.ok, true);
  if (!created.ok) return;
  assert.ok(created.data.warning, "expected overlap warning");
  assert.ok(created.data.overlappingPlans.length >= 1);
  const newId = created.data.plan.planId;

  // 2) Update plan to non-overlapping window and explicitly clear actionCommand with "" -> warning clears & command cleared
  const updated = await updateManagedPlan(adminBase, newId, {
    title: "추가 결제 롤백 점검 (수정)",
    startTime: "15:00",
    endTime: "15:10",
    actionCommand: "",
  });
  assert.equal(updated.ok, true);
  if (!updated.ok) return;
  assert.equal(updated.data.plan.title, "추가 결제 롤백 점검 (수정)");
  assert.equal(updated.data.plan.actionCommand, null);
  assert.equal(updated.data.warning, null);

  // 3) List confirms updated plan exists
  const listed = await listManagedPlans(adminBase);
  assert.equal(listed.ok, true);
  if (listed.ok) {
    assert.ok(listed.data.some((p) => p.planId === newId && p.startTime === "15:00"));
  }

  // 4) Delete plan and verify removal
  const deleted = await deleteManagedPlan(adminBase, newId);
  assert.equal(deleted.ok, true);
  const afterDelete = await listManagedPlans(adminBase);
  assert.equal(afterDelete.ok, true);
  if (afterDelete.ok) {
    assert.equal(afterDelete.data.some((p) => p.planId === newId), false);
  }
});

test("Session logs list, detail, anomaly analysis, and delete work end-to-end (F3014, F3020)", async (t) => {
  const baseUrl = process.env.TEST_AG_UI_BASE_URL;
  if (!baseUrl) {
    t.skip("Set TEST_AG_UI_BASE_URL to run live session log integration test");
    return;
  }
  const adminBase = `${baseUrl}/bff/mock-ag-ui`;

  const listed = await listLogSessions(adminBase);
  assert.equal(listed.ok, true);
  if (!listed.ok) return;
  assert.ok(listed.data.length >= 1);
  const targetId =
    listed.data.find((s) => s.sessionId === "sess-mock-replan")?.sessionId ?? listed.data[0].sessionId;

  const detail = await getLogSessionDetail(adminBase, targetId);
  assert.equal(detail.ok, true);
  if (detail.ok) {
    assert.ok(detail.data.events.length >= 1);
  }

  const anomaly = await analyzeLogSessionAnomalies(adminBase, targetId);
  assert.equal(anomaly.ok, true);
  if (anomaly.ok) {
    assert.ok(["NORMAL", "ANOMALY", "UNDETERMINED"].includes(anomaly.data.status));
  }

  const deleted = await deleteLogSession(adminBase, targetId);
  assert.equal(deleted.ok, true);
  const afterDetail = await getLogSessionDetail(adminBase, targetId);
  assert.equal(afterDetail.ok, false);
  if (!afterDetail.ok) {
    assert.equal(afterDetail.status, 404);
  }
});
