import { resolveInitialMode } from "./ag-ui-client";
import {
  fetchIncidentDetailFromBackend,
  fetchIncidentsFromBackend,
  toIncidentListItem,
  type BackendIncidentDetail,
  type IncidentListItem,
} from "./backend-api";
import { demoIncidentList } from "./demo";
import { Workspace } from "./workspace";

export const dynamic = "force-dynamic";

export default async function Page({
  searchParams,
}: {
  searchParams: Promise<{ incident?: string | string[]; mode?: string | string[] }>;
}) {
  const { incident, mode } = await searchParams;
  const incidentParam = Array.isArray(incident) ? incident[0] : incident;
  const modeParam = Array.isArray(mode) ? mode[0] : mode;
  const initialMode = resolveInitialMode(modeParam, process.env.NEXT_PUBLIC_AG_UI_MODE);

  if (initialMode === "mock") {
    const selected =
      demoIncidentList.find((entry) => entry.incidentCode === incidentParam) ?? demoIncidentList[0];
    return (
      <Workspace
        key={`${selected.incidentCode}:mock`}
        incident={selected}
        incidents={demoIncidentList}
        initialDetail={null}
        initialMode="mock"
        backendError={null}
      />
    );
  }

  const listResult = await fetchIncidentsFromBackend();
  if (!listResult.ok) {
    const fallback =
      demoIncidentList.find((entry) => entry.incidentCode === incidentParam) ?? demoIncidentList[0];
    return (
      <Workspace
        key={`${fallback.incidentCode}:live-error`}
        incident={fallback}
        incidents={demoIncidentList}
        initialDetail={null}
        initialMode="live"
        backendError={listResult.message}
      />
    );
  }

  const incidentItems: IncidentListItem[] = listResult.data.map(toIncidentListItem);
  if (incidentItems.length === 0) {
    return (
      <Workspace
        key="empty:live"
        incident={null}
        incidents={[]}
        initialDetail={null}
        initialMode="live"
        backendError={null}
      />
    );
  }

  const targetCode = incidentParam || incidentItems[0].incidentCode;
  const detailResult = await fetchIncidentDetailFromBackend(targetCode);

  let selected: IncidentListItem | null =
    incidentItems.find((entry) => entry.incidentCode === targetCode) ?? null;
  let initialDetail: BackendIncidentDetail | null = null;
  let backendError: string | null = null;

  if (detailResult.ok) {
    initialDetail = detailResult.data;
    selected = toIncidentListItem(detailResult.data.incident);
  } else {
    backendError = detailResult.message;
    if (!selected) {
      selected = incidentItems[0];
    }
  }

  return (
    <Workspace
      key={`${selected?.incidentCode ?? "none"}:live`}
      incident={selected}
      incidents={incidentItems}
      initialDetail={initialDetail}
      initialMode="live"
      backendError={backendError}
    />
  );
}
