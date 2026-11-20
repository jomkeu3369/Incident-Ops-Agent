import type { Approval, Plan, PlanStep, Snapshot, WorkflowStatus } from "./ag-ui-contract";

export type BackendIncident = {
  id: number;
  incidentCode: string;
  title: string;
  serviceName: string;
  severity: string;
  status: string;
  summary: string | null;
  createdAt: string | null;
  updatedAt: string | null;
};

export type BackendRemediationPlan = {
  id?: number;
  planId: string;
  incidentCode: string;
  steps: PlanStep[];
  verificationCriteria: string;
  createdBy: string;
  createdAt: string | null;
};

export type BackendRemediationApproval = {
  id?: number;
  approvalId: string;
  incidentCode?: string | null;
  planId: string | null;
  stepNo: number | null;
  actionCommand: string;
  proposedBy: string;
  approvedBy: string | null;
  decision: "PENDING" | "APPROVED" | "REJECTED";
  createdAt: string | null;
  decidedAt: string | null;
};

export type BackendRemediationExecution = {
  id?: number;
  approvalId: string;
  incidentCode?: string | null;
  actionCommand: string;
  executedBy: string;
  executionStatus: "SUCCESS" | "FAILED";
  executionOutput: string | null;
  verificationSummary: string | null;
  executedAt: string | null;
};

export type BackendPostMortem = {
  id?: number;
  incidentCode: string;
  approvalId?: string | null;
  rootCause: string;
  timelineMarkdown: string;
  remediationSummary: string;
  preventionItems: string;
  createdAt: string | null;
};

export type BackendIncidentDetail = {
  incident: BackendIncident;
  plans: BackendRemediationPlan[];
  approvals: BackendRemediationApproval[];
  executions: BackendRemediationExecution[];
  postMortem: BackendPostMortem | null;
};

export type IncidentListItem = {
  incidentCode: string;
  title: string;
  serviceName: string;
  severity: string;
  status: string;
  summary: string;
  createdAt: string | null;
  startedAt?: string | null;
  resolved: boolean;
};

export type BackendResult<T> =
  | { ok: true; data: T }
  | { ok: false; status: number; message: string };

function backendConfig(): { baseUrl: string; token: string } {
  const baseUrl = (process.env.BACKEND_INTERNAL_URL || "http://backend:8080").replace(/\/$/, "");
  const token = process.env.INTERNAL_SERVICE_TOKEN || "";
  return { baseUrl, token };
}

function mapHttpError(status: number, context: "list" | "detail"): string {
  if (status === 401 || status === 403) {
    return "백엔드 서버 인증 오류가 발생했습니다. 서버 설정을 확인해 주세요.";
  }
  if (status === 404 && context === "detail") {
    return "선택한 장애를 찾을 수 없습니다.";
  }
  return "백엔드 서버에서 장애 정보를 불러오지 못했습니다.";
}

export async function fetchIncidentsFromBackend(
  fetchImpl: typeof fetch = fetch,
): Promise<BackendResult<BackendIncident[]>> {
  const { baseUrl, token } = backendConfig();
  if (!token) {
    return { ok: false, status: 503, message: "백엔드 내부 토큰이 설정되지 않았습니다." };
  }
  try {
    const response = await fetchImpl(`${baseUrl}/api/incidents`, {
      method: "GET",
      headers: {
        Accept: "application/json",
        "X-Internal-Token": token,
      },
      cache: "no-store",
    });
    if (!response.ok) {
      return { ok: false, status: response.status, message: mapHttpError(response.status, "list") };
    }
    const data = (await response.json()) as BackendIncident[];
    return { ok: true, data: Array.isArray(data) ? data : [] };
  } catch {
    return { ok: false, status: 503, message: "백엔드 서버에 연결할 수 없습니다." };
  }
}

