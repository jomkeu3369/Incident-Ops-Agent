import {
  formatToolResultSummary,
  normalizeSnapshot,
  stepLabel,
  windowLabel,
  type A2aProgress,
  type ChatMessageRecord,
  type CheckpointSync,
  type Plan,
  type ReplanRecord,
  type Snapshot,
  type WorkflowStatus,
} from "./ag-ui-contract";
import {
  hydrateSnapshotFromDetail,
  type BackendIncidentDetail,
  type BackendPostMortem,
  type BackendRemediationExecution,
  type IncidentListItem,
} from "./backend-api";
import { initialSnapshot } from "./demo";

export type EntryKind = "record" | "operator" | "assistant" | "step" | "tool" | "change";

export type TimelineEntry = {
  id: string;
  label: string;
  text: string;
  kind: EntryKind;
  streaming?: boolean;
};

export type WorkspaceState = {
  incidentCode: string;
  snapshot: Snapshot;
  executions: BackendRemediationExecution[];
  postMortem: BackendPostMortem | null;
  entries: TimelineEntry[];
  toolNames: Record<string, string>;
  activeStep: string | null;
  lastEventSeq: number;
  lastCompletedStep: string | null;
  a2aProgress: A2aProgress | null;
  replan: ReplanRecord | null;
  busy: boolean;
  restoring: boolean;
  error: string | null;
  needsResync: boolean;
};

export type WorkspaceAction =
  | { type: "RESET"; incident: IncidentListItem; detail?: BackendIncidentDetail | null }
  | { type: "REQUEST"; entry: TimelineEntry }
  | { type: "RESTORE_STARTED" }
  | { type: "RUN_STARTED" }
  | { type: "RUN_FINISHED" }
  | { type: "RUN_ERROR"; message: string; needsResync?: boolean }
  | { type: "STEP_STARTED"; stepName: string }
  | { type: "STEP_FINISHED"; stepName: string }
  | { type: "TOOL_CALL_START"; toolCallId: string; toolCallName: string }
  | { type: "TOOL_CALL_RESULT"; toolCallId: string; messageId: string; content: string }
  | { type: "TEXT_MESSAGE_START"; messageId: string; role: string }
  | { type: "TEXT_MESSAGE_CONTENT"; messageId: string; delta: string }
  | { type: "TEXT_MESSAGE_END"; messageId: string }
  | { type: "STATE_SNAPSHOT"; snapshot: unknown }
  | { type: "CHECKPOINT_SYNC"; value: CheckpointSync }
  | { type: "A2A_PROGRESS"; value: A2aProgress }
  | { type: "MESSAGES_SNAPSHOT"; messages: ChatMessageRecord[] }
  | { type: "CUSTOM_REPLAN"; value: ReplanRecord }
  | { type: "BACKEND_DETAIL_SYNCED"; detail: BackendIncidentDetail };

const LEDGER_OUTRANKS_CANCELLED: ReadonlySet<Snapshot["workflowStatus"]> = new Set([
  "AWAITING_APPROVAL",
  "AWAITING_EXECUTION_RESULT",
  "EXECUTION_FAILED",
  "RESOLVED",
]);

function mergePlans(backendPlan: Plan | null, existingPlan: Plan | null): Plan | null {
  if (!backendPlan) return existingPlan;
  if (!existingPlan || existingPlan.planId !== backendPlan.planId) {
    return {
      ...backendPlan,
      steps: backendPlan.steps ?? [],
      evidence: backendPlan.evidence ?? [],
    };
  }
  const existingEvidence = existingPlan.evidence ?? [];
  const backendEvidence = backendPlan.evidence ?? [];
  return {
    ...backendPlan,
    steps: (backendPlan.steps ?? []).length > 0 ? backendPlan.steps : (existingPlan.steps ?? []),
    workWindow: backendPlan.workWindow ?? existingPlan.workWindow ?? null,
    evidence: existingEvidence.length > 0 ? existingEvidence : backendEvidence,
  };
}

export function createInitialState(
  incident: IncidentListItem,
  detail?: BackendIncidentDetail | null,
): WorkspaceState {
  const base = detail ? hydrateSnapshotFromDetail(detail) : initialSnapshot(incident);
  return {
    incidentCode: incident.incidentCode,
    snapshot: base,
    executions: detail?.executions ?? [],
    postMortem: detail?.postMortem ?? null,
    entries: [
      { id: "intake", label: "장애 접수", text: incident.summary || incident.title, kind: "record" },
      ...(incident.resolved || base.workflowStatus === "RESOLVED"
        ? []
        : [
            {
              id: "scope",
              label: "요청 조건",
              text: "이전 버전의 502 비율과 변경 금지 시간을 확인합니다.",
              kind: "record" as const,
            },
          ]),
    ],
    toolNames: {},
    activeStep: null,
    lastEventSeq: base.eventSeq ?? 0,
    lastCompletedStep: null,
    a2aProgress: null,
    replan: null,
    busy: false,
    restoring: false,
    error: null,
    needsResync: false,
  };
}

