"use client";

import Link from "next/link";
import { Activity, ArrowUpRight, CalendarClock, Check, ChevronRight, Clock3, FlaskConical, ListChecks, RotateCcw, Send, X } from "lucide-react";
import { useEffect, useReducer, useRef } from "react";
import {
  allowedWindow, demoPrompt, freezeWindow, incidents, initialSnapshot, previousWindow, runDemo, timeLabel, windowLabel,
  type Decision, type DemoEvent, type Incident, type Replan, type RunInput, type Snapshot, type WorkWindow,
} from "./demo";
import styles from "./workspace.module.css";

type Entry = { id: string; label: string; text: string; kind: "record" | "operator" | "change" };
type State = { snapshot: Snapshot; entries: Entry[]; replan: Replan | null; busy: boolean; error: string | null };
type Action = DemoEvent | { type: "REQUEST"; entry: Entry } | { type: "RESET"; incident: Incident };

const statusText: Record<Snapshot["workflowStatus"], string> = {
  IDLE: "계획 요청 전",
  PLANNING: "계획 수립 중",
  AWAITING_APPROVAL: "승인 대기",
  AWAITING_EXECUTION_RESULT: "실행 결과 대기",
  REJECTED: "반려됨",
  RESOLVED: "해결됨",
};

function createDemoId(): string {
  return crypto.randomUUID?.() ?? Array.from(
    crypto.getRandomValues(new Uint8Array(16)),
    (byte) => byte.toString(16).padStart(2, "0"),
  ).join("");
}

function initialState(incident: Incident): State {
  return {
    snapshot: initialSnapshot(incident),
    entries: [
      { id: "intake", label: "장애 접수", text: incident.summary, kind: "record" },
      ...(incident.resolved ? [] : [{
        id: "scope", label: "요청 조건", text: "이전 버전의 502 비율과 변경 금지 시간을 확인합니다.", kind: "record" as const,
      }]),
    ],
    replan: null,
    busy: false,
    error: null,
  };
}

function reduce(state: State, action: Action): State {
  switch (action.type) {
    case "RESET":
      return initialState(action.incident);
    case "REQUEST":
      return { ...state, busy: true, error: null, entries: [...state.entries, action.entry] };
    case "RUN_STARTED":
      return { ...state, busy: true };
    case "RUN_FINISHED":
      return { ...state, busy: false };
    case "RUN_ERROR":
      return { ...state, busy: false, error: action.message };
    case "STATE_SNAPSHOT":
      return action.snapshot.incidentCode === state.snapshot.incidentCode ? { ...state, snapshot: action.snapshot } : state;
    case "TOOL_CALL_RESULT": {
      const { source, summary } = JSON.parse(action.content) as { source: string; summary: string };
      return {
        ...state,
        entries: [...state.entries, { id: action.messageId, label: source, text: summary, kind: "record" }],
      };
    }
    case "CUSTOM": {
      const replan = action.value;
      return {
        ...state, replan,
        entries: [...state.entries, {
          id: "replan", label: "작업 창 재계획",
          text: `${replan.reason} ${windowLabel(replan.nextWindow)}로 이동했습니다.`, kind: "change",
        }],
      };
    }
  }
}

