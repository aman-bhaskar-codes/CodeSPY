import { useQuery } from "@tanstack/react-query";
import { api } from "../../api/client";
import { Button } from "../../ui/Button";
import { StatusBadge } from "../../ui/StatusBadge";

export function ModelAvailabilityPanel({
  repositoryId,
}: {
  repositoryId: string;
}) {
  const models = useQuery({
    queryKey: ["model-availability", repositoryId],
    queryFn: ({ signal }) => api.modelAvailability(repositoryId, signal),
    enabled: !!repositoryId,
    retry: false,
  });
  return (
    <section aria-labelledby="model-availability-heading">
      <h3 id="model-availability-heading">Model availability</h3>
      <p className="muted">
        Model routing and disclosure policies are managed by the server. This
        view makes no provider calls.
      </p>
      {models.isPending && <p role="status">Loading model configuration…</p>}
      {models.isError && (
        <div role="alert">
          Model configuration is unavailable.{" "}
          <Button onClick={() => void models.refetch()}>
            Retry model status
          </Button>
        </div>
      )}
      {models.data && (
        <>
          {!models.data.live_access_verified && (
            <p>
              Live access is not verified. Configuration status does not prove a
              provider call will succeed.
            </p>
          )}
          {!models.data.profiles?.length && (
            <p>No model profiles are configured for this repository.</p>
          )}
          <ul className="model-profiles">
            {models.data.profiles?.map((profile) => (
              <li key={profile.alias}>
                <div>
                  <strong>{profile.alias}</strong>
                  <span className="muted">{profile.provider}</span>
                </div>
                <StatusBadge tone={profile.eligible ? "neutral" : "warning"}>
                  {profile.status.replaceAll("_", " ")}
                </StatusBadge>
                <span>
                  {profile.eligible
                    ? "Eligible for server routing"
                    : "Unavailable for routing"}
                </span>
                <span className="muted">
                  Tools:{" "}
                  {profile.supports_tools ? "supported" : "not supported"}
                </span>
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}
