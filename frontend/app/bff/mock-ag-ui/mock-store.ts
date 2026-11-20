import type {
  AnomalyFindingItem,
  ManagedPlanCreateInput,
  ManagedPlanItem,
  ManagedPlanMutationResponse,
  ManagedPlanUpdateInput,
  SessionAnomalyResponse,
  SessionLogEvent,
  SessionSummary,
} from "../../ops-admin-client";

type MockAdminStore = {
  plans: Map<string, ManagedPlanItem>;
  sessions: Map<string, SessionLogEvent[]>;
  seeded: boolean;
};

const globalStore = globalThis as typeof globalThis & {
  __mockOpsAdminStore?: MockAdminStore;
};

function getStore(): MockAdminStore {
  if (!globalStore.__mockOpsAdminStore) {
    globalStore.__mockOpsAdminStore = {
      plans: new Map(),
      sessions: new Map(),
      seeded: false,
    };
  }
  const store = globalStore.__mockOpsAdminStore;
  if (!store.seeded) {
    store.seeded = true;
    const now = "2026-11-18T14:15:00+09:00";
    store.plans.set("PLAN-MOCK-001", {
      planId: "PLAN-MOCK-001",
      incidentCode: "INC-2026-009",
      title: "결제 서비스 v41 롤백 작업",
      serviceName: "payment-service",
      planDate: "2026-11-18",
      startTime: "14:40",
      endTime: "14:45",
      actionCommand: "kubectl rollout undo deployment/payment-service",
      evidenceChunkIds: ["LOG-901", "RUNBOOK-01"],
      notes: "변경 금지 구간(14:30~14:40) 이후 재계획된 작업 창",
      createdAt: now,
      updatedAt: now,
    });

    store.sessions.set("sess-mock-replan", [
      {
        sessionId: "sess-mock-replan",
        eventType: "step_started",
        timestamp: "2026-11-18T14:12:00+09:00",
        payload: { step: "analyze" },
      },
      {
        sessionId: "sess-mock-replan",
        eventType: "step_finished",
        timestamp: "2026-11-18T14:12:01+09:00",
        payload: { step: "plan", steps: ["a2a:log-analyzer-agent", "a2a:remediation-agent"] },
      },
      {
        sessionId: "sess-mock-replan",
        eventType: "tool_call",
        timestamp: "2026-11-18T14:12:02+09:00",
        payload: { tool: "list_rollback_candidates", args: { serviceName: "payment-service" }, ok: true, elapsedMs: 142 },
      },
      {
        sessionId: "sess-mock-replan",
        eventType: "replan",
        timestamp: "2026-11-18T14:12:04+09:00",
        payload: { reason: "기존 작업 창(14:28~14:33)이 변경 금지 시간과 겹칩니다." },
      },
      {
        sessionId: "sess-mock-replan",
        eventType: "step_finished",
        timestamp: "2026-11-18T14:12:05+09:00",
        payload: { step: "evaluate", workflowStatus: "AWAITING_APPROVAL" },
      },
    ]);
  }
  return store;
}

function findOverlaps(plans: Iterable<ManagedPlanItem>, target: ManagedPlanItem): ManagedPlanItem[] {
  const overlapping: ManagedPlanItem[] = [];
  for (const existing of plans) {
    if (existing.planId === target.planId) continue;
    if (existing.planDate !== target.planDate) continue;
    if (existing.startTime < target.endTime && existing.endTime > target.startTime) {
      overlapping.push(existing);
    }
  }
  return overlapping;
}

export function mockListPlans(): ManagedPlanItem[] {
  return Array.from(getStore().plans.values()).sort((a, b) =>
    `${a.planDate} ${a.startTime}`.localeCompare(`${b.planDate} ${b.startTime}`),
  );
}

export function mockGetPlan(planId: string): ManagedPlanItem | null {
  return getStore().plans.get(planId) ?? null;
}

export function mockCreatePlan(body: ManagedPlanCreateInput): ManagedPlanMutationResponse {
  const store = getStore();
  const now = new Date().toISOString();
  const planId = body.planId?.trim() || `PLAN-${Date.now().toString(36).toUpperCase()}`;
  const item: ManagedPlanItem = {
    planId,
    incidentCode: body.incidentCode.trim(),
    title: body.title.trim(),
    serviceName: body.serviceName.trim() || "payment-service",
    planDate: body.planDate,
    startTime: body.startTime,
    endTime: body.endTime,
    actionCommand: body.actionCommand ?? null,
    evidenceChunkIds: body.evidenceChunkIds ?? [],
    notes: body.notes ?? "",
    createdAt: now,
    updatedAt: now,
  };
  const overlappingPlans = findOverlaps(store.plans.values(), item);
  store.plans.set(planId, item);
  const warning =
    overlappingPlans.length > 0
      ? `같은 날짜(${item.planDate})에 시간이 겹치는 계획 ${overlappingPlans.length}건이 있습니다.`
      : null;
  return { plan: item, overlappingPlans, warning };
}

