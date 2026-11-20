import type { Plan, Snapshot, WorkWindow } from "./ag-ui-contract";
import type { IncidentListItem } from "./backend-api";

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

const at = (time: string) => `2026-11-18T${time}:00+09:00`;

export const allowedWindow: WorkWindow = { start: at("14:28"), end: at("14:50") };
export const freezeWindow: WorkWindow = { start: at("14:30"), end: at("14:40") };
export const previousWindow: WorkWindow = { start: at("14:28"), end: at("14:33") };
export const nextWindow: WorkWindow = { start: at("14:40"), end: at("14:45") };

export const demoPrompt =
  "이전 버전의 502 비율이 1% 미만이면 롤백을 제안하고, 14:28~14:50 사이에서 5분 작업 창을 찾아줘.";

export const demoPlan: Plan = {
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

export function initialSnapshot(incident: Pick<IncidentListItem, "incidentCode" | "resolved">): Snapshot {
  return {
    incidentCode: incident.incidentCode,
    workflowStatus: incident.resolved ? "RESOLVED" : "IDLE",
    plan: null,
    approval: null,
    replanHistory: [],
  };
}

export const demoIncidentList: IncidentListItem[] = incidents.map((item) => ({
  incidentCode: item.incidentCode,
  title: item.title,
  serviceName: item.serviceName,
  severity: item.severity,
  status: item.resolved ? "RESOLVED" : "OPEN",
  summary: item.summary,
  createdAt: item.startedAt,
  startedAt: item.startedAt,
  resolved: item.resolved,
}));
