import { InlineNotice } from "../../../ui/InlineNotice";
import { operationProblem } from "../investigationTypes";
import { useInvestigations } from "../investigationsContext";

/** Failed commands and failed reads, as persistent inline notices. */
export function InvestigationNotices() {
  const {
    listQuery,
    detailQuery,
    eventsQuery,
    createMutation,
    executeMutation,
    cancelMutation,
    resumeMutation,
    retryMutation,
    feedbackMutation,
  } = useInvestigations();
  return (
    <>
      {[
        createMutation,
        executeMutation,
        cancelMutation,
        resumeMutation,
        retryMutation,
        feedbackMutation,
      ]
        .filter((mutation) => mutation.isError)
        .map((mutation, index) => (
          <InlineNotice key={index} tone="danger">
            {operationProblem(mutation.error)}
          </InlineNotice>
        ))}
      {listQuery.isError && (
        <InlineNotice tone="danger">
          Investigations could not be loaded.{" "}
          <button type="button" onClick={() => void listQuery.refetch()}>
            Retry investigations
          </button>
        </InlineNotice>
      )}
      {detailQuery.isError && (
        <InlineNotice tone="danger">
          {operationProblem(detailQuery.error)}{" "}
          <button type="button" onClick={() => void detailQuery.refetch()}>
            Reload investigation
          </button>
        </InlineNotice>
      )}
      {eventsQuery.isError && (
        <InlineNotice tone="danger">
          The event timeline could not be refreshed.{" "}
          <button type="button" onClick={() => void eventsQuery.refetch()}>
            Retry timeline
          </button>
        </InlineNotice>
      )}
    </>
  );
}
