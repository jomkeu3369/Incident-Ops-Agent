import { NextResponse } from "next/server";
import type { ChatMessageRecord, Decision, ReplanRecord, Snapshot } from "../../ag-ui-contract";
import { demoPlan, incidents, initialSnapshot, nextWindow, previousWindow } from "../../demo";

type MockThreadState = {
  incidentCode: string;
  snapshot: Snapshot;
  messages: ChatMessageRecord[];
};

const globalStore = globalThis as typeof globalThis & {
  __mockAgUiThreads?: Map<string, MockThreadState>;
};

const threadStore = (globalStore.__mockAgUiThreads ??= new Map<string, MockThreadState>());

const delay = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

function sseFrame(event: Record<string, unknown>): Uint8Array {
  return new TextEncoder().encode(`data: ${JSON.stringify(event)}\n\n`);
}

function getOrResetThread(threadId: string, incidentCode: string): MockThreadState {
  const existing = threadStore.get(threadId);
  if (existing && existing.incidentCode === incidentCode) {
    return existing;
  }
  const inc = incidents.find((item) => item.incidentCode === incidentCode) ?? incidents[0];
  const fresh: MockThreadState = {
    incidentCode,
    snapshot: { ...initialSnapshot(inc), incidentCode },
    messages: [],
  };
  threadStore.set(threadId, fresh);
  return fresh;
}

