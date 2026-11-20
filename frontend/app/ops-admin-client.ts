import { resolveAgUiUrl, type AgUiMode } from "./ag-ui-client";

export type ManagedPlanItem = {
  planId: string;
  incidentCode: string;
  title: string;
  serviceName: string;
  planDate: string;
  startTime: string;
  endTime: string;
  actionCommand: string | null;
  evidenceChunkIds: string[];
  notes: string;
  createdAt: string;
  updatedAt: string;
};

export type ManagedPlanCreateInput = {
  planId?: string;
  incidentCode: string;
  title: string;
  serviceName: string;
  planDate: string;
  startTime: string;
  endTime: string;
  actionCommand?: string | null;
  evidenceChunkIds?: string[];
  notes?: string;
};

export type ManagedPlanUpdateInput = {
  title?: string;
  planDate?: string;
  startTime?: string;
  endTime?: string;
  actionCommand?: string | null;
  notes?: string;
};

export type ManagedPlanMutationResponse = {
  plan: ManagedPlanItem;
  overlappingPlans: ManagedPlanItem[];
  warning: string | null;
};

export type SessionSummary = {
  sessionId: string;
  eventCount: number;
  startedAt: string | null;
  lastEventAt: string | null;
};

export type SessionLogEvent = {
  sessionId?: string;
  eventType: string;
  timestamp?: string;
  payload?: Record<string, unknown>;
};

export type SessionLogsResponse = {
  sessionId: string;
  events: SessionLogEvent[];
};

export type AnomalyFindingItem = {
  rule: "REPEATED_IDENTICAL_CALL" | "EXCESSIVE_REPLAN" | "UNPLANNED_TOOL_CALL";
  severity: "WARN" | "HIGH";
  summary: string;
  evidence: Record<string, unknown>;
};

export type SessionAnomalyResponse = {
  sessionId: string;
  status: "NORMAL" | "ANOMALY" | "UNDETERMINED";
  findings: AnomalyFindingItem[];
  diagnosis: string | null;
  llmCalls: number;
  evaluatedEventCount: number;
};

export type ApiCallResult<T> = { ok: true; data: T } | { ok: false; status: number; message: string };

export function resolveAdminBaseUrl(mode: AgUiMode): string {
  return resolveAgUiUrl(mode).replace(/\/$/, "");
}

async function parseErrorMessage(response: Response, fallback: string): Promise<string> {
  try {
    const body = (await response.json()) as Record<string, unknown>;
    if (typeof body.detail === "string" && body.detail.trim()) return body.detail.trim();
    if (typeof body.message === "string" && body.message.trim()) return body.message.trim();
  } catch {
    // ignore non-JSON body
  }
  return fallback;
}

async function requestJson<T>(
  url: string,
  init: RequestInit,
  fallbackError: string,
  fetchImpl: typeof fetch = fetch,
): Promise<ApiCallResult<T>> {
  try {
    const response = await fetchImpl(url, { ...init, cache: "no-store" });
    if (!response.ok) {
      return {
        ok: false,
        status: response.status,
        message: await parseErrorMessage(response, fallbackError),
      };
    }
    if (response.status === 204) {
      return { ok: true, data: undefined as T };
    }
    const data = (await response.json()) as T;
    return { ok: true, data };
  } catch {
    return { ok: false, status: 503, message: "서버에 연결할 수 없습니다." };
  }
}

export function listManagedPlans(
  baseUrl: string,
  fetchImpl: typeof fetch = fetch,
): Promise<ApiCallResult<ManagedPlanItem[]>> {
  return requestJson<ManagedPlanItem[]>(
    `${baseUrl}/plans`,
    { method: "GET", headers: { Accept: "application/json" } },
    "계획 목록을 불러오지 못했습니다.",
    fetchImpl,
  );
}

export function createManagedPlan(
  baseUrl: string,
  body: ManagedPlanCreateInput,
  fetchImpl: typeof fetch = fetch,
): Promise<ApiCallResult<ManagedPlanMutationResponse>> {
  return requestJson<ManagedPlanMutationResponse>(
    `${baseUrl}/plans`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(body),
    },
    "계획을 저장하지 못했습니다.",
    fetchImpl,
  );
}

export function updateManagedPlan(
  baseUrl: string,
  planId: string,
  body: ManagedPlanUpdateInput,
  fetchImpl: typeof fetch = fetch,
): Promise<ApiCallResult<ManagedPlanMutationResponse>> {
  return requestJson<ManagedPlanMutationResponse>(
    `${baseUrl}/plans/${encodeURIComponent(planId)}`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(body),
    },
    "계획을 수정하지 못했습니다.",
    fetchImpl,
  );
}

export function deleteManagedPlan(
  baseUrl: string,
  planId: string,
  fetchImpl: typeof fetch = fetch,
): Promise<ApiCallResult<void>> {
  return requestJson<void>(
    `${baseUrl}/plans/${encodeURIComponent(planId)}`,
    { method: "DELETE" },
    "계획을 삭제하지 못했습니다.",
    fetchImpl,
  );
}

export function listLogSessions(
  baseUrl: string,
  fetchImpl: typeof fetch = fetch,
): Promise<ApiCallResult<SessionSummary[]>> {
  return requestJson<SessionSummary[]>(
    `${baseUrl}/sessions`,
    { method: "GET", headers: { Accept: "application/json" } },
    "실행 로그 세션 목록을 불러오지 못했습니다.",
    fetchImpl,
  );
}

export function getLogSessionDetail(
  baseUrl: string,
  sessionId: string,
  fetchImpl: typeof fetch = fetch,
): Promise<ApiCallResult<SessionLogsResponse>> {
  return requestJson<SessionLogsResponse>(
    `${baseUrl}/sessions/${encodeURIComponent(sessionId)}`,
    { method: "GET", headers: { Accept: "application/json" } },
    "실행 로그 상세를 불러오지 못했습니다.",
    fetchImpl,
  );
}

export function deleteLogSession(
  baseUrl: string,
  sessionId: string,
  fetchImpl: typeof fetch = fetch,
): Promise<ApiCallResult<void>> {
  return requestJson<void>(
    `${baseUrl}/sessions/${encodeURIComponent(sessionId)}`,
    { method: "DELETE" },
    "실행 로그를 삭제하지 못했습니다.",
    fetchImpl,
  );
}

export function analyzeLogSessionAnomalies(
  baseUrl: string,
  sessionId: string,
  fetchImpl: typeof fetch = fetch,
): Promise<ApiCallResult<SessionAnomalyResponse>> {
  return requestJson<SessionAnomalyResponse>(
    `${baseUrl}/sessions/${encodeURIComponent(sessionId)}/anomalies`,
    { method: "GET", headers: { Accept: "application/json" } },
    "이상 징후 분석에 실패했습니다.",
    fetchImpl,
  );
}
