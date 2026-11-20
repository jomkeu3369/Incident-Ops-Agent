import { NextResponse } from "next/server";
import { mockAnalyzeSessionAnomalies } from "../../../mock-store";

export async function GET(
  _request: Request,
  context: { params: Promise<{ sessionId: string }> },
) {
  const { sessionId } = await context.params;
  const report = mockAnalyzeSessionAnomalies(sessionId);
  if (!report) {
    return NextResponse.json({ detail: `실행 로그를 찾을 수 없습니다: ${sessionId}` }, { status: 404 });
  }
  return NextResponse.json(report, { status: 200, headers: { "Cache-Control": "no-store" } });
}
