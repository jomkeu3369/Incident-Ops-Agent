import { HttpAgent, type AgentSubscriber, type Message } from "@ag-ui/client";
import type { ChatMessageRecord, ForwardedProps, ReplanRecord } from "./ag-ui-contract";
import type { WorkspaceAction } from "./workspace-state";

export type AgUiMode = "live" | "mock";

export function resolveAgUiUrl(mode: AgUiMode): string {
  if (mode === "mock") return "/api/mock-ag-ui";
  return process.env.NEXT_PUBLIC_AG_UI_URL || "/api/ag-ui";
}

export type AgUiRunRequest = {
  url: string;
  threadId: string;
  runId: string;
  userMessage?: { id: string; content: string };
  forwardedProps: ForwardedProps;
  signal: AbortSignal;
  dispatch: (action: WorkspaceAction) => void;
};

export type AgUiRunOutcome =
  | { ok: true }
  | { ok: false; aborted: boolean; statusCode?: number; message: string; needsResync: boolean };

function extractErrorDetail(bodyText: string): string | null {
  if (!bodyText) return null;
  try {
    const parsed = JSON.parse(bodyText) as Record<string, unknown>;
    if (typeof parsed.detail === "string" && parsed.detail.trim()) return parsed.detail.trim();
    if (typeof parsed.message === "string" && parsed.message.trim()) return parsed.message.trim();
  } catch {
    // plain text fallback
  }
  return null;
}

function isReplanRecord(value: unknown): value is ReplanRecord {
  if (!value || typeof value !== "object") return false;
  const obj = value as Record<string, unknown>;
  return (
    typeof obj.reason === "string" &&
    Boolean(obj.previousWindow) &&
    typeof obj.previousWindow === "object" &&
    Boolean(obj.nextWindow) &&
    typeof obj.nextWindow === "object"
  );
}

function mapSnapshotMessages(messages: Message[]): ChatMessageRecord[] {
  return messages
    .filter((msg) => msg.role === "user" || msg.role === "assistant")
    .map((msg) => ({
      id: msg.id,
      role: msg.role as "user" | "assistant",
      content: typeof msg.content === "string" ? msg.content : "",
    }));
}

