import assert from "node:assert/strict";
import test from "node:test";
import { resolveInitialMode, runAgUiRequest } from "../app/ag-ui-client";
import { incidents, nextWindow, previousWindow } from "../app/demo";
import {
  canResumeApprovedExecution,
  canSendPrompt,
  canSubmitDecision,
  createInitialState,
  workspaceReducer,
  type WorkspaceAction,
} from "../app/workspace-state";

const activeIncident = incidents[0];

test("reducer handles streaming text, steps, tool calls, replan, and incident isolation", () => {
  let state = createInitialState(activeIncident);

  state = workspaceReducer(state, { type: "RUN_STARTED" });
  assert.equal(state.busy, true);

  state = workspaceReducer(state, { type: "STEP_STARTED", stepName: "execute" });
  assert.equal(state.activeStep, "워커 에이전트 실행");

  state = workspaceReducer(state, {
    type: "TOOL_CALL_START",
    toolCallId: "tc-1",
    toolCallName: "log-analyzer-agent",
  });
  state = workspaceReducer(state, {
    type: "TOOL_CALL_RESULT",
    toolCallId: "tc-1",
    messageId: "msg-tc-1",
    content: JSON.stringify({ ok: true, pulledCount: 2, chunkIds: ["LOG-104", "RUNBOOK-01"] }),
  });
  assert.equal(state.entries.at(-1)?.label, "로그 분석 에이전트");
  assert.match(state.entries.at(-1)?.text ?? "", /LOG-104/);

  state = workspaceReducer(state, {
    type: "CUSTOM_REPLAN",
    value: {
      reason: "변경 금지 시간과 겹칩니다.",
      previousWindow,
      nextWindow,
    },
  });
  assert.equal(state.replan?.nextWindow.start, nextWindow.start);

  // Snapshot for a different incident must be ignored
  state = workspaceReducer(state, {
    type: "STATE_SNAPSHOT",
    snapshot: {
      incidentCode: "INC-2026-999",
      workflowStatus: "RESOLVED",
      plan: null,
      approval: null,
      replanHistory: [],
    },
  });
  assert.equal(state.snapshot.incidentCode, activeIncident.incidentCode);
  assert.equal(state.snapshot.workflowStatus, "IDLE");

  // Streaming assistant text deltas concatenate cleanly
  state = workspaceReducer(state, { type: "TEXT_MESSAGE_START", messageId: "m-1", role: "assistant" });
  state = workspaceReducer(state, { type: "TEXT_MESSAGE_CONTENT", messageId: "m-1", delta: "롤백 계획을 " });
  state = workspaceReducer(state, { type: "TEXT_MESSAGE_CONTENT", messageId: "m-1", delta: "등록했습니다." });
  state = workspaceReducer(state, { type: "TEXT_MESSAGE_END", messageId: "m-1" });
  assert.equal(state.entries.at(-1)?.text, "롤백 계획을 등록했습니다.");
  assert.equal(state.entries.at(-1)?.streaming, false);

  state = workspaceReducer(state, { type: "RUN_FINISHED" });
  assert.equal(state.busy, false);
  assert.equal(state.activeStep, null);
});

test("canSendPrompt allows IDLE, REJECTED, BLOCKED, EXECUTION_FAILED and blocks AWAITING_APPROVAL/RESOLVED", () => {
  assert.equal(canSendPrompt("IDLE", false), true);
  assert.equal(canSendPrompt("REJECTED", false), true);
  assert.equal(canSendPrompt("BLOCKED", false), true);
  assert.equal(canSendPrompt("EXECUTION_FAILED", false), true);
  assert.equal(canSendPrompt("PLANNING", false), false);
  assert.equal(canSendPrompt("AWAITING_APPROVAL", false), false);
  assert.equal(canSendPrompt("RESOLVED", false), false);
  assert.equal(canSendPrompt("IDLE", true), false);
});

