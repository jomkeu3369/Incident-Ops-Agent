export const incidents = [
  {
    incidentCode: "INC-2026-009",
    title: "결제 서비스 502 오류 증가",
    serviceName: "payment-service",
    severity: "SEV1",
    startedAt: "2026-11-18T14:04:00+09:00",
    summary: "신규 배포 직후 결제 요청의 502 응답이 증가했습니다.",
    resolved: false,
  },
  {
    incidentCode: "INC-2026-008",
    title: "결제 승인 응답 지연",
    serviceName: "payment-service",
    severity: "SEV2",
    startedAt: "2026-11-18T11:16:00+09:00",
    summary: "결제 승인 응답 지연이 해소됐으며 복구 확인을 마쳤습니다.",
    resolved: true,
  },
] as const;

export type Incident = (typeof incidents)[number];
export type Decision = "APPROVED" | "REJECTED";

export type WorkWindow = { start: string; end: string };
const at = (time: string) => `2026-11-18T${time}:00+09:00`;

export const allowedWindow = { start: at("14:28"), end: at("14:50") };
export const freezeWindow = { start: at("14:30"), end: at("14:40") };
export const previousWindow = { start: at("14:28"), end: at("14:33") };
const nextWindow = { start: at("14:40"), end: at("14:45") };

export const demoPrompt = "이전 버전의 502 비율이 1% 미만이면 롤백을 제안하고, 14:28~14:50 사이에서 5분 작업 창을 찾아줘.";

const plan = {
  planId: "PLAN-2026-009",
  workWindow: nextWindow,
  steps: [
    {
      stepNo: 1,
      description: "결제 서비스를 이전 버전 v41로 롤백",
      condition: "후보 버전의 502 비율이 1% 미만",
      actionCommand: "kubectl rollout undo deployment/payment-service",
      evidenceChunkIds: ["LOG-901", "RUNBOOK-01"],
    },
  ],
  evidence: [
    { chunkId: "LOG-901", summary: "배포 직후 DB 커넥션 대기 시간 초과가 늘었습니다." },
    { chunkId: "RUNBOOK-01", summary: "배포 직후 커넥션 누수가 확인되면 이전 안정 버전으로 복구합니다." },
  ],
  verificationCriteria: "결제 오류율과 DB 커넥션 대기 시간이 정상 범위로 돌아오는지 확인합니다.",
};

export type Snapshot = {
  incidentCode: string;
  workflowStatus: "IDLE" | "PLANNING" | "AWAITING_APPROVAL" | "AWAITING_EXECUTION_RESULT" | "REJECTED" | "RESOLVED";
  plan: typeof plan | null;
  approval: {
    approvalId: string;
    planId: string;
    stepNo: number;
    decision: "PENDING" | Decision;
  } | null;
};

export type Replan = { reason: string; previousWindow: WorkWindow; nextWindow: WorkWindow };

export type DemoEvent =
  | { type: "RUN_STARTED" | "RUN_FINISHED"; threadId: string; runId: string }
  | { type: "STATE_SNAPSHOT"; snapshot: Snapshot }
  | { type: "TOOL_CALL_RESULT"; toolCallId: string; messageId: string; role: "tool"; content: string }
  | { type: "CUSTOM"; name: "replan"; value: Replan }
  | { type: "RUN_ERROR"; message: string };

export type RunInput = {
  threadId: string;
  runId: string;
  messages: { id: string; role: "user"; content: string }[];
  tools: [];
  context: [];
  forwardedProps: {
    incidentCode: string;
    incidentStartedAt?: string;
    approvalDecision?: { approvalId: string; decision: Decision };
  };
};

const pending = {
  approvalId: "APR-2026-009",
  planId: plan.planId,
  stepNo: 1,
  decision: "PENDING",
} as const;

export function initialSnapshot(incident: Incident): Snapshot {
  return {
    incidentCode: incident.incidentCode,
    workflowStatus: incident.resolved ? "RESOLVED" : "IDLE",
    plan: null,
    approval: null,
  };
}

const toolResults = [
  ["GitHub Deployments", "현재 배포 v42의 이전 성공 버전 v41을 찾았습니다."],
  ["Grafana Cloud", "v42의 502 비율은 18%, v41은 0.3%로 기준인 1% 미만입니다."],
  ["Google Calendar", "14:30–14:40은 변경 금지 구간입니다."],
] as const;

export async function* runDemo(input: RunInput, snapshot: Snapshot, signal: AbortSignal): AsyncGenerator<DemoEvent> {
  const { threadId, runId, forwardedProps } = input;
  yield { type: "RUN_STARTED", threadId, runId };

  const approval = forwardedProps.approvalDecision;
  if (approval) {
    if (snapshot.workflowStatus !== "AWAITING_APPROVAL" || snapshot.approval?.approvalId !== approval.approvalId) {
      yield { type: "RUN_ERROR", message: "현재 승인 요청을 다시 확인해 주세요." };
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 260));
    if (signal.aborted) return;
    yield {
      type: "STATE_SNAPSHOT",
      snapshot: {
        ...snapshot,
        workflowStatus: approval.decision === "APPROVED" ? "AWAITING_EXECUTION_RESULT" : "REJECTED",
        approval: { ...snapshot.approval, decision: approval.decision },
      },
    };
  } else {
    yield { type: "STATE_SNAPSHOT", snapshot: { ...snapshot, workflowStatus: "PLANNING" } };
    for (const [index, [source, summary]] of toolResults.entries()) {
      await new Promise((resolve) => setTimeout(resolve, 300));
      if (signal.aborted) return;
      yield {
        type: "TOOL_CALL_RESULT",
        toolCallId: `${runId}-tool-${index}`,
        messageId: `${runId}-result-${index}`,
        role: "tool",
        content: JSON.stringify({ source, summary }),
      };
    }
    yield { type: "CUSTOM", name: "replan", value: { reason: "기존 작업 창이 변경 금지 시간과 겹칩니다.", previousWindow, nextWindow } };
    yield { type: "STATE_SNAPSHOT", snapshot: { ...snapshot, workflowStatus: "AWAITING_APPROVAL", plan, approval: pending } };
  }
  yield { type: "RUN_FINISHED", threadId, runId };
}

const clock = new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit", timeZone: "Asia/Seoul" });
export const timeLabel = (value: string) => clock.format(new Date(value));
export const windowLabel = (window: WorkWindow) => `${timeLabel(window.start)}–${timeLabel(window.end)}`;
