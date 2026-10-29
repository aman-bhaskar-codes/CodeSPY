const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function selectedInvestigation(repositoryId: string, snapshotId?: string) {
  const url = new URL(window.location.href);
  const value = url.searchParams.get("investigation");
  return url.searchParams.get("repository") === repositoryId && (!snapshotId || url.searchParams.get("snapshot") === snapshotId) && value && uuid.test(value) ? value : null;
}

export function rememberInvestigation(id: string) {
  if (!uuid.test(id)) return;
  const url = new URL(window.location.href);
  url.searchParams.set("investigation", id);
  window.history.replaceState(window.history.state, "", url);
}
