"""Spring 승인 원장을 업무 상태의 기준으로 삼아 AG-UI 스냅샷을 판정·재구성한다."""

from __future__ import annotations

from typing import Any

# 원장에 승인이 남아 있어 운영자가 계속 처리해야 하는 상태
OPEN_APPROVAL_STATUSES = frozenset({"AWAITING_APPROVAL", "AWAITING_EXECUTION_RESULT"})


def _items(detail: dict[str, Any] | None, key: str) -> list[dict[str, Any]]:
    raw = detail.get(key) if isinstance(detail, dict) else None
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def is_resolved(detail: dict[str, Any] | None) -> bool:
    incident = detail.get("incident") if isinstance(detail, dict) else None
    return isinstance(incident, dict) and incident.get("status") == "RESOLVED"


def find_approval(detail: dict[str, Any] | None, approval_id: str) -> dict[str, Any] | None:
    return next((a for a in _items(detail, "approvals") if a.get("approvalId") == approval_id), None)


def find_execution(detail: dict[str, Any] | None, approval_id: str) -> dict[str, Any] | None:
    return next((e for e in _items(detail, "executions") if e.get("approvalId") == approval_id), None)


def find_plan(detail: dict[str, Any] | None, plan_id: str | None) -> dict[str, Any] | None:
    if not plan_id:
        return None
    return next((p for p in _items(detail, "plans") if p.get("planId") == plan_id), None)


def approval_status(detail: dict[str, Any] | None, approval_id: str) -> str | None:
    """승인 1건의 원장 상태를 워크플로 상태로 변환한다. 원장에 없으면 None."""
    execution = find_execution(detail, approval_id)
    if execution is not None:
        return "EXECUTION_FAILED" if execution.get("executionStatus") == "FAILED" else "RESOLVED"
    approval = find_approval(detail, approval_id)
    if approval is None:
        return None
    return {
        "PENDING": "AWAITING_APPROVAL",
        "APPROVED": "AWAITING_EXECUTION_RESULT",
        "REJECTED": "REJECTED",
    }.get(str(approval.get("decision")))


def approval_view(approval: dict[str, Any]) -> dict[str, Any]:
    return {
        "approvalId": approval.get("approvalId"),
        "planId": approval.get("planId"),
        "stepNo": approval.get("stepNo") or 2,
        "decision": approval.get("decision"),
    }


def reconcile_snapshot(snapshot: dict[str, Any], detail: dict[str, Any] | None) -> dict[str, Any]:
    """저장된 스냅샷이 원장보다 뒤처졌으면 원장 상태를 반영한 새 스냅샷을, 같으면 원본을 반환한다."""
    if not isinstance(detail, dict):
        return snapshot
    updated = dict(snapshot)
    approval = snapshot.get("approval")
    approval_id = str(approval.get("approvalId") or "") if isinstance(approval, dict) else ""
    if approval_id:
        ledger_approval = find_approval(detail, approval_id)
        if ledger_approval is not None and ledger_approval.get("decision"):
            updated["approval"] = {**approval, "decision": ledger_approval["decision"]}
        status = approval_status(detail, approval_id)
        if status is not None:
            updated["workflowStatus"] = status
    # 장애가 이미 해결됐다면 어떤 스냅샷도 그 아래 상태로 남기지 않는다.
    if is_resolved(detail):
        updated["workflowStatus"] = "RESOLVED"
    return updated if updated != snapshot else snapshot
