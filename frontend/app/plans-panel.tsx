"use client";

import { AlertTriangle, CalendarClock, Pencil, Plus, RefreshCw, Trash2, X } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import type { AgUiMode } from "./ag-ui-client";
import {
  createManagedPlan,
  deleteManagedPlan,
  listManagedPlans,
  resolveAdminBaseUrl,
  updateManagedPlan,
  type ManagedPlanItem,
} from "./ops-admin-client";
import styles from "./workspace.module.css";

type PlansPanelProps = {
  mode: AgUiMode;
  defaultIncidentCode: string;
  defaultServiceName: string;
};

export function PlansPanel({ mode, defaultIncidentCode, defaultServiceName }: PlansPanelProps) {
  const baseUrl = resolveAdminBaseUrl(mode);
  const [plans, setPlans] = useState<ManagedPlanItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [warning, setWarning] = useState<string | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);

  const [incidentCode, setIncidentCode] = useState(defaultIncidentCode);
  const [serviceName, setServiceName] = useState(defaultServiceName);
  const [title, setTitle] = useState("");
  const [planDate, setPlanDate] = useState("2026-11-18");
  const [startTime, setStartTime] = useState("14:40");
  const [endTime, setEndTime] = useState("14:45");
  const [actionCommand, setActionCommand] = useState("kubectl rollout undo deployment/payment-service");
  const [notes, setNotes] = useState("");

  const loadPlans = useCallback(async () => {
    setLoading(true);
    setError(null);
    const res = await listManagedPlans(baseUrl);
    if (res.ok) {
      setPlans(res.data);
    } else {
      setError(res.message);
    }
    setLoading(false);
  }, [baseUrl]);

  useEffect(() => {
    void loadPlans();
  }, [loadPlans]);

  function startEdit(plan: ManagedPlanItem) {
    setEditingId(plan.planId);
    setIncidentCode(plan.incidentCode);
    setServiceName(plan.serviceName);
    setTitle(plan.title);
    setPlanDate(plan.planDate);
    setStartTime(plan.startTime);
    setEndTime(plan.endTime);
    setActionCommand(plan.actionCommand ?? "");
    setNotes(plan.notes);
    setWarning(null);
    setError(null);
  }

  function cancelEdit() {
    setEditingId(null);
    setIncidentCode(defaultIncidentCode);
    setServiceName(defaultServiceName);
    setTitle("");
    setPlanDate("2026-11-18");
    setStartTime("14:40");
    setEndTime("14:45");
    setActionCommand("kubectl rollout undo deployment/payment-service");
    setNotes("");
    setError(null);
  }

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (!title.trim() || saving) return;
    setSaving(true);
    setError(null);
    setWarning(null);

    if (editingId) {
      const res = await updateManagedPlan(baseUrl, editingId, {
        title: title.trim(),
        planDate,
        startTime,
        endTime,
        actionCommand: actionCommand.trim(),
        notes: notes.trim(),
      });
      if (res.ok) {
        if (res.data.warning) setWarning(res.data.warning);
        cancelEdit();
        await loadPlans();
      } else {
        setError(res.message);
      }
    } else {
      const res = await createManagedPlan(baseUrl, {
        incidentCode: incidentCode.trim() || defaultIncidentCode,
        serviceName: serviceName.trim() || defaultServiceName,
        title: title.trim(),
        planDate,
        startTime,
        endTime,
        actionCommand: actionCommand.trim() || null,
        notes: notes.trim(),
      });
      if (res.ok) {
        if (res.data.warning) setWarning(res.data.warning);
        cancelEdit();
        await loadPlans();
      } else {
        setError(res.message);
      }
    }
    setSaving(false);
  }

  async function handleDelete(planId: string) {
    if (saving) return;
    setSaving(true);
    setError(null);
    const res = await deleteManagedPlan(baseUrl, planId);
    if (res.ok) {
      if (editingId === planId) cancelEdit();
      await loadPlans();
    } else {
      setError(res.message);
    }
    setSaving(false);
  }

  return (
    <div className={styles.adminGrid}>
      <section className={styles.adminPanel} aria-labelledby="plan-form-label">
        <div className={styles.panelHead}>
          <h2 id="plan-form-label">
            <CalendarClock size={18} aria-hidden="true" /> {editingId ? "계획 수정" : "작업 계획 저장"}
          </h2>
          {editingId && (
            <button type="button" className={styles.secondaryButton} onClick={cancelEdit}>
              <X size={14} aria-hidden="true" /> 취소
            </button>
          )}
        </div>

        <form className={styles.adminForm} onSubmit={(e) => void handleSubmit(e)}>
          <div className={styles.formRowTwo}>
            <label className={styles.field}>
              <span>장애 코드</span>
              <input
                type="text"
                value={incidentCode}
                disabled={Boolean(editingId) || saving}
                onChange={(e) => setIncidentCode(e.target.value)}
                required
              />
            </label>
            <label className={styles.field}>
              <span>서비스</span>
              <input
                type="text"
                value={serviceName}
                disabled={Boolean(editingId) || saving}
                onChange={(e) => setServiceName(e.target.value)}
                required
              />
            </label>
          </div>

          <label className={styles.field}>
            <span>계획 제목</span>
            <input
              type="text"
              value={title}
              disabled={saving}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="예: 결제 서비스 v41 롤백"
              required
            />
          </label>

          <div className={styles.formRowThree}>
            <label className={styles.field}>
              <span>작업 날짜</span>
              <input type="date" value={planDate} disabled={saving} onChange={(e) => setPlanDate(e.target.value)} required />
            </label>
            <label className={styles.field}>
              <span>시작 시각</span>
              <input type="time" value={startTime} disabled={saving} onChange={(e) => setStartTime(e.target.value)} required />
            </label>
            <label className={styles.field}>
              <span>종료 시각</span>
              <input type="time" value={endTime} disabled={saving} onChange={(e) => setEndTime(e.target.value)} required />
            </label>
          </div>

          <label className={styles.field}>
            <span>조치 명령어 (선택)</span>
            <input
              type="text"
              className={styles.mono}
              value={actionCommand}
              disabled={saving}
              onChange={(e) => setActionCommand(e.target.value)}
            />
          </label>

          <label className={styles.field}>
            <span>비고</span>
            <textarea rows={2} value={notes} disabled={saving} onChange={(e) => setNotes(e.target.value)} />
          </label>

          <button type="submit" className={styles.primaryButton} disabled={saving || !title.trim()}>
            <Plus size={15} aria-hidden="true" /> {editingId ? "변경 저장" : "계획 저장"}
          </button>
        </form>
      </section>

      <section className={styles.adminPanel} aria-labelledby="plan-list-label">
        <div className={styles.panelHead}>
          <h2 id="plan-list-label">저장된 작업 계획 ({plans.length})</h2>
          <button type="button" className={styles.secondaryButton} disabled={loading} onClick={() => void loadPlans()}>
            <RefreshCw size={14} aria-hidden="true" /> 새로고침
          </button>
        </div>

        {warning && (
          <div className={styles.warningBox} role="status">
            <AlertTriangle size={16} aria-hidden="true" />
            <span>{warning}</span>
          </div>
        )}
        {error && (
          <p className={styles.error} role="alert">
            {error}
          </p>
        )}

        {loading ? (
          <p className={styles.progress}>계획 목록을 불러오는 중</p>
        ) : plans.length === 0 ? (
          <p className={styles.empty}>저장된 작업 계획이 없습니다.</p>
        ) : (
          <ul className={styles.adminList}>
            {plans.map((plan) => (
              <li key={plan.planId} className={styles.adminCard}>
                <div className={styles.adminCardHead}>
                  <div>
                    <span className={styles.mono}>
                      {plan.planDate} {plan.startTime}–{plan.endTime}
                    </span>
                    <span className={styles.recordMeta}> · {plan.incidentCode} · {plan.serviceName}</span>
                  </div>
                  <div className={styles.cardActions}>
                    <button
                      type="button"
                      className={styles.iconButton}
                      disabled={saving}
                      onClick={() => startEdit(plan)}
                      aria-label={`${plan.title} 수정`}
                    >
                      <Pencil size={14} aria-hidden="true" /> 수정
                    </button>
                    <button
                      type="button"
                      className={styles.iconButton}
                      disabled={saving}
                      onClick={() => void handleDelete(plan.planId)}
                      aria-label={`${plan.title} 삭제`}
                    >
                      <Trash2 size={14} aria-hidden="true" /> 삭제
                    </button>
                  </div>
                </div>
                <strong>{plan.title}</strong>
                {plan.actionCommand && (
                  <p className={styles.command}>
                    <code>{plan.actionCommand}</code>
                  </p>
                )}
                {plan.notes && <p className={styles.recordText}>{plan.notes}</p>}
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
