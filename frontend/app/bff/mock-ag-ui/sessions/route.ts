import { NextResponse } from "next/server";
import { mockListSessions } from "../mock-store";

export async function GET() {
  return NextResponse.json(mockListSessions(), { status: 200, headers: { "Cache-Control": "no-store" } });
}
