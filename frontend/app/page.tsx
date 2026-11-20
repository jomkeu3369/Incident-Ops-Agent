import { Workspace } from "./workspace";
import { incidents } from "./demo";
import { resolveInitialMode } from "./ag-ui-client";

export default async function Page({
  searchParams,
}: {
  searchParams: Promise<{ incident?: string | string[]; mode?: string | string[] }>;
}) {
  const { incident, mode } = await searchParams;
  const incidentParam = Array.isArray(incident) ? incident[0] : incident;
  const modeParam = Array.isArray(mode) ? mode[0] : mode;
  const selected = incidents.find((entry) => entry.incidentCode === incidentParam) ?? incidents[0];
  const initialMode = resolveInitialMode(modeParam, process.env.NEXT_PUBLIC_AG_UI_MODE);

  return <Workspace key={`${selected.incidentCode}:${initialMode}`} incident={selected} initialMode={initialMode} />;
}
