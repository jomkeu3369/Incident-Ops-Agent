"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  Activity,
  ArrowUpRight,
  CalendarClock,
  Check,
  ChevronRight,
  Clock3,
  FileText,
  ListChecks,
  Radio,
  RefreshCw,
  RotateCcw,
  Send,
  X,
} from "lucide-react";
import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { resolveAgUiUrl, runAgUiRequest, type AgUiMode } from "./ag-ui-client";
import {
  STATUS_LABELS,
  dateLabelKst,
  timeLabel,
  windowLabel,
  type Decision,
  type ReplanRecord,
  type WorkWindow,
} from "./ag-ui-contract";
import type { BackendIncidentDetail, IncidentListItem } from "./backend-api";
import { allowedWindow, demoPrompt, freezeWindow, previousWindow } from "./demo";
import { createManagedPlan, resolveAdminBaseUrl } from "./ops-admin-client";
import { PlansPanel } from "./plans-panel";
import { SessionsPanel } from "./sessions-panel";
import {
  canResumeApprovedExecution,
  canSendPrompt,
  canSubmitDecision,
  createInitialState,
  workspaceReducer,
} from "./workspace-state";
import styles from "./workspace.module.css";

function createClientId(): string {
  return (
    crypto.randomUUID?.() ??
    Array.from(crypto.getRandomValues(new Uint8Array(16)), (byte) => byte.toString(16).padStart(2, "0")).join("")
  );
}

function threadStorageKey(mode: AgUiMode, incidentCode: string): string {
  return `incident-ops:thread:${mode}:${incidentCode}`;
}

function readStoredThreadId(mode: AgUiMode, incidentCode: string): string | null {
  try {
    return window.sessionStorage.getItem(threadStorageKey(mode, incidentCode));
  } catch {
    return null;
  }
}

function writeStoredThreadId(mode: AgUiMode, incidentCode: string, threadId: string | null): void {
  try {
    const key = threadStorageKey(mode, incidentCode);
    if (threadId) {
      window.sessionStorage.setItem(key, threadId);
    } else {
      window.sessionStorage.removeItem(key);
    }
  } catch {
    // sessionStorage unavailable
  }
}

export type WorkspaceView = "workspace" | "plans" | "sessions";

type WorkspaceProps = {
  incident: IncidentListItem | null;
  incidents: IncidentListItem[];
  initialDetail: BackendIncidentDetail | null;
  initialMode: AgUiMode;
  initialView: WorkspaceView;
  backendError: string | null;
};

export function Workspace({ incident, incidents, initialDetail, initialMode, initialView, backendError }: WorkspaceProps) {
  const router = useRouter();
  const mode = initialMode;
  const [view, setView] = useState<WorkspaceView>(initialView);

  function switchView(nextView: WorkspaceView) {
    if (nextView === view) return;
    setView(nextView);
    try {
      const url = new URL(window.location.href);
      if (nextView === "workspace") url.searchParams.delete("view");
      else url.searchParams.set("view", nextView);
      window.history.replaceState(null, "", `${url.pathname}${url.search}${url.hash}`);
    } catch {
      // ignore non-browser contexts
    }
  }

  function switchMode(nextMode: AgUiMode) {
    if (nextMode === mode) return;
    const viewSuffix = view !== "workspace" ? `&view=${view}` : "";
    router.push(`/?mode=${nextMode}${viewSuffix}`);
  }

  if (!incident) {
    return (
      <>
        <header className={styles.topbar}>
          <div className={styles.topInner}>
            <div className={styles.brand}>
              <Activity size={20} aria-hidden="true" /> Incident Ops
            </div>
            <nav className={styles.viewTabs} aria-label="화면 전환">
              <button type="button" className={styles.navTab} aria-pressed={view === "workspace"} onClick={() => switchView("workspace")}>
                장애 대응
              </button>
              <button type="button" className={styles.navTab} aria-pressed={view === "plans"} onClick={() => switchView("plans")}>
                계획 관리
              </button>
              <button type="button" className={styles.navTab} aria-pressed={view === "sessions"} onClick={() => switchView("sessions")}>
                실행 로그
              </button>
            </nav>
            <div className={styles.topActions}>
              <div className={styles.modeSwitch} role="group" aria-label="AG-UI 연결 대상">
                <button
                  type="button"
                  className={styles.modeButton}
                  aria-pressed={mode === "live"}
                  onClick={() => switchMode("live")}
                >
                  <Radio size={13} aria-hidden="true" /> AG-UI
                </button>
                <button
                  type="button"
                  className={styles.modeButton}
                  aria-pressed={mode === "mock"}
                  onClick={() => switchMode("mock")}
                >
                  모의 SSE
                </button>
              </div>
            </div>
          </div>
        </header>
        <main className={styles.main}>
          {view === "plans" ? (
            <PlansPanel mode={mode} defaultIncidentCode="INC-2026-001" defaultServiceName="payment-service" />
          ) : view === "sessions" ? (
            <SessionsPanel mode={mode} />
          ) : (
            <section className={styles.emptyState}>
              <h1>등록된 장애가 없습니다</h1>
              <p>
                {backendError ||
                  "현재 조회된 장애 티켓이 없습니다. 백엔드에 장애를 등록하거나 모의 SSE 모드로 전환해 시나리오를 확인할 수 있습니다."}
              </p>
            </section>
          )}
        </main>
      </>
    );
  }

  return (
    <ActiveWorkspace
      incident={incident}
      incidents={incidents}
      initialDetail={initialDetail}
      mode={mode}
      view={view}
      backendError={backendError}
      onSwitchMode={switchMode}
      onSwitchView={switchView}
    />
  );
}