export function Workspace({ incident }: { incident: Incident }) {
  const [state, dispatch] = useReducer(reduce, incident, initialState);
  const activeRun = useRef<AbortController | null>(null);
  const threadId = useRef<string | null>(null);
  const { snapshot, entries, replan, busy } = state;
  const approvedStep = snapshot.plan && snapshot.approval && snapshot.plan.planId === snapshot.approval.planId
    ? snapshot.plan.steps.find((step) => step.stepNo === snapshot.approval?.stepNo)
    : undefined;

  useEffect(() => () => activeRun.current?.abort(), []);

  function reset() {
    activeRun.current?.abort();
    activeRun.current = null;
    threadId.current = null;
    dispatch({ type: "RESET", incident });
  }

  async function submit(decision?: Decision) {
    if (activeRun.current || incident.resolved) return;
    if (decision ? snapshot.workflowStatus !== "AWAITING_APPROVAL" || !snapshot.approval : snapshot.workflowStatus !== "IDLE") return;

    const controller = new AbortController();
    activeRun.current = controller;
    try {
      threadId.current ??= createDemoId();
      const runId = createDemoId();
      const approvalDecision = decision && snapshot.approval
        ? { approvalId: snapshot.approval.approvalId, decision }
        : undefined;
      const input: RunInput = {
        threadId: threadId.current,
        runId,
        messages: approvalDecision ? [] : [{ id: runId, role: "user", content: demoPrompt }],
        tools: [],
        context: [],
        forwardedProps: approvalDecision
          ? { incidentCode: incident.incidentCode, approvalDecision }
          : { incidentCode: incident.incidentCode, incidentStartedAt: incident.startedAt },
      };
      dispatch({
        type: "REQUEST",
        entry: {
          id: runId,
          label: "운영자",
          text: approvalDecision ? (decision === "APPROVED" ? "복구 계획을 승인합니다." : "복구 계획을 반려합니다.") : demoPrompt,
          kind: "operator",
        },
      });
      for await (const event of runDemo(input, snapshot, controller.signal)) {
        if (controller.signal.aborted) return;
        dispatch(event);
      }
    } catch {
      if (!controller.signal.aborted) dispatch({ type: "RUN_ERROR", message: "모의 실행이 중단됐습니다. 새 모의 실행으로 다시 시작해 주세요." });
    } finally {
      if (activeRun.current === controller) activeRun.current = null;
    }
  }

  return (
    <>
      <header className={styles.topbar}>
        <div className={styles.topInner}>
          <div className={styles.brand}><Activity size={21} aria-hidden="true" /> Incident Ops</div>
          <span className={styles.mode}><FlaskConical size={15} aria-hidden="true" /> 모의 실행</span>
          <button type="button" className={styles.secondaryButton} onClick={reset} disabled={incident.resolved || (snapshot.workflowStatus === "IDLE" && !state.error)}>
            <RotateCcw size={15} aria-hidden="true" /> 새 모의 실행
          </button>
        </div>
      </header>

      <main className={styles.main}>
        <div className={styles.incidentHeader}>
          <div>
            <div className={styles.incidentMeta}><span className={styles.mono}>{incident.incidentCode}</span><span>{incident.serviceName}</span></div>
            <h1>{incident.title}</h1>
            <p>{incident.summary}</p>
          </div>
          <dl className={styles.headerFacts}>
            <div><dt>심각도</dt><dd className={incident.severity === "SEV1" ? styles.severity : undefined}>{incident.severity}</dd></div>
            <div><dt>발생 시각</dt><dd className={styles.mono}>{timeLabel(incident.startedAt)} KST</dd></div>
          </dl>
        </div>

        <div className={styles.workspace}>
          <nav className={styles.incidents} aria-labelledby="incidents-label">
            <h2 id="incidents-label">장애 목록</h2>
            <ul>
              {incidents.map((item) => (
                <li key={item.incidentCode}>
                  <Link
                    href={`/?incident=${item.incidentCode}`}
                    aria-current={item.incidentCode === incident.incidentCode ? "page" : undefined}
                    className={styles.incidentLink}
                  >
                    <span className={styles.incidentLinkTop}><span className={styles.mono}>{item.incidentCode}</span><span>{item.severity}</span></span>
                    <strong>{item.title}</strong>
                    <span className={styles.incidentLinkBottom}>{item.resolved ? "해결됨" : "모의 시나리오"}<ChevronRight size={14} aria-hidden="true" /></span>
                  </Link>
                </li>
              ))}
            </ul>
          </nav>

          <section className={styles.activity} aria-labelledby="activity-label">
            <div className={styles.panelHead}>
              <h2 id="activity-label"><ListChecks size={18} aria-hidden="true" /> 작업 기록</h2>
              {snapshot.approval?.decision === "PENDING" && <a href="#plan">계획 검토 <ArrowUpRight size={14} aria-hidden="true" /></a>}
            </div>

            <ol className={styles.trail} aria-label="작업 타임라인" aria-live="polite">
              {entries.map((entry) => (
                <li key={entry.id} data-kind={entry.kind}><strong>{entry.label}</strong><p>{entry.text}</p></li>
              ))}
            </ol>
            {busy && <p className={styles.progress} role="status">모의 실행 처리 중</p>}
            {state.error && <p className={styles.error} role="alert">{state.error}</p>}

            {snapshot.workflowStatus === "IDLE" && (
              <div className={styles.composer}>
                <h3>복구 요청</h3>
                <p>{demoPrompt}</p>
                <button className={styles.primaryButton} type="button" disabled={busy} onClick={() => void submit()}>
                  <Send size={15} aria-hidden="true" /> 계획 요청
                </button>
              </div>
            )}
          </section>

          <aside className={styles.plan} id="plan" aria-labelledby="plan-label">
            <div className={styles.panelHead}>
              <h2 id="plan-label"><CalendarClock size={18} aria-hidden="true" /> 복구 계획</h2>
              <span className={styles.status} data-state={snapshot.workflowStatus} role="status">{statusText[snapshot.workflowStatus]}</span>
            </div>

            {incident.resolved ? (
              <div className={styles.planBody}>
                <h3>복구 확인</h3>
                <p>결제 승인 응답 시간이 정상 범위로 돌아왔습니다.</p>
              </div>
            ) : (
              <div className={styles.planBody}>
                <Schedule replan={replan} />
                {snapshot.plan && snapshot.approval ? (
                  <div className={styles.approval}>
                    <div className={styles.approvalHead}><h3>승인 대상</h3><span className={styles.mono}>{snapshot.approval.approvalId}</span></div>
                    <p className={styles.actionTitle}>{approvedStep?.description}</p>
                    <details>
                      <summary>근거 및 복구 확인 기준</summary>
                      <ul className={styles.evidence}>
                        {snapshot.plan.evidence.map((item) => <li key={item.chunkId}><code>{item.chunkId}</code> {item.summary}</li>)}
                      </ul>
                      {approvedStep?.actionCommand && <p className={styles.command}><code>{approvedStep.actionCommand}</code></p>}
                      <p>{snapshot.plan.verificationCriteria}</p>
                    </details>
                    {snapshot.workflowStatus === "AWAITING_APPROVAL" && snapshot.approval.decision === "PENDING" && approvedStep ? (
                      <div className={styles.actions}>
                        <button type="button" className={styles.secondaryButton} disabled={busy} onClick={() => void submit("REJECTED")}><X size={16} aria-hidden="true" /> 반려</button>
                        <button type="button" className={styles.primaryButton} disabled={busy} onClick={() => void submit("APPROVED")}><Check size={16} aria-hidden="true" /> 승인</button>
                      </div>
                    ) : snapshot.approval.decision !== "PENDING" ? (
                      <p className={styles.decision} role="status">
                        {snapshot.approval.decision === "APPROVED" ? "승인을 기록했습니다. 복구 실행 결과를 기다립니다." : "계획을 반려했습니다."}
                      </p>
                    ) : <p className={styles.error} role="alert">계획에서 승인 대상을 찾지 못했습니다.</p>}
                  </div>
                ) : <p className={styles.empty}>계획을 요청하면 검토할 조치가 여기에 표시됩니다.</p>}
              </div>
            )}
          </aside>
        </div>
      </main>
    </>
  );
}