export function workspaceReducer(state: WorkspaceState, action: WorkspaceAction): WorkspaceState {
  switch (action.type) {
    case "RESET":
      return createInitialState(action.incident, action.detail);

    case "REQUEST":
      return {
        ...state,
        busy: true,
        error: null,
        needsResync: false,
        entries: [...state.entries, action.entry],
      };

    case "RESTORE_STARTED":
      return {
        ...state,
        busy: true,
        restoring: true,
        error: null,
        needsResync: false,
      };

    case "RUN_STARTED":
      return { ...state, busy: true, error: null, a2aProgress: null };

    case "RUN_FINISHED":
      return {
        ...state,
        busy: false,
        restoring: false,
        activeStep: null,
        entries: state.entries.map((entry) => (entry.streaming ? { ...entry, streaming: false } : entry)),
      };

    case "RUN_ERROR":
      return {
        ...state,
        busy: false,
        restoring: false,
        activeStep: null,
        error: action.message,
        needsResync: Boolean(action.needsResync),
        entries: state.entries.map((entry) => (entry.streaming ? { ...entry, streaming: false } : entry)),
      };

    case "STEP_STARTED":
      return {
        ...state,
        activeStep: stepLabel(action.stepName),
      };

    case "STEP_FINISHED": {
      const label = stepLabel(action.stepName);
      return {
        ...state,
        activeStep: state.activeStep === label ? null : state.activeStep,
      };
    }

    case "TOOL_CALL_START":
      return {
        ...state,
        toolNames: { ...state.toolNames, [action.toolCallId]: action.toolCallName },
      };

    case "TOOL_CALL_RESULT": {
      const toolName = state.toolNames[action.toolCallId];
      const { label, text } = formatToolResultSummary(toolName, action.content);
      const id = action.messageId || `tool-${action.toolCallId}`;
      if (state.entries.some((entry) => entry.id === id)) {
        return state;
      }
      return {
        ...state,
        entries: [...state.entries, { id, label, text, kind: "tool" }],
      };
    }

    case "TEXT_MESSAGE_START": {
      if (action.role !== "assistant") return state;
      if (state.entries.some((entry) => entry.id === action.messageId)) {
        return state;
      }
      return {
        ...state,
        entries: [
          ...state.entries,
          { id: action.messageId, label: "에이전트", text: "", kind: "assistant", streaming: true },
        ],
      };
    }

    case "TEXT_MESSAGE_CONTENT": {
      const exists = state.entries.some((entry) => entry.id === action.messageId);
      if (!exists) {
        return {
          ...state,
          entries: [
            ...state.entries,
            {
              id: action.messageId,
              label: "에이전트",
              text: action.delta,
              kind: "assistant",
              streaming: true,
            },
          ],
        };
      }
      return {
        ...state,
        entries: state.entries.map((entry) =>
          entry.id === action.messageId ? { ...entry, text: entry.text + action.delta } : entry,
        ),
      };
    }

    case "TEXT_MESSAGE_END":
      return {
        ...state,
        entries: state.entries.map((entry) =>
          entry.id === action.messageId ? { ...entry, streaming: false } : entry,
        ),
      };

    case "STATE_SNAPSHOT": {
      const next = normalizeSnapshot(action.snapshot, state.incidentCode);
      if (
        !next ||
        next.incidentCode !== state.incidentCode ||
        (next.eventSeq !== undefined && next.eventSeq < state.lastEventSeq)
      ) {
        return state;
      }
      const latestReplan =
        next.replanHistory.length > 0 ? next.replanHistory[next.replanHistory.length - 1] : state.replan;

      // During restore, never overwrite an authoritative persisted Spring state (resolved or already decided)
      // with a stale IDLE or AWAITING_APPROVAL snapshot from Redis thread memory.
      if (state.restoring) {
        const currentIsPersistedTerminalOrDecided =
          state.snapshot.workflowStatus === "RESOLVED" ||
          state.snapshot.workflowStatus === "REJECTED" ||
          state.snapshot.workflowStatus === "EXECUTION_FAILED" ||
          state.snapshot.workflowStatus === "AWAITING_EXECUTION_RESULT";
        const incomingIsStale =
          next.workflowStatus === "IDLE" ||
          (currentIsPersistedTerminalOrDecided && next.workflowStatus === "AWAITING_APPROVAL");
        if (incomingIsStale) {
          return {
            ...state,
            snapshot: {
              ...state.snapshot,
              plan: mergePlans(state.snapshot.plan, next.plan),
              replanHistory: next.replanHistory.length > 0 ? next.replanHistory : state.snapshot.replanHistory,
            },
            replan: latestReplan,
          };
        }
      }

      if (state.snapshot.workflowStatus === "RESOLVED" && next.workflowStatus !== "RESOLVED") {
        return state;
      }

      return {
        ...state,
        lastEventSeq: next.eventSeq ?? state.lastEventSeq,
        snapshot: {
          ...next,
          plan: mergePlans(next.plan, state.snapshot.plan),
        },
        replan: latestReplan,
      };
    }

    case "CHECKPOINT_SYNC":
      return action.value.incidentCode !== state.incidentCode || action.value.lastEventSeq < state.lastEventSeq
        ? state
        : { ...state, lastEventSeq: action.value.lastEventSeq, lastCompletedStep: action.value.lastCompletedStep };

    case "A2A_PROGRESS":
      return action.value.incidentCode !== state.incidentCode
        ? state
        : { ...state, a2aProgress: action.value };

    case "MESSAGES_SNAPSHOT": {
      if (!action.messages.length) return state;
      const baseRecords = state.entries.filter((entry) => entry.id === "intake" || entry.id === "scope");
      const restored: TimelineEntry[] = action.messages
        .filter((msg) => msg.role === "user" || msg.role === "assistant")
        .map((msg) => ({
          id: msg.id,
          label: msg.role === "user" ? "운영자" : "에이전트",
          text: msg.content,
          kind: msg.role === "user" ? "operator" : "assistant",
        }));
      const replanEntries: TimelineEntry[] = state.snapshot.replanHistory.map((item, idx) => ({
        id: `replan-restored-${idx}`,
        label: "작업 창 재계획",
        text: `${item.reason} ${windowLabel(item.nextWindow)}로 이동했습니다.`,
        kind: "change",
      }));
      return {
        ...state,
        entries: [...baseRecords, ...replanEntries, ...restored],
      };
    }

    case "CUSTOM_REPLAN": {
      const replan = action.value;
      const entryId = `replan-${replan.previousWindow.start}-${replan.nextWindow.start}`;
      const alreadyExists = state.entries.some((entry) => entry.id === entryId);
      return {
        ...state,
        replan,
        entries: alreadyExists
          ? state.entries
          : [
              ...state.entries,
              {
                id: entryId,
                label: "작업 창 재계획",
                text: `${replan.reason} ${windowLabel(replan.nextWindow)}로 이동했습니다.`,
                kind: "change",
              },
            ],
      };
    }

    case "BACKEND_DETAIL_SYNCED": {
      if (action.detail.incident.incidentCode !== state.incidentCode) {
        return state;
      }
      const hydrated = hydrateSnapshotFromDetail(action.detail);
      // Retain a live BLOCKED planning outcome (insufficient evidence / REVISED_NO_ROLLBACK)
      // when Spring has neither resolved the incident nor registered a new pending approval.
      const retainBlockedOutcome =
        state.snapshot.workflowStatus === "BLOCKED" &&
        hydrated.workflowStatus !== "RESOLVED" &&
        hydrated.workflowStatus !== "AWAITING_APPROVAL";
      const regressesResolved =
        state.snapshot.workflowStatus === "RESOLVED" && hydrated.workflowStatus !== "RESOLVED";
      // 취소 표시는 원장에 처리할 승인이나 실행 결과가 없을 때만 유지한다.
      const retainCancelled =
        state.snapshot.workflowStatus === "CANCELLED" &&
        !LEDGER_OUTRANKS_CANCELLED.has(hydrated.workflowStatus);
      if (retainBlockedOutcome || regressesResolved || retainCancelled) {
        return {
          ...state,
          executions: action.detail.executions,
          postMortem: action.detail.postMortem,
          needsResync: false,
        };
      }
      return {
        ...state,
        snapshot: {
          ...hydrated,
          plan: mergePlans(hydrated.plan, state.snapshot.plan),
          replanHistory: state.snapshot.replanHistory,
        },
        executions: action.detail.executions,
        postMortem: action.detail.postMortem,
        needsResync: false,
      };
    }
  }
}

export function canSendPrompt(status: WorkflowStatus, resolved: boolean): boolean {
  if (resolved) return false;
  return status === "IDLE" || status === "REJECTED" || status === "BLOCKED" || status === "EXECUTION_FAILED" ||
    status === "CANCELLED";
}

export function canResumeApprovedExecution(snapshot: Snapshot): boolean {
  return (
    snapshot.workflowStatus === "AWAITING_EXECUTION_RESULT" &&
    snapshot.approval !== null &&
    snapshot.approval.decision === "APPROVED"
  );
}

export function canSubmitDecision(snapshot: Snapshot, decision: "APPROVED" | "REJECTED"): boolean {
  if (!snapshot.approval) return false;
  if (snapshot.workflowStatus === "AWAITING_APPROVAL" && snapshot.approval.decision === "PENDING") {
    return true;
  }
  return decision === "APPROVED" && canResumeApprovedExecution(snapshot);
}