function ActiveWorkspace({
  incident,
  incidents,
  initialDetail,
  mode,
  view,
  backendError,
  onSwitchMode,
  onSwitchView,
}: {
  incident: IncidentListItem;
  incidents: IncidentListItem[];
  initialDetail: BackendIncidentDetail | null;
  mode: AgUiMode;
  view: WorkspaceView;
  backendError: string | null;
  onSwitchMode: (nextMode: AgUiMode) => void;
  onSwitchView: (nextView: WorkspaceView) => void;
}) {
  const [prompt, setPrompt] = useState(demoPrompt);
  const [incidentStartedAt, setIncidentStartedAt] = useState(incident.startedAt ?? "");
  const [planSaveNotice, setPlanSaveNotice] = useState<string | null>(null);
  const [savingPlan, setSavingPlan] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [state, dispatch] = useReducer(workspaceReducer, undefined, () =>
    createInitialState(incident, initialDetail),
  );
  const activeRun = useRef<AbortController | null>(null);
  const threadIdRef = useRef<string | null>(null);
  const planningRequest = useRef<{ content: string; incidentStartedAt?: string; runId?: string } | null>(null);

  const {
    snapshot, executions, postMortem, entries, replan, busy, activeStep, restoring, error, needsResync,
    lastEventSeq, a2aProgress,
  } = state;
  const approvedStep =
    snapshot.plan && snapshot.approval && snapshot.plan.planId === snapshot.approval.planId
      ? (snapshot.plan.steps ?? []).find((step) => step.stepNo === snapshot.approval?.stepNo)
      : undefined;

  const syncBackendDetail = useCallback(async () => {
    if (mode !== "live") return;
    try {
      const response = await fetch(`/bff/incidents/${encodeURIComponent(incident.incidentCode)}`, {
        method: "GET",
        cache: "no-store",
      });
      if (!response.ok) return;
      const detail = (await response.json()) as BackendIncidentDetail;
      dispatch({ type: "BACKEND_DETAIL_SYNCED", detail });
    } catch {
      // keep existing UI state if refresh fails transiently
    }
  }, [incident.incidentCode, mode]);

  const restoreSession = useCallback(
    async (targetMode: AgUiMode, storedThreadId: string, cursor = 0) => {
      if (activeRun.current) return;
      const controller = new AbortController();
      activeRun.current = controller;
      threadIdRef.current = storedThreadId;
      dispatch({ type: "RESTORE_STARTED" });

      try {
        await runAgUiRequest({
          url: resolveAgUiUrl(targetMode),
          threadId: storedThreadId,
          runId: createClientId(),
          forwardedProps: {
            incidentCode: incident.incidentCode,
            restore: true,
            lastEventSeq: cursor,
          },
          signal: controller.signal,
          dispatch,
        });
        if (!controller.signal.aborted && targetMode === "live") {
          await syncBackendDetail();
        }
      } finally {
        if (activeRun.current === controller) {
          activeRun.current = null;
        }
      }
    },
    [incident.incidentCode, syncBackendDetail],
  );

  useEffect(() => {
    const saved = readStoredThreadId(mode, incident.incidentCode);
    if (saved && !(mode === "mock" && incident.resolved)) {
      void restoreSession(mode, saved);
    }
    return () => {
      activeRun.current?.abort();
      activeRun.current = null;
    };
  }, [incident.incidentCode, incident.resolved, mode, restoreSession]);

  function resetSession() {
    activeRun.current?.abort();
    activeRun.current = null;
    threadIdRef.current = null;
    planningRequest.current = null;
    writeStoredThreadId(mode, incident.incidentCode, null);
    setPrompt(demoPrompt);
    dispatch({ type: "RESET", incident, detail: initialDetail });
  }

  function handleSwitchMode(nextMode: AgUiMode) {
    if (nextMode === mode) return;
    activeRun.current?.abort();
    activeRun.current = null;
    threadIdRef.current = null;
    onSwitchMode(nextMode);
  }

  async function submitPrompt(resume = false) {
    const startedAt = incidentStartedAt.trim() || undefined;
    const request = resume ? planningRequest.current : { content: prompt.trim(), incidentStartedAt: startedAt };
    if (!request?.content || activeRun.current || snapshot.workflowStatus === "RESOLVED") return;
    const trimmed = request.content;
    if (!resume && !canSendPrompt(snapshot.workflowStatus, incident.resolved)) return;

    const controller = new AbortController();
    activeRun.current = controller;

    try {
      const tid = threadIdRef.current ?? createClientId();
      threadIdRef.current = tid;
      writeStoredThreadId(mode, incident.incidentCode, tid);
      const runId = createClientId();
      // 재개가 다시 끊겨도 같은 원본 실행을 가리키도록 처음 runId를 유지한다
      planningRequest.current = { ...request, runId: request.runId ?? runId };

      dispatch({
        type: "REQUEST",
        entry: {
          id: runId,
          label: "운영자",
          text: resume ? "중단된 계획 수립을 재개합니다." : trimmed,
          kind: "operator",
        },
      });

      const outcome = await runAgUiRequest({
        url: resolveAgUiUrl(mode),
        threadId: tid,
        runId,
        userMessage: { id: runId, content: trimmed },
        forwardedProps: {
          incidentCode: incident.incidentCode,
          incidentStartedAt: request.incidentStartedAt,
          ...(resume ? { resume: true, resumeRunId: request.runId } : {}),
          lastEventSeq,
        },
        signal: controller.signal,
        dispatch: (action) => {
          if (activeRun.current === controller) dispatch(action);
        },
      });

      if (outcome.ok && activeRun.current === controller && !controller.signal.aborted) {
        planningRequest.current = null;
        setPrompt("");
        await syncBackendDetail();
      }
    } finally {
      if (activeRun.current === controller) {
        activeRun.current = null;
      }
    }
  }

  async function submitDecision(decision: Decision) {
    if (activeRun.current) return;
    if (!snapshot.approval || !canSubmitDecision(snapshot, decision)) {
      return;
    }

    const resuming = canResumeApprovedExecution(snapshot);
    planningRequest.current = null;
    const controller = new AbortController();
    activeRun.current = controller;

    try {
      const tid = threadIdRef.current ?? createClientId();
      threadIdRef.current = tid;
      writeStoredThreadId(mode, incident.incidentCode, tid);
      const runId = createClientId();

      dispatch({
        type: "REQUEST",
        entry: {
          id: runId,
          label: "운영자",
          text: resuming
            ? "승인된 복구 실행을 재개합니다."
            : decision === "APPROVED"
              ? "복구 계획을 승인합니다."
              : "복구 계획을 반려합니다.",
          kind: "operator",
        },
      });

      await runAgUiRequest({
        url: resolveAgUiUrl(mode),
        threadId: tid,
        runId,
        forwardedProps: {
          incidentCode: incident.incidentCode,
          lastEventSeq,
          approvalDecision: {
            approvalId: snapshot.approval.approvalId,
            decision,
          },
        },
        signal: controller.signal,
        dispatch: (action) => {
          if (activeRun.current === controller) dispatch(action);
        },
      });

      if (activeRun.current === controller && !controller.signal.aborted) {
        await syncBackendDetail();
      }
    } finally {
      if (activeRun.current === controller) {
        activeRun.current = null;
      }
    }
  }

  async function handleResync() {
    const tid = threadIdRef.current ?? readStoredThreadId(mode, incident.incidentCode);
    if (tid) {
      await restoreSession(mode, tid, lastEventSeq);
    } else {
      await syncBackendDetail();
    }
  }

  async function cancelActiveRun() {
    const previous = activeRun.current;
    const tid = threadIdRef.current;
    if (!previous || !tid || restoring || cancelling || mode !== "live") return;
    const controller = new AbortController();
    activeRun.current = controller;
    setCancelling(true);
    try {
      const outcome = await runAgUiRequest({
        url: resolveAgUiUrl(mode),
        threadId: tid,
        runId: createClientId(),
        forwardedProps: { incidentCode: incident.incidentCode, explicitCancel: true, lastEventSeq },
        signal: controller.signal,
        dispatch,
      });
      if (outcome.ok && !controller.signal.aborted) {
        planningRequest.current = null;
        await syncBackendDetail();
      }
    } finally {
      previous.abort();
      if (activeRun.current === controller) activeRun.current = null;
      setCancelling(false);
    }
  }

  async function saveCurrentPlanToManager() {
    if (!snapshot.plan || savingPlan) return;
    setSavingPlan(true);
    setPlanSaveNotice(null);
    const activeWin = snapshot.plan.workWindow ?? replan?.nextWindow ?? previousWindow;
    const planDate = dateLabelKst(activeWin.start);
    const startTime = timeLabel(activeWin.start);
    const endTime = timeLabel(activeWin.end);
    const res = await createManagedPlan(resolveAdminBaseUrl(mode), {
      incidentCode: incident.incidentCode,
      title: approvedStep?.description || `${incident.serviceName} 복구 계획`,
      serviceName: incident.serviceName,
      planDate,
      startTime,
      endTime,
      actionCommand: approvedStep?.actionCommand ?? null,
      evidenceChunkIds: (snapshot.plan.evidence ?? []).map((e) => e.chunkId),
      notes: snapshot.plan.verificationCriteria,
    });
    if (res.ok) {
      setPlanSaveNotice(res.data.warning ? `저장 완료 · ${res.data.warning}` : "계획 관리에 저장했습니다.");
    } else {
      setPlanSaveNotice(res.message);
    }
    setSavingPlan(false);
  }

  const isResolved = incident.resolved || snapshot.workflowStatus === "RESOLVED";
  const showComposer = canSendPrompt(snapshot.workflowStatus, isResolved);
  const hasSession = snapshot.workflowStatus !== "IDLE" || Boolean(error) || Boolean(threadIdRef.current);
  const displayTimestamp = incident.startedAt || incident.createdAt;

  return (
    <>
      <header className={styles.topbar}>
        <div className={styles.topInner}>
          <div className={styles.brand}>
            <Activity size={20} aria-hidden="true" /> Incident Ops
          </div>
          <nav className={styles.viewTabs} aria-label="화면 전환">
            <button type="button" className={styles.navTab} aria-pressed={view === "workspace"} onClick={() => onSwitchView("workspace")}>
              장애 대응
            </button>
            <button type="button" className={styles.navTab} aria-pressed={view === "plans"} onClick={() => onSwitchView("plans")}>
              계획 관리
            </button>
            <button type="button" className={styles.navTab} aria-pressed={view === "sessions"} onClick={() => onSwitchView("sessions")}>
              실행 로그
            </button>
          </nav>
          <div className={styles.topActions}>
            <div className={styles.modeSwitch} role="group" aria-label="AG-UI 연결 대상">
              <button
                type="button"
                className={styles.modeButton}
                aria-pressed={mode === "live"}
                onClick={() => handleSwitchMode("live")}
              >
                <Radio size={13} aria-hidden="true" /> AG-UI
              </button>
              <button
                type="button"
                className={styles.modeButton}
                aria-pressed={mode === "mock"}
                onClick={() => handleSwitchMode("mock")}
              >
                모의 SSE
              </button>
            </div>
            {view === "workspace" && (
              <button
                type="button"
                className={styles.secondaryButton}
                onClick={resetSession}
                disabled={isResolved || !hasSession}
              >
                <RotateCcw size={14} aria-hidden="true" /> 새 대화
              </button>
            )}
          </div>
        </div>
      </header>

      <main className={styles.main}>
        {view === "plans" ? (
          <PlansPanel
            mode={mode}
            defaultIncidentCode={incident.incidentCode}
            defaultServiceName={incident.serviceName}
          />
        ) : view === "sessions" ? (
          <SessionsPanel mode={mode} />
        ) : (
          <>
        {backendError && (
          <div className={styles.bannerError} role="alert">
            <span>{backendError}</span>
            {mode === "live" && (
              <button type="button" className={styles.secondaryButton} onClick={() => handleSwitchMode("mock")}>
                모의 SSE로 전환
              </button>
            )}
          </div>
        )}

        <div className={styles.incidentHeader}>
          <div>
            <div className={styles.incidentMeta}>
              <span className={styles.mono}>{incident.incidentCode}</span>
              <span>{incident.serviceName}</span>
            </div>
            <h1>{incident.title}</h1>
            <p>{incident.summary}</p>
          </div>
          <dl className={styles.headerFacts}>
            <div>
              <dt>심각도</dt>
              <dd className={incident.severity === "SEV1" ? styles.severity : undefined}>{incident.severity}</dd>
            </div>
            <div>
              <dt>{incident.startedAt ? "발생 시각" : "접수 시각"}</dt>
              <dd className={styles.mono}>{displayTimestamp ? `${timeLabel(displayTimestamp)} KST` : "—"}</dd>
            </div>
          </dl>
        </div>

        <div className={styles.workspace}>
          <nav className={styles.incidents} aria-labelledby="incidents-label">
            <h2 id="incidents-label">장애 목록</h2>
            <ul>
              {incidents.map((item) => {
                const href = `/?incident=${encodeURIComponent(item.incidentCode)}&mode=${mode}`;
                return (
                  <li key={item.incidentCode}>
                    <Link
                      href={href}
                      aria-current={item.incidentCode === incident.incidentCode ? "page" : undefined}
                      className={styles.incidentLink}
                    >
                      <span className={styles.incidentLinkTop}>
                        <span className={styles.mono}>{item.incidentCode}</span>
                        <span>{item.severity}</span>
                      </span>
                      <strong>{item.title}</strong>
                      <span className={styles.incidentLinkBottom}>
                        {item.resolved ? "해결됨" : item.status === "WAITING_APPROVAL" ? "승인 대기" : "대응 중"}
                        <ChevronRight size={14} aria-hidden="true" />
                      </span>
                    </Link>
                  </li>
                );
              })}
            </ul>
          </nav>

          <section className={styles.activity} aria-labelledby="activity-label">
            <div className={styles.panelHead}>
              <h2 id="activity-label">
                <ListChecks size={18} aria-hidden="true" /> 작업 기록
              </h2>
              {snapshot.approval?.decision === "PENDING" && (
                <a href="#plan">
                  계획 검토 <ArrowUpRight size={14} aria-hidden="true" />
                </a>
              )}
            </div>

            <ol className={styles.trail} aria-label="작업 타임라인" aria-live="polite">
              {entries.map((entry) => (
                <li key={entry.id} data-kind={entry.kind}>
                  <strong>{entry.label}</strong>
                  <p>{entry.text}</p>
                </li>
              ))}
            </ol>

            {(busy || cancelling) && (
              <>
                <p className={styles.progress} role="status">
                  {cancelling ? "실행 취소 요청 중" : restoring ? "이전 대화 상태를 복원하는 중"
                    : a2aProgress?.stage === "searching_index" ? "로그 색인 검색 중"
                    : a2aProgress?.stage === "pulling_chunks" ? "근거 청크 인출 중"
                    : activeStep ? `${activeStep} 진행 중` : "AG-UI 응답을 받는 중"}
                </p>
                {mode === "live" && !restoring && !cancelling && snapshot.workflowStatus !== "RESOLVED" && (
                  <button type="button" className={styles.secondaryButton} onClick={() => void cancelActiveRun()}>
                    <X size={14} aria-hidden="true" /> 실행 취소
                  </button>
                )}
              </>
            )}

            {error && (
              <div className={styles.errorBox} role="alert">
                <p className={styles.error}>{error}</p>
                {needsResync && (
                  <button type="button" className={styles.secondaryButton} disabled={busy} onClick={() => void handleResync()}>
                    <RefreshCw size={14} aria-hidden="true" /> 상태 다시 조회
                  </button>
                )}
                {needsResync && mode === "live" && planningRequest.current && snapshot.workflowStatus !== "RESOLVED" && (
                  <button type="button" className={styles.secondaryButton} disabled={busy} onClick={() => void submitPrompt(true)}>
                    <RefreshCw size={14} aria-hidden="true" /> 계획 수립 재개
                  </button>
                )}
              </div>
            )}

            {showComposer && (
              <form
                className={styles.composer}
                onSubmit={(event) => {
                  event.preventDefault();
                  void submitPrompt();
                }}
              >
                <label htmlFor="prompt-input" className={styles.composerLabel}>
                  복구 요청
                </label>
                <textarea
                  id="prompt-input"
                  className={styles.promptInput}
                  rows={2}
                  value={prompt}
                  disabled={busy}
                  onChange={(event) => setPrompt(event.target.value)}
                  placeholder="복구 조건이나 작업 시간 범위를 입력하세요"
                />
                {mode === "live" && (
                  <div className={styles.startedAtRow}>
                    <label htmlFor="started-at-input">장애 시작 시각 (RFC 3339, 선택)</label>
                    <input
                      id="started-at-input"
                      type="text"
                      className={styles.startedAtInput}
                      value={incidentStartedAt}
                      disabled={busy}
                      onChange={(event) => setIncidentStartedAt(event.target.value)}
                      placeholder="예: 2026-11-18T14:04:00+09:00"
                    />
                  </div>
                )}
                <div className={styles.composerFooter}>
                  <button className={styles.primaryButton} type="submit" disabled={busy || !prompt.trim()}>
                    <Send size={14} aria-hidden="true" /> 계획 요청
                  </button>
                </div>
              </form>
            )}
          </section>

          <aside className={styles.plan} id="plan" aria-labelledby="plan-label">
            <div className={styles.panelHead}>
              <h2 id="plan-label">
                <CalendarClock size={18} aria-hidden="true" /> 복구 계획
              </h2>
              <span className={styles.status} data-state={snapshot.workflowStatus} role="status">
                {STATUS_LABELS[snapshot.workflowStatus]}
              </span>
            </div>

            <div className={styles.planBody}>
              {!isResolved && <Schedule replan={replan} workWindow={snapshot.plan?.workWindow ?? null} />}

              {snapshot.plan && snapshot.approval ? (
                <div className={styles.approval}>
                  <div className={styles.approvalHead}>
                    <h3>승인 대상</h3>
                    <span className={styles.mono}>{snapshot.approval.approvalId}</span>
                  </div>
                  <p className={styles.actionTitle}>{approvedStep?.description}</p>
                  <details>
                    <summary>근거 및 복구 확인 기준</summary>
                    <ul className={styles.evidence}>
                      {(snapshot.plan.evidence ?? []).map((item) => (
                        <li key={item.chunkId}>
                          <code>{item.chunkId}</code> {item.summary}
                        </li>
                      ))}
                    </ul>
                    {approvedStep?.actionCommand && (
                      <p className={styles.command}>
                        <code>{approvedStep.actionCommand}</code>
                      </p>
                    )}
                    <p>{snapshot.plan.verificationCriteria}</p>
                  </details>
                  {snapshot.workflowStatus === "AWAITING_APPROVAL" &&
                  snapshot.approval.decision === "PENDING" &&
                  approvedStep ? (
                    <div className={styles.actions}>
                      <button
                        type="button"
                        className={styles.secondaryButton}
                        disabled={busy}
                        onClick={() => void submitDecision("REJECTED")}
                      >
                        <X size={16} aria-hidden="true" /> 반려
                      </button>
                      <button
                        type="button"
                        className={styles.primaryButton}
                        disabled={busy}
                        onClick={() => void submitDecision("APPROVED")}
                      >
                        <Check size={16} aria-hidden="true" /> 승인
                      </button>
                    </div>
                  ) : snapshot.workflowStatus === "RESOLVED" ? (
                    <p className={styles.decisionOk} role="status">
                      승인된 조치를 실행하고 복구 확인과 포스트모템을 반영했습니다.
                    </p>
                  ) : snapshot.workflowStatus === "EXECUTION_FAILED" ? (
                    <p className={styles.error} role="alert">
                      승인 조치 실행 또는 완료 반영에 실패했습니다. 조건을 확인해 주세요.
                    </p>
                  ) : canResumeApprovedExecution(snapshot) ? (
                    <div className={styles.resumeBox}>
                      <p className={styles.decision} role="status">
                        승인을 기록했습니다. 복구 실행이 완료되지 않았으면 아래에서 다시 실행할 수 있습니다.
                      </p>
                      <button
                        type="button"
                        className={styles.primaryButton}
                        disabled={busy}
                        onClick={() => void submitDecision("APPROVED")}
                      >
                        <RefreshCw size={15} aria-hidden="true" /> 실행 재개
                      </button>
                    </div>
                  ) : snapshot.approval.decision !== "PENDING" ? (
                    <p className={styles.decision} role="status">
                      {snapshot.approval.decision === "APPROVED"
                        ? "승인을 기록했습니다. 복구 실행 결과를 기다립니다."
                        : "계획을 반려했습니다. 조건을 바꿔 다시 요청할 수 있습니다."}
                    </p>
                  ) : (
                    <p className={styles.error} role="alert">
                      계획에서 승인 대상을 찾지 못했습니다.
                    </p>
                  )}
                  <div className={styles.savePlanRow}>
                    <button
                      type="button"
                      className={styles.secondaryButton}
                      disabled={savingPlan}
                      onClick={() => void saveCurrentPlanToManager()}
                    >
                      계획 관리에 저장
                    </button>
                    {planSaveNotice && (
                      <p className={styles.recordMeta} role="status">
                        {planSaveNotice}
                      </p>
                    )}
                  </div>
                </div>
              ) : snapshot.workflowStatus === "BLOCKED" ? (
                <p className={styles.decision} role="status">
                  근거 또는 시간 제약으로 롤백 승인을 등록하지 못했습니다. 조건을 조정해 다시 요청해 주세요.
                </p>
              ) : isResolved && executions.length === 0 && !postMortem ? (
                <div className={styles.resolvedNote}>
                  <h3>복구 확인</h3>
                  <p>장애 복구가 완료되었습니다.</p>
                </div>
              ) : !isResolved ? (
                <p className={styles.empty}>계획을 요청하면 검토할 조치가 여기에 표시됩니다.</p>
              ) : null}

              {executions.length > 0 && (
                <section className={styles.recordSection} aria-labelledby="executions-label">
                  <h3 id="executions-label">실행 이력</h3>
                  <ul className={styles.executionList}>
                    {executions.map((exec) => (
                      <li key={exec.approvalId} className={styles.executionItem}>
                        <div className={styles.executionHead}>
                          <span className={styles.mono}>{exec.approvalId}</span>
                          <span
                            className={styles.status}
                            data-state={exec.executionStatus === "SUCCESS" ? "RESOLVED" : "EXECUTION_FAILED"}
                          >
                            {exec.executionStatus === "SUCCESS" ? "성공" : "실패"}
                          </span>
                        </div>
                        <p className={styles.command}>
                          <code>{exec.actionCommand}</code>
                        </p>
                        {exec.verificationSummary && <p className={styles.recordText}>{exec.verificationSummary}</p>}
                        {exec.executedAt && (
                          <span className={styles.recordMeta}>
                            {exec.executedBy} · {timeLabel(exec.executedAt)} KST
                          </span>
                        )}
                      </li>
                    ))}
                  </ul>
                </section>
              )}

              {postMortem && (
                <section className={styles.recordSection} aria-labelledby="postmortem-label">
                  <h3 id="postmortem-label" className={styles.postMortemTitle}>
                    <FileText size={15} aria-hidden="true" /> 포스트모템
                  </h3>
                  <dl className={styles.postMortemList}>
                    <div>
                      <dt>근본 원인</dt>
                      <dd>{postMortem.rootCause}</dd>
                    </div>
                    <div>
                      <dt>복구 요약</dt>
                      <dd>{postMortem.remediationSummary}</dd>
                    </div>
                    <div>
                      <dt>재발 방지</dt>
                      <dd>{postMortem.preventionItems}</dd>
                    </div>
                  </dl>
                  {postMortem.timelineMarkdown && (
                    <details className={styles.timelineDetails}>
                      <summary>대응 타임라인</summary>
                      <pre className={styles.timelinePre}>{postMortem.timelineMarkdown}</pre>
                    </details>
                  )}
                </section>
              )}
            </div>
          </aside>
        </div>
          </>
        )}
      </main>
    </>
  );
}