export async function POST(request: Request) {
  let body: Record<string, unknown>;
  try {
    body = (await request.json()) as Record<string, unknown>;
  } catch {
    return NextResponse.json({ detail: "JSON 본문을 읽을 수 없습니다." }, { status: 400 });
  }

  const threadId = typeof body.threadId === "string" && body.threadId ? body.threadId : null;
  const runId = typeof body.runId === "string" && body.runId ? body.runId : null;
  const forwardedProps =
    body.forwardedProps && typeof body.forwardedProps === "object"
      ? (body.forwardedProps as Record<string, unknown>)
      : null;

  if (!threadId || !runId || !forwardedProps || typeof forwardedProps.incidentCode !== "string" || !forwardedProps.incidentCode) {
    return NextResponse.json({ detail: "threadId, runId, forwardedProps.incidentCode는 필수입니다." }, { status: 400 });
  }

  const incidentCode = forwardedProps.incidentCode;
  const restore = forwardedProps.restore === true;
  const approvalDecision =
    forwardedProps.approvalDecision && typeof forwardedProps.approvalDecision === "object"
      ? (forwardedProps.approvalDecision as { approvalId?: string; decision?: Decision })
      : null;

  if (restore && approvalDecision) {
    return NextResponse.json({ detail: "restore와 approvalDecision은 함께 보낼 수 없습니다." }, { status: 400 });
  }

  const thread = getOrResetThread(threadId, incidentCode);

  if (approvalDecision) {
    const decision = approvalDecision.decision;
    const approvalId = approvalDecision.approvalId;
    if (!approvalId || (decision !== "APPROVED" && decision !== "REJECTED")) {
      return NextResponse.json({ detail: "approvalDecision 형식이 올바르지 않습니다." }, { status: 400 });
    }
    if (!thread.snapshot.approval || thread.snapshot.approval.approvalId !== approvalId) {
      return NextResponse.json({ detail: "승인 요청을 찾을 수 없습니다." }, { status: 404 });
    }
    if (thread.snapshot.approval.decision !== "PENDING") {
      return NextResponse.json({ detail: "이미 처리된 승인 요청입니다." }, { status: 409 });
    }
  }

  const incomingMessages = Array.isArray(body.messages) ? (body.messages as ChatMessageRecord[]) : [];
  const lastUserMessage = [...incomingMessages].reverse().find((msg) => msg.role === "user");

  const stream = new ReadableStream<Uint8Array>({
    async start(controller) {
      const send = (event: Record<string, unknown>) => {
        if (request.signal.aborted) return;
        controller.enqueue(sseFrame(event));
      };

      try {
        send({ type: "RUN_STARTED", threadId, runId });

        if (restore) {
          send({ type: "STATE_SNAPSHOT", snapshot: thread.snapshot });
          send({ type: "MESSAGES_SNAPSHOT", messages: thread.messages });
          send({ type: "RUN_FINISHED", threadId, runId });
          controller.close();
          return;
        }

        if (approvalDecision && approvalDecision.decision && approvalDecision.approvalId) {
          const decision = approvalDecision.decision;
          const approvalId = approvalDecision.approvalId;
          const currentApproval = thread.snapshot.approval!;

          if (decision === "REJECTED") {
            await delay(140);
            const rejectedSnapshot: Snapshot = {
              ...thread.snapshot,
              workflowStatus: "REJECTED",
              approval: { ...currentApproval, decision: "REJECTED" },
            };
            thread.snapshot = rejectedSnapshot;
            send({ type: "STATE_SNAPSHOT", snapshot: rejectedSnapshot });

            const msgId = `msg-a-${runId}`;
            const text = `승인 요청(${approvalId})을 반려했습니다. 조치를 실행하지 않았으며 조건을 바꿔 다시 요청할 수 있습니다.`;
            send({ type: "TEXT_MESSAGE_START", messageId: msgId, role: "assistant" });
            send({ type: "TEXT_MESSAGE_CONTENT", messageId: msgId, delta: text });
            send({ type: "TEXT_MESSAGE_END", messageId: msgId });
            thread.messages.push({ id: msgId, role: "assistant", content: text });

            send({ type: "RUN_FINISHED", threadId, runId });
            controller.close();
            return;
          }

          const waitingSnapshot: Snapshot = {
            ...thread.snapshot,
            workflowStatus: "AWAITING_EXECUTION_RESULT",
            approval: { ...currentApproval, decision: "APPROVED" },
          };
          thread.snapshot = waitingSnapshot;
          send({ type: "STATE_SNAPSHOT", snapshot: waitingSnapshot });
          send({ type: "STEP_STARTED", stepName: "execute_approved" });

          await delay(240);
          if (request.signal.aborted) {
            controller.close();
            return;
          }

          send({ type: "STEP_FINISHED", stepName: "execute_approved" });
          const resolvedSnapshot: Snapshot = {
            ...waitingSnapshot,
            workflowStatus: "RESOLVED",
          };
          thread.snapshot = resolvedSnapshot;
          send({ type: "STATE_SNAPSHOT", snapshot: resolvedSnapshot });

          const msgId = `msg-a-${runId}`;
          const text = `승인(${approvalId})된 롤백 조치를 실행하고 복구 기준과 포스트모템을 반영했습니다. 장애 상태: RESOLVED`;
          send({ type: "TEXT_MESSAGE_START", messageId: msgId, role: "assistant" });
          send({ type: "TEXT_MESSAGE_CONTENT", messageId: msgId, delta: text });
          send({ type: "TEXT_MESSAGE_END", messageId: msgId });
          thread.messages.push({ id: msgId, role: "assistant", content: text });

          send({ type: "RUN_FINISHED", threadId, runId });
          controller.close();
          return;
        }

        const userText = lastUserMessage?.content ?? "";
        if (userText) {
          thread.messages.push({
            id: lastUserMessage?.id || `msg-u-${runId}`,
            role: "user",
            content: userText,
          });
        }

        if (userText.includes("오류 테스트") || userText.includes("실패 테스트")) {
          await delay(120);
          send({
            type: "RUN_ERROR",
            message: "외부 지표 조회 중 일시 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.",
            code: "MOCK_UPSTREAM_ERROR",
          });
          controller.close();
          return;
        }

        send({ type: "STEP_STARTED", stepName: "analyze" });
        send({ type: "STATE_SNAPSHOT", snapshot: { ...thread.snapshot, workflowStatus: "PLANNING" } });
        await delay(120);
        send({ type: "STEP_FINISHED", stepName: "analyze" });

        send({ type: "STEP_STARTED", stepName: "execute" });

        const toolSequence = [
          {
            id: `${runId}-log`,
            name: "log-analyzer-agent",
            args: { incidentCode },
            result: { ok: true, pulledCount: 2, chunkIds: ["LOG-901", "RUNBOOK-01"] },
          },
          {
            id: `${runId}-gh`,
            name: "list_rollback_candidates",
            args: { serviceName: "payment-service" },
            result: { source: "GitHub Deployments", summary: "현재 배포 v42의 이전 성공 버전 v41을 찾았습니다." },
          },
          {
            id: `${runId}-graf`,
            name: "get_release_metrics",
            args: { versions: ["v42", "v41"] },
            result: { source: "Grafana Cloud", summary: "v42의 502 비율은 18%, v41은 0.3%로 기준인 1% 미만입니다." },
          },
          {
            id: `${runId}-cal`,
            name: "get_change_freeze_windows",
            args: { range: "14:28-14:50" },
            result: { source: "Google Calendar", summary: "14:30–14:40은 변경 금지 구간입니다." },
          },
        ];

        for (const tc of toolSequence) {
          if (request.signal.aborted) {
            controller.close();
            return;
          }
          send({ type: "TOOL_CALL_START", toolCallId: tc.id, toolCallName: tc.name });
          send({ type: "TOOL_CALL_ARGS", toolCallId: tc.id, delta: JSON.stringify(tc.args) });
          send({ type: "TOOL_CALL_END", toolCallId: tc.id });
          await delay(140);
          send({
            type: "TOOL_CALL_RESULT",
            messageId: `msg-tc-${tc.id}`,
            toolCallId: tc.id,
            role: "tool",
            content: JSON.stringify(tc.result),
          });
        }
        send({ type: "STEP_FINISHED", stepName: "execute" });

        send({ type: "STEP_STARTED", stepName: "evaluate" });
        const replan: ReplanRecord = {
          reason: "기존 작업 창이 변경 금지 시간과 겹칩니다.",
          previousWindow,
          nextWindow,
        };
        send({ type: "CUSTOM", name: "replan", value: replan });

        const awaitingSnapshot: Snapshot = {
          incidentCode,
          workflowStatus: "AWAITING_APPROVAL",
          plan: demoPlan,
          approval: {
            approvalId: `APR-${incidentCode}-01`,
            planId: demoPlan.planId,
            stepNo: 1,
            decision: "PENDING",
          },
          replanHistory: [replan],
        };
        thread.snapshot = awaitingSnapshot;
        send({ type: "STEP_FINISHED", stepName: "evaluate" });
        send({ type: "STATE_SNAPSHOT", snapshot: awaitingSnapshot });

        const msgId = `msg-a-${runId}`;
        const reply =
          "이전 버전 v41의 502 비율이 0.3%로 기준(1% 미만)을 충족했습니다. 변경 금지 구간(14:30–14:40)을 피해 14:40–14:45 작업 창으로 롤백 계획을 등록했으니 검토 후 승인 또는 반려해 주세요.";
        send({ type: "TEXT_MESSAGE_START", messageId: msgId, role: "assistant" });
        send({ type: "TEXT_MESSAGE_CONTENT", messageId: msgId, delta: reply });
        send({ type: "TEXT_MESSAGE_END", messageId: msgId });
        thread.messages.push({ id: msgId, role: "assistant", content: reply });

        send({ type: "RUN_FINISHED", threadId, runId });
        controller.close();
      } catch {
        try {
          send({ type: "RUN_ERROR", message: "모의 AG-UI 스트림 처리 중 오류가 발생했습니다." });
        } finally {
          controller.close();
        }
      }
    },
  });

  return new Response(stream, {
    status: 200,
    headers: {
      "Content-Type": "text/event-stream; charset=utf-8",
      "Cache-Control": "no-cache, no-transform",
      Connection: "keep-alive",
    },
  });
}