export function mockUpdatePlan(
  planId: string,
  patch: ManagedPlanUpdateInput,
): { ok: true; data: ManagedPlanMutationResponse } | { ok: false; status: number; message: string } {
  const store = getStore();
  const existing = store.plans.get(planId);
  if (!existing) {
    return { ok: false, status: 404, message: `계획을 찾을 수 없습니다: ${planId}` };
  }
  const next: ManagedPlanItem = {
    ...existing,
    title: patch.title !== undefined ? patch.title.trim() : existing.title,
    planDate: patch.planDate ?? existing.planDate,
    startTime: patch.startTime ?? existing.startTime,
    endTime: patch.endTime ?? existing.endTime,
    actionCommand:
      patch.actionCommand !== undefined ? (patch.actionCommand ? patch.actionCommand.trim() : null) : existing.actionCommand,
    notes: patch.notes !== undefined ? patch.notes : existing.notes,
    updatedAt: new Date().toISOString(),
  };
  if (next.startTime >= next.endTime) {
    return { ok: false, status: 400, message: "시작 시각은 종료 시각보다 이전이어야 합니다." };
  }
  const overlappingPlans = findOverlaps(store.plans.values(), next);
  store.plans.set(planId, next);
  const warning =
    overlappingPlans.length > 0
      ? `같은 날짜(${next.planDate})에 시간이 겹치는 계획 ${overlappingPlans.length}건이 있습니다.`
      : null;
  return { ok: true, data: { plan: next, overlappingPlans, warning } };
}

export function mockDeletePlan(planId: string): boolean {
  return getStore().plans.delete(planId);
}

export function mockRecordSessionEvent(sessionId: string, eventType: string, payload: Record<string, unknown>): void {
  const store = getStore();
  const list = store.sessions.get(sessionId) ?? [];
  list.push({
    sessionId,
    eventType,
    timestamp: new Date().toISOString(),
    payload,
  });
  store.sessions.set(sessionId, list);
}

export function mockListSessions(): SessionSummary[] {
  const result: SessionSummary[] = [];
  for (const [sessionId, events] of getStore().sessions.entries()) {
    result.push({
      sessionId,
      eventCount: events.length,
      startedAt: events[0]?.timestamp ?? null,
      lastEventAt: events[events.length - 1]?.timestamp ?? null,
    });
  }
  return result.reverse();
}

export function mockGetSessionEvents(sessionId: string): SessionLogEvent[] | null {
  return getStore().sessions.get(sessionId) ?? null;
}

export function mockDeleteSession(sessionId: string): boolean {
  return getStore().sessions.delete(sessionId);
}

export function mockAnalyzeSessionAnomalies(sessionId: string): SessionAnomalyResponse | null {
  const events = getStore().sessions.get(sessionId);
  if (!events) return null;

  const relevant = events.filter((e) =>
    ["step_started", "step_finished", "tool_call", "replan", "llm_decision"].includes(e.eventType),
  );
  if (relevant.length === 0) {
    return {
      sessionId,
      status: "UNDETERMINED",
      findings: [],
      diagnosis: null,
      llmCalls: 0,
      evaluatedEventCount: events.length,
    };
  }

  const findings: AnomalyFindingItem[] = [];
  const counts = new Map<string, number>();
  let replanCount = 0;

  for (const ev of relevant) {
    const payload = ev.payload ?? {};
    if (ev.eventType === "replan") replanCount += 1;
    if (ev.eventType === "tool_call" && typeof payload.tool === "string") {
      const key = `${payload.tool}:${JSON.stringify(payload.args ?? {})}`;
      counts.set(key, (counts.get(key) ?? 0) + 1);
    }
  }

  for (const [key, count] of counts.entries()) {
    if (count >= 2) {
      const tool = key.split(":")[0];
      findings.push({
        rule: "REPEATED_IDENTICAL_CALL",
        severity: count >= 3 ? "HIGH" : "WARN",
        summary: `도구 \`${tool}\`이 동일한 입력으로 ${count}회 반복 호출되었습니다`,
        evidence: { tool, count },
      });
    }
  }
  if (replanCount >= 3) {
    findings.push({
      rule: "EXCESSIVE_REPLAN",
      severity: "HIGH",
      summary: `재계획이 ${replanCount}회 발생해 임계치(3회)에 도달했습니다`,
      evidence: { replanCount, threshold: 3 },
    });
  }

  if (findings.length === 0) {
    return {
      sessionId,
      status: "NORMAL",
      findings: [],
      diagnosis: null,
      llmCalls: 0,
      evaluatedEventCount: events.length,
    };
  }

  return {
    sessionId,
    status: "ANOMALY",
    findings,
    diagnosis: `세션 실행 로그에서 이상 규칙 위반이 ${findings.length}건 탐지되었습니다. ${findings[0].summary}. 도구 입력 제약과 재계획 조건을 점검해 재발을 방지하세요.`,
    llmCalls: 0,
    evaluatedEventCount: events.length,
  };
}