function Schedule({ replan }: { replan: Replan | null }) {
  const draft = replan?.previousWindow ?? previousWindow;

  function position(window: WorkWindow) {
    const start = Date.parse(allowedWindow.start);
    const duration = Date.parse(allowedWindow.end) - start;
    return {
      insetInlineStart: `${(Date.parse(window.start) - start) / duration * 100}%`,
      width: `${(Date.parse(window.end) - Date.parse(window.start)) / duration * 100}%`,
    };
  }

  return (
    <section className={styles.schedule} aria-labelledby="schedule-label">
      <div className={styles.scheduleHead}><h3 id="schedule-label">작업 창</h3><span>KST</span></div>
      <p className={styles.window}>
        <Clock3 size={17} aria-hidden="true" />
        <span className={styles.mono}>{windowLabel(replan?.nextWindow ?? draft)}</span>
        <span>{replan ? "재계획" : "초안"}</span>
      </p>
      <div className={styles.timeline} aria-hidden="true">
        <div className={styles.track}><span className={styles.freezeBar} style={position(freezeWindow)} /></div>
        <div className={styles.track}><span className={styles.oldBar} style={position(draft)} />{replan && <span className={styles.newBar} style={position(replan.nextWindow)} />}</div>
        <div className={styles.axis}><span>{timeLabel(allowedWindow.start)}</span><span>{timeLabel(allowedWindow.end)}</span></div>
      </div>
      <dl className={styles.legend}>
        <div><dt className={styles.freezeKey}>변경 금지</dt><dd>{windowLabel(freezeWindow)}</dd></div>
        <div><dt className={styles.oldKey}>충돌한 초안</dt><dd>{windowLabel(draft)}</dd></div>
        {replan && <div><dt className={styles.newKey}>재계획</dt><dd>{windowLabel(replan.nextWindow)}</dd></div>}
      </dl>
    </section>
  );
}
