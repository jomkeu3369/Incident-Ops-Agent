import { NextResponse } from "next/server";
import type { ManagedPlanUpdateInput } from "../../../../ops-admin-client";
import { mockDeletePlan, mockGetPlan, mockUpdatePlan } from "../../mock-store";

export async function GET(
  _request: Request,
  context: { params: Promise<{ planId: string }> },
) {
  const { planId } = await context.params;
  const item = mockGetPlan(planId);
  if (!item) {
    return NextResponse.json({ detail: `계획을 찾을 수 없습니다: ${planId}` }, { status: 404 });
  }
  return NextResponse.json(item, { status: 200 });
}

export async function PATCH(
  request: Request,
  context: { params: Promise<{ planId: string }> },
) {
  const { planId } = await context.params;
  let body: ManagedPlanUpdateInput;
  try {
    body = (await request.json()) as ManagedPlanUpdateInput;
  } catch {
    return NextResponse.json({ detail: "JSON 본문을 읽을 수 없습니다." }, { status: 400 });
  }

  const res = mockUpdatePlan(planId, body);
  if (!res.ok) {
    return NextResponse.json({ detail: res.message }, { status: res.status });
  }
  return NextResponse.json(res.data, { status: 200 });
}

export async function DELETE(
  _request: Request,
  context: { params: Promise<{ planId: string }> },
) {
  const { planId } = await context.params;
  if (!mockDeletePlan(planId)) {
    return NextResponse.json({ detail: `계획을 찾을 수 없습니다: ${planId}` }, { status: 404 });
  }
  return new Response(null, { status: 204 });
}