function Schedule({ replan, workWindow }: { replan: ReplanRecord | null; workWindow: WorkWindow | null }) {
  const draft = replan?.previousWindow ?? previousWindow;
  const activeWindow = workWindow ?? replan?.nextWindow ?? draft;

  const axisStart = Math.min(
    Date.parse(allowedWindow.start),
    Date.parse(draft.start),
    Date.parse(activeWindow.start),
  );
  const axisEnd = Math.max(
    Date.parse(allowedWindow.end),
    Date.parse(draft.end),
    Date.parse(activeWindow.end),
  );
  const duration = Math.max(axisEnd - axisStart, 1);

  function position(window: WorkWindow) {
    const startMs = Date.parse(window.start);
    const endMs = Date.parse(window.end);
    const left = Math.max(0, Math.min(100, ((startMs - axisStart) / duration) * 100));
    const width = Math.max(2, Math.min(100 - left, ((endMs - startMs) / duration) * 100));
    return {
      insetInlineStart: `${left}%`,
      width: `${width}%`,
    };
  }

  return (
    <section className={styles.schedule} aria-labelledby="schedule-label">
      <div className={styles.scheduleHead}>
        <h3 id="schedule-label">작업 창</h3>
        <span>KST</span>
      </div>
      <p className={styles.window}>
        <Clock3 size={17} aria-hidden="true" />
        <span className={styles.mono}>{windowLabel(activeWindow)}</span>
        <span>{replan ? "재계획" : workWindow ? "확정" : "초안"}</span>
      </p>
      <div className={styles.timeline} aria-hidden="true">
        <div className={styles.track}>
          <span className={styles.freezeBar} style={position(freezeWindow)} />
        </div>
        <div className={styles.track}>
          <span className={styles.oldBar} style={position(draft)} />
          {(replan || workWindow) && <span className={styles.newBar} style={position(activeWindow)} />}
        </div>
        <div className={styles.axis}>
          <span>{timeLabel(new Date(axisStart).toISOString())}</span>
          <span>{timeLabel(new Date(axisEnd).toISOString())}</span>
        </div>
      </div>
      <dl className={styles.legend}>
        <div>
          <dt className={styles.freezeKey}>변경 금지</dt>
          <dd>{windowLabel(freezeWindow)}</dd>
        </div>
        <div>
          <dt className={styles.oldKey}>충돌한 초안</dt>
          <dd>{windowLabel(draft)}</dd>
        </div>
        {(replan || workWindow) && (
          <div>
            <dt className={styles.newKey}>{replan ? "재계획" : "작업 창"}</dt>
            <dd>{windowLabel(activeWindow)}</dd>
          </div>
        )}
      </dl>
    </section>
  );
}
