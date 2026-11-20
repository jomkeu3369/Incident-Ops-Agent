import { NextResponse } from "next/server";
import type { ManagedPlanCreateInput } from "../../../ops-admin-client";
import { mockCreatePlan, mockListPlans } from "../mock-store";

const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
const TIME_RE = /^\d{2}:\d{2}$/;

export async function GET() {
  return NextResponse.json(mockListPlans(), { status: 200, headers: { "Cache-Control": "no-store" } });
}

export async function POST(request: Request) {
  let body: Partial<ManagedPlanCreateInput>;
  try {
    body = (await request.json()) as Partial<ManagedPlanCreateInput>;
  } catch {
    return NextResponse.json({ detail: "JSON 본문을 읽을 수 없습니다." }, { status: 400 });
  }

  if (
    !body.incidentCode?.trim() ||
    !body.title?.trim() ||
    !body.planDate ||
    !DATE_RE.test(body.planDate) ||
    !body.startTime ||
    !TIME_RE.test(body.startTime) ||
    !body.endTime ||
    !TIME_RE.test(body.endTime)
  ) {
    return NextResponse.json({ detail: "장애 코드, 제목, 날짜(YYYY-MM-DD), 시작·종료 시각(HH:MM)은 필수입니다." }, { status: 400 });
  }
  if (body.startTime >= body.endTime) {
    return NextResponse.json({ detail: "시작 시각은 종료 시각보다 이전이어야 합니다." }, { status: 400 });
  }

  const created = mockCreatePlan({
    planId: body.planId,
    incidentCode: body.incidentCode,
    title: body.title,
    serviceName: body.serviceName || "payment-service",
    planDate: body.planDate,
    startTime: body.startTime,
    endTime: body.endTime,
    actionCommand: body.actionCommand ?? null,
    evidenceChunkIds: Array.isArray(body.evidenceChunkIds) ? body.evidenceChunkIds : [],
    notes: body.notes ?? "",
  });
  return NextResponse.json(created, { status: 201 });
}
