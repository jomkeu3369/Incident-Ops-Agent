import { Workspace } from "./workspace";
import { incidents } from "./demo";

export default async function Page({
  searchParams,
}: {
  searchParams: Promise<{ incident?: string | string[] }>;
}) {
  const { incident } = await searchParams;
  const selected = incidents.find((entry) => entry.incidentCode === incident) ?? incidents[0];

  return <Workspace key={selected.incidentCode} incident={selected} />;
}
