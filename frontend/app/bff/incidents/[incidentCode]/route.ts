import { NextResponse } from "next/server";
import { fetchIncidentDetailFromBackend } from "../../../backend-api";

export async function GET(
  _request: Request,
  context: { params: Promise<{ incidentCode: string }> },
) {
  const { incidentCode } = await context.params;
  if (!incidentCode || !incidentCode.trim()) {
    return NextResponse.json({ message: "장애 코드가 필요합니다." }, { status: 400 });
  }

  const result = await fetchIncidentDetailFromBackend(incidentCode.trim());
  if (!result.ok) {
    return NextResponse.json({ message: result.message }, { status: result.status });
  }

  return NextResponse.json(result.data, {
    status: 200,
    headers: { "Cache-Control": "no-store" },
  });
}
