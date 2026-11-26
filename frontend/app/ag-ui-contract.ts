export type WorkflowStatus =
  | "IDLE"
  | "PLANNING"
  | "AWAITING_APPROVAL"
  | "AWAITING_EXECUTION_RESULT"
  | "REJECTED"
  | "RESOLVED"
  | "EXECUTION_FAILED"
  | "CANCELLED"
  | "BLOCKED";

export type Decision = "APPROVED" | "REJECTED";

export type WorkWindow = {
  start: string;
  end: string;
};

export type PlanStep = {
  stepNo: number;
  description: string;
  condition?: string;
  actionCommand?: string | null;
  evidenceChunkIds: string[];
};

export type EvidenceItem = {
  chunkId: string;
  summary: string;
  corpusType?: string;
};

export type Plan = {
  planId: string;
  workWindow?: WorkWindow | null;
  steps: PlanStep[];
  evidence: EvidenceItem[];
  verificationCriteria: string;
};

export type Approval = {
  approvalId: string;
  planId: string;
  stepNo: number;
  decision: "PENDING" | Decision;
};

export type ReplanRecord = {
  reason: string;
  previousWindow: WorkWindow;
  nextWindow: WorkWindow;
};

export type Snapshot = {
  incidentCode: string;
  eventSeq?: number;
  workflowStatus: WorkflowStatus;
  plan: Plan | null;
  approval: Approval | null;
  replanHistory: ReplanRecord[];
};

export type ChatMessageRecord = {
  id: string;
  role: "user" | "assistant" | "system" | "tool";
  content: string;
};

export type ForwardedProps = {
  incidentCode: string;
  incidentStartedAt?: string;
  approvalDecision?: {
    approvalId: string;
    decision: Decision;
  };
  restore?: true;
  resume?: true;
  explicitCancel?: true;
  lastEventSeq?: number;
};

export type CheckpointSync = {
  incidentCode: string;
  lastEventSeq: number;
  lastCompletedStep: string | null;
};

export type A2aProgress = {
  version: number;
  incidentCode: string;
  runId: string;
  worker: string;
  taskId: string;
  state: string;
  stage: string;
  elapsedMs: number;
};

export function eventSequence(...values: unknown[]): number | undefined {
  return values.find((value): value is number =>
    typeof value === "number" && Number.isSafeInteger(value) && value >= 0,
  );
}

export const STATUS_LABELS: Record<WorkflowStatus, string> = {
  IDLE: "계획 요청 전",
  PLANNING: "계획 수립 중",
  AWAITING_APPROVAL: "승인 대기",
  AWAITING_EXECUTION_RESULT: "실행 결과 대기",
  REJECTED: "반려됨",
  RESOLVED: "해결됨",
  EXECUTION_FAILED: "실행 실패",
  CANCELLED: "실행 취소됨",
  BLOCKED: "진행 보류",
};

const STEP_LABELS: Record<string, string> = {
  analyze: "요청 분석",
  plan: "실행 계획 수립",
  execute: "워커 에이전트 실행",
  evaluate: "근거 판정 및 승인 등록",
  respond: "응답 정리",
  execute_approved: "승인 조치 실행",
};

const TOOL_LABELS: Record<string, string> = {
  "log-analyzer-agent": "로그 분석 에이전트",
  "remediation-agent": "복구 계획 에이전트",
  list_rollback_candidates: "GitHub Deployments",
  get_release_metrics: "Grafana Cloud",
  get_change_freeze_windows: "Google Calendar",
};

export function stepLabel(stepName: string): string {
  return STEP_LABELS[stepName] ?? stepName;
}

export function toolLabel(toolName: string): string {
  return TOOL_LABELS[toolName] ?? toolName;
}

export function formatToolResultSummary(
  toolName: string | undefined,
  rawContent: string,
): { label: string; text: string } {
  const fallbackLabel = toolName ? toolLabel(toolName) : "도구 결과";
  try {
    const parsed = JSON.parse(rawContent) as Record<string, unknown>;
    if (typeof parsed.source === "string" && typeof parsed.summary === "string") {
      return { label: parsed.source, text: parsed.summary };
    }
    if (toolName === "log-analyzer-agent") {
      const chunks = Array.isArray(parsed.chunkIds) ? parsed.chunkIds.join(", ") : "";
      const count = typeof parsed.pulledCount === "number" ? parsed.pulledCount : 0;
      if (parsed.ok === false) {
        return { label: fallbackLabel, text: "로그 청크 조회에 실패했습니다." };
      }
      return {
        label: fallbackLabel,
        text: chunks ? `근거 청크 ${count}건(${chunks})을 확보했습니다.` : `근거 청크 ${count}건을 확인했습니다.`,
      };
    }
    if (toolName === "remediation-agent") {
      const verdict = typeof parsed.verdict === "string" ? parsed.verdict : "";
      const status = typeof parsed.status === "string" ? parsed.status : "";
      const detail = [status, verdict].filter(Boolean).join(" · ");
      return {
        label: fallbackLabel,
        text: detail ? `복구 계획 검토 완료 (${detail})` : "복구 계획 검토를 마쳤습니다.",
      };
    }
    if (parsed.ok === false) {
      const code = typeof parsed.errorCode === "string" ? ` (${parsed.errorCode})` : "";
      return { label: fallbackLabel, text: `조회에 실패했습니다${code}.` };
    }
    if (typeof parsed.summary === "string") {
      return { label: fallbackLabel, text: parsed.summary };
    }
    if (typeof parsed.elapsedMs === "number") {
      return { label: fallbackLabel, text: `조회를 마쳤습니다 (${Math.round(parsed.elapsedMs)}ms).` };
    }
    return { label: fallbackLabel, text: "조회를 마쳤습니다." };
  } catch {
    return { label: fallbackLabel, text: rawContent || "조회를 마쳤습니다." };
  }
}