export async function fetchIncidentDetailFromBackend(
  incidentCode: string,
  fetchImpl: typeof fetch = fetch,
): Promise<BackendResult<BackendIncidentDetail>> {
  const { baseUrl, token } = backendConfig();
  if (!token) {
    return { ok: false, status: 503, message: "백엔드 내부 토큰이 설정되지 않았습니다." };
  }
  try {
    const response = await fetchImpl(`${baseUrl}/api/incidents/${encodeURIComponent(incidentCode)}`, {
      method: "GET",
      headers: {
        Accept: "application/json",
        "X-Internal-Token": token,
      },
      cache: "no-store",
    });
    if (!response.ok) {
      return { ok: false, status: response.status, message: mapHttpError(response.status, "detail") };
    }
    const raw = (await response.json()) as Partial<BackendIncidentDetail>;
    if (!raw || !raw.incident) {
      return { ok: false, status: 502, message: "백엔드 장애 상세 응답 형식이 올바르지 않습니다." };
    }
    return {
      ok: true,
      data: {
        incident: raw.incident,
        plans: Array.isArray(raw.plans) ? raw.plans : [],
        approvals: Array.isArray(raw.approvals) ? raw.approvals : [],
        executions: Array.isArray(raw.executions) ? raw.executions : [],
        postMortem: raw.postMortem ?? null,
      },
    };
  } catch {
    return { ok: false, status: 503, message: "백엔드 서버에 연결할 수 없습니다." };
  }
}

export function toIncidentListItem(incident: BackendIncident): IncidentListItem {
  return {
    incidentCode: incident.incidentCode,
    title: incident.title,
    serviceName: incident.serviceName,
    severity: incident.severity,
    status: incident.status,
    summary: incident.summary ?? "",
    createdAt: incident.createdAt,
    startedAt: null,
    resolved: incident.status === "RESOLVED",
  };
}

function toFrontendPlan(plan: BackendRemediationPlan): Plan {
  const uniqueChunkIds = Array.from(
    new Set(plan.steps.flatMap((step) => step.evidenceChunkIds ?? [])),
  );
  return {
    planId: plan.planId,
    workWindow: null,
    steps: plan.steps,
    evidence: uniqueChunkIds.map((chunkId) => ({
      chunkId,
      summary: "백엔드에 저장된 근거 청크 ID입니다.",
    })),
    verificationCriteria: plan.verificationCriteria,
  };
}

export function hydrateSnapshotFromDetail(detail: BackendIncidentDetail): Snapshot {
  const { incident, plans, approvals, executions } = detail;
  const latestApproval = approvals.length > 0 ? approvals[approvals.length - 1] : null;
  const matchedPlan =
    (latestApproval?.planId ? plans.find((p) => p.planId === latestApproval.planId) : undefined) ??
    (plans.length > 0 ? plans[plans.length - 1] : null);

  const matchedExecution = latestApproval
    ? executions.find((exec) => exec.approvalId === latestApproval.approvalId) ?? null
    : null;

  let workflowStatus: WorkflowStatus = "IDLE";
  if (incident.status === "RESOLVED") {
    workflowStatus = "RESOLVED";
  } else if (latestApproval) {
    if (latestApproval.decision === "PENDING") {
      workflowStatus = "AWAITING_APPROVAL";
    } else if (latestApproval.decision === "APPROVED") {
      if (matchedExecution?.executionStatus === "FAILED") {
        workflowStatus = "EXECUTION_FAILED";
      } else if (matchedExecution?.executionStatus === "SUCCESS") {
        workflowStatus = "RESOLVED";
      } else {
        workflowStatus = "AWAITING_EXECUTION_RESULT";
      }
    } else if (latestApproval.decision === "REJECTED") {
      workflowStatus = "REJECTED";
    }
  }

  const approval: Approval | null =
    latestApproval && latestApproval.planId && latestApproval.stepNo !== null
      ? {
          approvalId: latestApproval.approvalId,
          planId: latestApproval.planId,
          stepNo: latestApproval.stepNo,
          decision: latestApproval.decision,
        }
      : null;

  return {
    incidentCode: incident.incidentCode,
    workflowStatus,
    plan: matchedPlan ? toFrontendPlan(matchedPlan) : null,
    approval,
    replanHistory: [],
  };
}
