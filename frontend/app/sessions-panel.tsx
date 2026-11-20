"use client";

import { ListChecks, RefreshCw, ShieldAlert, Trash2 } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import type { AgUiMode } from "./ag-ui-client";
import { timeLabel } from "./ag-ui-contract";
import {
  analyzeLogSessionAnomalies,
  deleteLogSession,
  getLogSessionDetail,
  listLogSessions,
  resolveAdminBaseUrl,
  type SessionAnomalyResponse,
  type SessionLogEvent,
  type SessionSummary,
} from "./ops-admin-client";
import styles from "./workspace.module.css";

const ANOMALY_LABEL: Record<SessionAnomalyResponse["status"], string> = {
  NORMAL: "이상 없음",
  ANOMALY: "이상 탐지",
  UNDETERMINED: "판정 불가",
};

export function SessionsPanel({ mode }: { mode: AgUiMode }) {
  const baseUrl = resolveAdminBaseUrl(mode);
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [events, setEvents] = useState<SessionLogEvent[]>([]);
  const [anomaly, setAnomaly] = useState<SessionAnomalyResponse | null>(null);
  const [loadingList, setLoadingList] = useState(true);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [analyzing, setAnalyzing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadDetail = useCallback(
    async (sessionId: string) => {
      setSelectedId(sessionId);
      setLoadingDetail(true);
      setAnomaly(null);
      setError(null);
      const res = await getLogSessionDetail(baseUrl, sessionId);
      if (res.ok) {
        setEvents(res.data.events);
      } else {
        setEvents([]);
        setError(res.message);
      }
      setLoadingDetail(false);
    },
    [baseUrl],
  );

  const loadSessions = useCallback(async () => {
    setLoadingList(true);
    setError(null);
    const res = await listLogSessions(baseUrl);
    if (res.ok) {
      setSessions(res.data);
      if (res.data.length > 0) {
        const nextId = selectedId && res.data.some((s) => s.sessionId === selectedId)
          ? selectedId
          : res.data[0].sessionId;
        await loadDetail(nextId);
      } else {
        setSelectedId(null);
        setEvents([]);
        setAnomaly(null);
      }
    } else {
      setError(res.message);
    }
    setLoadingList(false);
  }, [baseUrl, loadDetail, selectedId]);

  useEffect(() => {
    void loadSessions();
  }, [baseUrl]); // eslint-disable-line react-hooks/exhaustive-deps

  async function handleDelete(sessionId: string) {
    setError(null);
    const res = await deleteLogSession(baseUrl, sessionId);
    if (res.ok) {
      if (selectedId === sessionId) {
        setSelectedId(null);
        setEvents([]);
        setAnomaly(null);
      }
      await loadSessions();
    } else {
      setError(res.message);
    }
  }

  async function handleAnalyze() {
    if (!selectedId || analyzing) return;
    setAnalyzing(true);
    setError(null);
    const res = await analyzeLogSessionAnomalies(baseUrl, selectedId);
    if (res.ok) {
      setAnomaly(res.data);
    } else {
      setError(res.message);
    }
    setAnalyzing(false);
  }

  return (
    <div className={styles.adminGrid}>
      <section className={styles.adminPanel} aria-labelledby="sessions-list-label">
        <div className={styles.panelHead}>
          <h2 id="sessions-list-label">
            <ListChecks size={18} aria-hidden="true" /> 실행 로그 세션 ({sessions.length})
          </h2>
          <button
            type="button"
            className={styles.secondaryButton}
            disabled={loadingList}
            onClick={() => void loadSessions()}
          >
            <RefreshCw size={14} aria-hidden="true" /> 새로고침
          </button>
        </div>

        {loadingList ? (
          <p className={styles.progress}>세션 목록을 불러오는 중</p>
        ) : sessions.length === 0 ? (
          <p className={styles.empty}>저장된 실행 로그가 없습니다.</p>
        ) : (
          <ul className={styles.adminList}>
            {sessions.map((item) => (
              <li key={item.sessionId} className={styles.adminCard} data-active={item.sessionId === selectedId || undefined}>
                <div className={styles.adminCardHead}>
                  <button
                    type="button"
                    className={styles.sessionSelectButton}
                    onClick={() => void loadDetail(item.sessionId)}
                  >
                    <span className={styles.mono}>{item.sessionId}</span>
                  </button>
                  <button
                    type="button"
                    className={styles.iconButton}
                    onClick={() => void handleDelete(item.sessionId)}
                    aria-label={`${item.sessionId} 로그 삭제`}
                  >
                    <Trash2 size={14} aria-hidden="true" /> 삭제
                  </button>
                </div>
                <p className={styles.recordMeta}>
                  이벤트 {item.eventCount}건
                  {item.lastEventAt ? ` · 마지막 기록 ${timeLabel(item.lastEventAt)} KST` : ""}
                </p>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className={styles.adminPanel} aria-labelledby="session-detail-label">
        <div className={styles.panelHead}>
          <h2 id="session-detail-label">실행 로그 상세 (읽기 전용)</h2>
          <button
            type="button"
            className={styles.primaryButton}
            disabled={!selectedId || analyzing}
            onClick={() => void handleAnalyze()}
          >
            <ShieldAlert size={15} aria-hidden="true" /> {analyzing ? "분석 중" : "이상 징후 분석"}
          </button>
        </div>

        {error && (
          <p className={styles.error} role="alert">
            {error}
          </p>
        )}

        {anomaly && (
          <div className={styles.anomalyBox} data-status={anomaly.status} role="status">
            <div className={styles.adminCardHead}>
              <strong>판정: {ANOMALY_LABEL[anomaly.status]}</strong>
              <span className={styles.recordMeta}>
                평가 이벤트 {anomaly.evaluatedEventCount}건 · LLM 호출 {anomaly.llmCalls}회
              </span>
            </div>
            {anomaly.findings.length > 0 && (
              <ul className={styles.evidence}>
                {anomaly.findings.map((finding, idx) => (
                  <li key={`${finding.rule}-${idx}`}>
                    <code>{finding.rule}</code> {finding.summary}
                  </li>
                ))}
              </ul>
            )}
            {anomaly.diagnosis && <p className={styles.recordText}>{anomaly.diagnosis}</p>}
          </div>
        )}

        {loadingDetail ? (
          <p className={styles.progress}>실행 로그를 불러오는 중</p>
        ) : !selectedId ? (
          <p className={styles.empty}>세션을 선택하면 실행 순서대로 이벤트가 표시됩니다.</p>
        ) : (
          <ol className={styles.trail}>
            {events.map((ev, index) => (
              <li key={`${ev.timestamp ?? index}-${ev.eventType}-${index}`} data-kind={ev.eventType === "replan" ? "change" : "record"}>
                <strong>
                  {ev.eventType}
                  {ev.timestamp ? ` · ${timeLabel(ev.timestamp)} KST` : ""}
                </strong>
                <p className={styles.mono}>{JSON.stringify(ev.payload ?? {})}</p>
              </li>
            ))}
          </ol>
        )}
      </section>
    </div>
  );
}
