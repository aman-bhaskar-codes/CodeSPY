import { Modal } from "../../../ui/Modal";

import { useInvestigations } from "../investigationsContext";

/** Modal that confirms cancelling the running investigation. */
export function CancelConfirmation() {
  const { cancelMutation, cancelIntent, setCancelIntent } = useInvestigations();
  return (
    <>
      <Modal
        open={!!cancelIntent}
        title="Cancel investigation"
        onClose={() => setCancelIntent(null)}
        busy={cancelMutation.isPending}
        description="This stops further work. A model call already sent may still settle usage."
      >
        <button
          type="button"
          disabled={cancelMutation.isPending}
          onClick={() => cancelMutation.mutate(cancelIntent!)}
        >
          Confirm cancellation
        </button>
        <button
          type="button"
          disabled={cancelMutation.isPending}
          onClick={() => setCancelIntent(null)}
        >
          Keep running
        </button>
      </Modal>
    </>
  );
}
