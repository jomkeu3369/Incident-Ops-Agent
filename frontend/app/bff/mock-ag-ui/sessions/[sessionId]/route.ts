import { NextResponse } from "next/server";
import { mockDeleteSession, mockGetSessionEvents } from "../../mock-store";

export async function GET(
  _request: Request,
  context: { params: Promise<{ sessionId: string }> },
) {
  const { sessionId } = await context.params;
  const events = mockGetSessionEvents(sessionId);
  if (!events || events.length === 0) {
    return NextResponse.json({ detail: `실행 로그를 찾을 수 없습니다: ${sessionId}` }, { status: 404 });
  }
  return NextResponse.json({ sessionId, events }, { status: 200, headers: { "Cache-Control": "no-store" } });
}

export async function DELETE(
  _request: Request,
  context: { params: Promise<{ sessionId: string }> },
) {
  const { sessionId } = await context.params;
  if (!mockDeleteSession(sessionId)) {
    return NextResponse.json({ detail: `실행 로그를 찾을 수 없습니다: ${sessionId}` }, { status: 404 });
  }
  return new Response(null, { status: 204 });
}