test("resuming interrupted approved execution allows APPROVED only and blocks REJECTED", () => {
  const awaitingApproval = {
    incidentCode: activeIncident.incidentCode,
    workflowStatus: "AWAITING_APPROVAL" as const,
    plan: null,
    approval: { approvalId: "APR-1", planId: "PLAN-1", stepNo: 1, decision: "PENDING" as const },
    replanHistory: [],
  };
  assert.equal(canResumeApprovedExecution(awaitingApproval), false);
  assert.equal(canSubmitDecision(awaitingApproval, "APPROVED"), true);
  assert.equal(canSubmitDecision(awaitingApproval, "REJECTED"), true);

  const interrupted = {
    ...awaitingApproval,
    workflowStatus: "AWAITING_EXECUTION_RESULT" as const,
    approval: { ...awaitingApproval.approval, decision: "APPROVED" as const },
  };
  assert.equal(canResumeApprovedExecution(interrupted), true);
  assert.equal(canSubmitDecision(interrupted, "APPROVED"), true);
  assert.equal(canSubmitDecision(interrupted, "REJECTED"), false);

  const resolved = { ...interrupted, workflowStatus: "RESOLVED" as const };
  assert.equal(canResumeApprovedExecution(resolved), false);
  assert.equal(canSubmitDecision(resolved, "APPROVED"), false);
});

test("resolveInitialMode honors explicit live/mock URL param over env default", () => {
  assert.equal(resolveInitialMode("live", "mock"), "live");
  assert.equal(resolveInitialMode("mock", "live"), "mock");
  assert.equal(resolveInitialMode(undefined, "mock"), "mock");
  assert.equal(resolveInitialMode(undefined, undefined), "live");
});