export async function runAgUiRequest({
  url,
  threadId,
  runId,
  userMessage,
  forwardedProps,
  signal,
  dispatch,
}: AgUiRunRequest): Promise<AgUiRunOutcome> {
  const abortController = new AbortController();
  let agentRef: HttpAgent | null = null;
  const onExternalAbort = () => {
    abortController.abort();
    agentRef?.abortRun();
  };
  if (signal.aborted) {
    return { ok: false, aborted: true, message: "", needsResync: false };
  }
  signal.addEventListener("abort", onExternalAbort, { once: true });

  let httpStatus: number | undefined;
  let httpErrorDetail: string | null = null;
  let receivedRunError = false;

  const instrumentedFetch: typeof fetch = async (input, init) => {
    const response = await fetch(input, {
      ...init,
      signal: abortController.signal,
    });
    if (!response.ok) {
      httpStatus = response.status;
      try {
        const cloned = response.clone();
        httpErrorDetail = extractErrorDetail(await cloned.text());
      } catch {
        httpErrorDetail = null;
      }
    }
    return response;
  };

  const initialMessages: Message[] = userMessage
    ? [{ id: userMessage.id, role: "user", content: userMessage.content }]
    : [];

  const agent = new HttpAgent({
    url,
    threadId,
    initialMessages,
    initialState: {},
    fetch: instrumentedFetch,
  });
  agentRef = agent;

  const isStale = () => signal.aborted || abortController.signal.aborted;

  const subscriber: AgentSubscriber = {
    onRunStartedEvent() {
      if (isStale()) return;
      dispatch({ type: "RUN_STARTED" });
    },
    onRunFinishedEvent() {
      if (isStale()) return;
      dispatch({ type: "RUN_FINISHED" });
    },
    onRunErrorEvent({ event }) {
      if (isStale()) return;
      receivedRunError = true;
      dispatch({
        type: "RUN_ERROR",
        message: event.message || "에이전트 실행 중 오류가 발생했습니다.",
        needsResync: Boolean(forwardedProps.approvalDecision),
      });
    },
    onStepStartedEvent({ event }) {
      if (isStale()) return;
      dispatch({ type: "STEP_STARTED", stepName: event.stepName });
    },
    onStepFinishedEvent({ event }) {
      if (isStale()) return;
      dispatch({ type: "STEP_FINISHED", stepName: event.stepName });
    },
    onToolCallStartEvent({ event }) {
      if (isStale()) return;
      dispatch({
        type: "TOOL_CALL_START",
        toolCallId: event.toolCallId,
        toolCallName: event.toolCallName,
      });
    },
    onToolCallResultEvent({ event }) {
      if (isStale()) return;
      const content = typeof event.content === "string" ? event.content : JSON.stringify(event.content);
      dispatch({
        type: "TOOL_CALL_RESULT",
        toolCallId: event.toolCallId,
        messageId: event.messageId,
        content,
      });
    },
    onTextMessageStartEvent({ event }) {
      if (isStale()) return;
      dispatch({
        type: "TEXT_MESSAGE_START",
        messageId: event.messageId,
        role: event.role ?? "assistant",
      });
    },
    onTextMessageContentEvent({ event }) {
      if (isStale()) return;
      dispatch({
        type: "TEXT_MESSAGE_CONTENT",
        messageId: event.messageId,
        delta: event.delta,
      });
    },
    onTextMessageEndEvent({ event }) {
      if (isStale()) return;
      dispatch({
        type: "TEXT_MESSAGE_END",
        messageId: event.messageId,
      });
    },
    onStateSnapshotEvent({ event }) {
      if (isStale()) return;
      dispatch({ type: "STATE_SNAPSHOT", snapshot: event.snapshot });
    },
    onMessagesSnapshotEvent({ event }) {
      if (isStale()) return;
      dispatch({
        type: "MESSAGES_SNAPSHOT",
        messages: mapSnapshotMessages(event.messages),
      });
    },
    onCustomEvent({ event }) {
      if (isStale()) return;
      if (event.name === "replan" && isReplanRecord(event.value)) {
        dispatch({ type: "CUSTOM_REPLAN", value: event.value });
      }
    },
  };

  try {
    await agent.runAgent(
      {
        runId,
        tools: [],
        context: [],
        forwardedProps,
        abortController,
      },
      subscriber,
    );
    if (isStale()) {
      return { ok: false, aborted: true, message: "", needsResync: false };
    }
    if (receivedRunError) {
      return {
        ok: false,
        aborted: false,
        message: "에이전트 실행 중 오류가 발생했습니다.",
        needsResync: Boolean(forwardedProps.approvalDecision),
      };
    }
    return { ok: true };
  } catch {
    if (isStale()) {
      return { ok: false, aborted: true, message: "", needsResync: false };
    }
    if (isStale()) {
      return { ok: false, aborted: true, message: "", needsResync: false };
    }
    if (receivedRunError) {
      return {
        ok: false,
        aborted: false,
        message: "에이전트 실행 중 오류가 발생했습니다.",
        needsResync: Boolean(forwardedProps.approvalDecision),
      };
    }

    let message = "AG-UI 스트림 연결이 중단되었습니다. 다시 조회해 상태를 확인해 주세요.";
    let needsResync = true;

    if (httpStatus === 409) {
      message = httpErrorDetail || "이미 처리된 승인 요청입니다. 최신 상태를 다시 조회해 주세요.";
      needsResync = true;
    } else if (httpStatus === 400) {
      message = httpErrorDetail || "요청 형식이 올바르지 않습니다. 입력을 확인해 주세요.";
      needsResync = false;
    } else if (httpStatus === 401 || httpStatus === 403) {
      message = "서버 인증 오류가 발생했습니다. 관리자에게 확인해 주세요.";
      needsResync = false;
    } else if (httpStatus === 404) {
      message = "AG-UI 엔드포인트를 찾을 수 없습니다. 서버 연결을 확인하거나 모의 SSE 모드로 전환해 주세요.";
      needsResync = false;
    } else if (httpStatus && httpStatus >= 500) {
      message = "에이전트 서버에서 일시 오류가 발생했습니다. 잠시 후 다시 조회해 주세요.";
      needsResync = true;
    }

    dispatch({ type: "RUN_ERROR", message, needsResync });
    return { ok: false, aborted: false, statusCode: httpStatus, message, needsResync };
  } finally {
    signal.removeEventListener("abort", onExternalAbort);
  }
}