function normalizeWorkWindow(raw: unknown): WorkWindow | null {
  if (!raw || typeof raw !== "object") return null;
  const w = raw as Record<string, unknown>;
  if (typeof w.start !== "string" || typeof w.end !== "string") return null;
  return { start: w.start, end: w.end };
}

function normalizePlan(raw: unknown): Plan | null {
  if (!raw || typeof raw !== "object") return null;
  const p = raw as Record<string, unknown>;
  const planId = typeof p.planId === "string" ? p.planId : "";
  const steps: PlanStep[] = Array.isArray(p.steps)
    ? p.steps
        .filter((item): item is Record<string, unknown> => Boolean(item && typeof item === "object"))
        .map((step, index) => ({
          stepNo: typeof step.stepNo === "number" ? step.stepNo : index + 1,
          description: typeof step.description === "string" ? step.description : "",
          condition: typeof step.condition === "string" ? step.condition : undefined,
          actionCommand:
            typeof step.actionCommand === "string" || step.actionCommand === null ? step.actionCommand : null,
          evidenceChunkIds: Array.isArray(step.evidenceChunkIds)
            ? step.evidenceChunkIds.filter((id): id is string => typeof id === "string")
            : [],
        }))
    : [];

  let evidence: EvidenceItem[];
  if (Array.isArray(p.evidence)) {
    evidence = p.evidence
      .filter((item): item is Record<string, unknown> => Boolean(item && typeof item === "object"))
      .map((item) => ({
        chunkId: typeof item.chunkId === "string" ? item.chunkId : "",
        summary: typeof item.summary === "string" ? item.summary : "",
        corpusType: typeof item.corpusType === "string" ? item.corpusType : undefined,
      }))
      .filter((item) => item.chunkId.length > 0);
  } else {
    const seen = new Set<string>();
    evidence = [];
    for (const step of steps) {
      for (const chunkId of step.evidenceChunkIds) {
        if (!seen.has(chunkId)) {
          seen.add(chunkId);
          evidence.push({ chunkId, summary: "저장된 복구 계획의 근거 청크" });
        }
      }
    }
  }

  return {
    planId,
    workWindow: normalizeWorkWindow(p.workWindow),
    steps,
    evidence,
    verificationCriteria: typeof p.verificationCriteria === "string" ? p.verificationCriteria : "",
  };
}

export function normalizeSnapshot(raw: unknown, fallbackIncidentCode: string): Snapshot | null {
  if (!raw || typeof raw !== "object") return null;
  const obj = raw as Record<string, unknown>;
  const incidentCode = typeof obj.incidentCode === "string" ? obj.incidentCode : fallbackIncidentCode;
  const workflowStatus = isWorkflowStatus(obj.workflowStatus) ? obj.workflowStatus : "IDLE";
  const plan = normalizePlan(obj.plan);
  const approval = obj.approval && typeof obj.approval === "object" ? (obj.approval as Approval) : null;
  const replanHistory = Array.isArray(obj.replanHistory)
    ? obj.replanHistory.filter((item): item is ReplanRecord => {
        if (!item || typeof item !== "object") return false;
        const r = item as Record<string, unknown>;
        return (
          typeof r.reason === "string" &&
          normalizeWorkWindow(r.previousWindow) !== null &&
          normalizeWorkWindow(r.nextWindow) !== null
        );
      })
    : [];
  const eventSeq = eventSequence(obj.eventSeq, obj.snapshotSeq);
  return { incidentCode, workflowStatus, plan, approval, replanHistory, eventSeq };
}

function isWorkflowStatus(value: unknown): value is WorkflowStatus {
  return typeof value === "string" && Object.hasOwn(STATUS_LABELS, value);
}

const OFFSETLESS_ISO_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?$/;

export function normalizeKstTimestamp(value: string): string {
  const trimmed = value.trim();
  if (OFFSETLESS_ISO_RE.test(trimmed)) {
    return `${trimmed}+09:00`;
  }
  return trimmed;
}

const kstClock = new Intl.DateTimeFormat("en-GB", {
  hour: "2-digit",
  minute: "2-digit",
  timeZone: "Asia/Seoul",
});

const kstDate = new Intl.DateTimeFormat("en-CA", {
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  timeZone: "Asia/Seoul",
});

export function dateLabelKst(value: string): string {
  const date = new Date(normalizeKstTimestamp(value));
  if (Number.isNaN(date.getTime())) return value.slice(0, 10);
  return kstDate.format(date);
}

export function timeLabel(value: string): string {
  const date = new Date(normalizeKstTimestamp(value));
  if (Number.isNaN(date.getTime())) return value;
  return kstClock.format(date);
}

export function windowLabel(window: WorkWindow): string {
  return `${timeLabel(window.start)}–${timeLabel(window.end)}`;
}