test("HttpAgent SSE end-to-end against /api/mock-ag-ui (plan, restore, approve, 409, reject, error, abort)", async (t) => {
  const baseUrl = process.env.TEST_AG_UI_BASE_URL;
  if (!baseUrl) {
    t.skip("Set TEST_AG_UI_BASE_URL to run live SSE integration test");
    return;
  }
  const url = `${baseUrl}/api/mock-ag-ui`;

  // 1) Planning request
  let state = createInitialState(activeIncident);
  const dispatch = (action: WorkspaceAction) => {
    state = workspaceReducer(state, action);
  };
  const threadId = `test-thread-${Date.now()}`;
  const ac1 = new AbortController();

  const planOutcome = await runAgUiRequest({
    url,
    threadId,
    runId: "run-1",
    userMessage: { id: "u-1", content: "이전 버전 502 비율 확인 후 롤백 계획 제안해줘" },
    forwardedProps: { incidentCode: activeIncident.incidentCode, incidentStartedAt: activeIncident.startedAt },
    signal: ac1.signal,
    dispatch,
  });
  assert.equal(planOutcome.ok, true);
  assert.equal(state.busy, false);
  assert.equal(state.snapshot.workflowStatus, "AWAITING_APPROVAL");
  assert.equal(state.snapshot.approval?.decision, "PENDING");
  assert.equal(state.replan?.nextWindow.start, nextWindow.start);
  assert.ok(state.entries.some((e) => e.kind === "assistant" && e.text.includes("v41")));

  // 2) Reload restore (restore: true)
  let restoredState = createInitialState(activeIncident);
  const restoreOutcome = await runAgUiRequest({
    url,
    threadId,
    runId: "run-restore",
    forwardedProps: { incidentCode: activeIncident.incidentCode, restore: true },
    signal: new AbortController().signal,
    dispatch: (a) => {
      restoredState = workspaceReducer(restoredState, a);
    },
  });
  assert.equal(restoreOutcome.ok, true);
  assert.equal(restoredState.snapshot.workflowStatus, "AWAITING_APPROVAL");
  assert.ok(restoredState.entries.some((e) => e.kind === "assistant"));

  // 3) Approve -> RESOLVED
  const approvalId = state.snapshot.approval!.approvalId;
  const approveOutcome = await runAgUiRequest({
    url,
    threadId,
    runId: "run-approve",
    forwardedProps: {
      incidentCode: activeIncident.incidentCode,
      approvalDecision: { approvalId, decision: "APPROVED" },
    },
    signal: new AbortController().signal,
    dispatch,
  });
  assert.equal(approveOutcome.ok, true);
  assert.equal(state.snapshot.workflowStatus, "RESOLVED");
  assert.equal(state.snapshot.approval?.decision, "APPROVED");

  // 4) Duplicate approval -> HTTP 409 conflict + needsResync
  const conflictOutcome = await runAgUiRequest({
    url,
    threadId,
    runId: "run-conflict",
    forwardedProps: {
      incidentCode: activeIncident.incidentCode,
      approvalDecision: { approvalId, decision: "APPROVED" },
    },
    signal: new AbortController().signal,
    dispatch,
  });
  assert.equal(conflictOutcome.ok, false);
  if (!conflictOutcome.ok) {
    assert.equal(conflictOutcome.statusCode, 409);
    assert.equal(conflictOutcome.needsResync, true);
  }
  assert.equal(state.needsResync, true);

  // 4b) Rejection flow on fresh thread -> REJECTED
  let rejectState = createInitialState(activeIncident);
  const rejectDispatch = (a: WorkspaceAction) => {
    rejectState = workspaceReducer(rejectState, a);
  };
  const rejectThreadId = `${threadId}-reject`;
  const rejectPlanOutcome = await runAgUiRequest({
    url,
    threadId: rejectThreadId,
    runId: "run-reject-plan",
    userMessage: { id: "u-r1", content: "롤백 계획 제안해줘" },
    forwardedProps: { incidentCode: activeIncident.incidentCode, incidentStartedAt: activeIncident.startedAt },
    signal: new AbortController().signal,
    dispatch: rejectDispatch,
  });
  assert.equal(rejectPlanOutcome.ok, true);
  const rejectApprovalId = rejectState.snapshot.approval!.approvalId;
  const rejectOutcome = await runAgUiRequest({
    url,
    threadId: rejectThreadId,
    runId: "run-reject-decide",
    forwardedProps: {
      incidentCode: activeIncident.incidentCode,
      approvalDecision: { approvalId: rejectApprovalId, decision: "REJECTED" },
    },
    signal: new AbortController().signal,
    dispatch: rejectDispatch,
  });
  assert.equal(rejectOutcome.ok, true);
  assert.equal(rejectState.snapshot.workflowStatus, "REJECTED");
  assert.equal(rejectState.snapshot.approval?.decision, "REJECTED");

  // 5) Error event (RUN_ERROR)
  let errState = createInitialState(activeIncident);
  const errOutcome = await runAgUiRequest({
    url,
    threadId: `${threadId}-err`,
    runId: "run-err",
    userMessage: { id: "u-err", content: "오류 테스트" },
    forwardedProps: { incidentCode: activeIncident.incidentCode, incidentStartedAt: activeIncident.startedAt },
    signal: new AbortController().signal,
    dispatch: (a) => {
      errState = workspaceReducer(errState, a);
    },
  });
  assert.equal(errOutcome.ok, false);
  assert.match(errState.error ?? "", /일시 오류/);

  // 6) Mid-stream abort (incident switch isolation)
  const abortController = new AbortController();
  let abortedEventsAfterCancel = 0;
  const abortPromise = runAgUiRequest({
    url,
    threadId: `${threadId}-abort`,
    runId: "run-abort",
    userMessage: { id: "u-abort", content: "롤백 계획" },
    forwardedProps: { incidentCode: activeIncident.incidentCode, incidentStartedAt: activeIncident.startedAt },
    signal: abortController.signal,
    dispatch: () => {
      if (abortController.signal.aborted) {
        abortedEventsAfterCancel += 1;
      }
    },
  });
  setTimeout(() => abortController.abort(), 50);
  const abortOutcome = await abortPromise;
  assert.equal(abortOutcome.ok, false);
  if (!abortOutcome.ok) {
    assert.equal(abortOutcome.aborted, true);
  }
  assert.equal(abortedEventsAfterCancel, 0);
});
