"use client";

import Link from "next/link";
import {
  Activity,
  ArrowUpRight,
  CalendarClock,
  Check,
  ChevronRight,
  Clock3,
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
  timeLabel,
  windowLabel,
  type Decision,
  type ReplanRecord,
  type WorkWindow,
} from "./ag-ui-contract";
import { allowedWindow, demoPrompt, freezeWindow, incidents, previousWindow, type Incident } from "./demo";
import { canSendPrompt, createInitialState, workspaceReducer } from "./workspace-state";
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

export function Workspace({ incident, initialMode }: { incident: Incident; initialMode: AgUiMode }) {
  const [mode, setMode] = useState<AgUiMode>(initialMode);
  const [prompt, setPrompt] = useState(demoPrompt);
  const [state, dispatch] = useReducer(workspaceReducer, incident, createInitialState);
  const activeRun = useRef<AbortController | null>(null);
  const threadIdRef = useRef<string | null>(null);

  const { snapshot, entries, replan, busy, activeStep, restoring, error, needsResync } = state;
  const approvedStep =
    snapshot.plan && snapshot.approval && snapshot.plan.planId === snapshot.approval.planId
      ? snapshot.plan.steps.find((step) => step.stepNo === snapshot.approval?.stepNo)
      : undefined;

  const restoreSession = useCallback(
    async (targetMode: AgUiMode, storedThreadId: string) => {
      if (activeRun.current || incident.resolved) return;
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
          },
          signal: controller.signal,
          dispatch,
        });
      } finally {
        if (activeRun.current === controller) {
          activeRun.current = null;
        }
      }
    },
    [incident.incidentCode, incident.resolved],
  );

  useEffect(() => {
    const saved = readStoredThreadId(mode, incident.incidentCode);
    if (saved && !incident.resolved) {
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
    writeStoredThreadId(mode, incident.incidentCode, null);
    setPrompt(demoPrompt);
    dispatch({ type: "RESET", incident });
  }

  function switchMode(nextMode: AgUiMode) {
    if (nextMode === mode) return;
    activeRun.current?.abort();
    activeRun.current = null;
    threadIdRef.current = null;
    setMode(nextMode);
    setPrompt(demoPrompt);
    dispatch({ type: "RESET", incident });
  }

  async function submitPrompt() {
    const trimmed = prompt.trim();
    if (!trimmed || activeRun.current || incident.resolved) return;
    if (!canSendPrompt(snapshot.workflowStatus, incident.resolved)) return;

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
          text: trimmed,
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
          incidentStartedAt: incident.startedAt,
        },
        signal: controller.signal,
        dispatch,
      });

      if (outcome.ok && !controller.signal.aborted) {
        setPrompt("");
      }
    } finally {
      if (activeRun.current === controller) {
        activeRun.current = null;
      }
    }
  }

  async function submitDecision(decision: Decision) {
    if (activeRun.current || incident.resolved) return;
    if (snapshot.workflowStatus !== "AWAITING_APPROVAL" || !snapshot.approval || snapshot.approval.decision !== "PENDING") {
      return;
    }

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
          text: decision === "APPROVED" ? "복구 계획을 승인합니다." : "복구 계획을 반려합니다.",
          kind: "operator",
        },
      });

      await runAgUiRequest({
        url: resolveAgUiUrl(mode),
        threadId: tid,
        runId,
        forwardedProps: {
          incidentCode: incident.incidentCode,
          approvalDecision: {
            approvalId: snapshot.approval.approvalId,
            decision,
          },
        },
        signal: controller.signal,
        dispatch,
      });
    } finally {
      if (activeRun.current === controller) {
        activeRun.current = null;
      }
    }
  }

  function handleResync() {
    const tid = threadIdRef.current ?? readStoredThreadId(mode, incident.incidentCode);
    if (!tid) return;
    void restoreSession(mode, tid);
  }

  const showComposer = canSendPrompt(snapshot.workflowStatus, incident.resolved);
  const hasSession = snapshot.workflowStatus !== "IDLE" || Boolean(error) || Boolean(threadIdRef.current);

  return (
    <>
      <header className={styles.topbar}>
        <div className={styles.topInner}>
          <div className={styles.brand}>
            <Activity size={20} aria-hidden="true" /> Incident Ops
          </div>
          <div className={styles.modeSwitch} role="group" aria-label="AG-UI 연결 대상">
            <button
              type="button"
              className={styles.modeButton}
              aria-pressed={mode === "live"}
              onClick={() => switchMode("live")}
            >
              <Radio size={14} aria-hidden="true" /> AG-UI
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
          <button
            type="button"
            className={styles.secondaryButton}
            onClick={resetSession}
            disabled={incident.resolved || !hasSession}
          >
            <RotateCcw size={15} aria-hidden="true" /> 새 대화
          </button>
        </div>
      </header>

      <main className={styles.main}>
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
              <dt>발생 시각</dt>
              <dd className={styles.mono}>{timeLabel(incident.startedAt)} KST</dd>
            </div>
          </dl>
        </div>

        <div className={styles.workspace}>
          <nav className={styles.incidents} aria-labelledby="incidents-label">
            <h2 id="incidents-label">장애 목록</h2>
            <ul>
              {incidents.map((item) => {
                const href = mode === "mock" ? `/?incident=${item.incidentCode}&mode=mock` : `/?incident=${item.incidentCode}`;
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
                        {item.resolved ? "해결됨" : "대응 중"}
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

            {busy && (
              <p className={styles.progress} role="status">
                {restoring ? "이전 대화 상태를 복원하는 중" : activeStep ? `${activeStep} 진행 중` : "AG-UI 응답을 받는 중"}
              </p>
            )}

            {error && (
              <div className={styles.errorBox} role="alert">
                <p className={styles.error}>{error}</p>
                {needsResync && threadIdRef.current && (
                  <button type="button" className={styles.secondaryButton} disabled={busy} onClick={handleResync}>
                    <RefreshCw size={14} aria-hidden="true" /> 상태 다시 조회
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
                <button className={styles.primaryButton} type="submit" disabled={busy || !prompt.trim()}>
                  <Send size={15} aria-hidden="true" /> 계획 요청
                </button>
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

            {incident.resolved ? (
              <div className={styles.planBody}>
                <h3>복구 확인</h3>
                <p>결제 승인 응답 시간이 정상 범위로 돌아왔습니다.</p>
              </div>
            ) : (
              <div className={styles.planBody}>
                <Schedule replan={replan} workWindow={snapshot.plan?.workWindow ?? null} />
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
                        {snapshot.plan.evidence.map((item) => (
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
                  </div>
                ) : snapshot.workflowStatus === "BLOCKED" ? (
                  <p className={styles.decision} role="status">
                    근거 또는 시간 제약으로 롤백 승인을 등록하지 못했습니다. 조건을 조정해 다시 요청해 주세요.
                  </p>
                ) : (
                  <p className={styles.empty}>계획을 요청하면 검토할 조치가 여기에 표시됩니다.</p>
                )}
              </div>
            )}
          </aside>
        </div>
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
