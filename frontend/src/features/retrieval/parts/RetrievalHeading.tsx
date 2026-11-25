import * as Tabs from "@radix-ui/react-tabs";
import { useRetrieval } from "../retrievalContext";

/** Section heading and the tab list. */
export function RetrievalHeading() {
  const { activeTab } = useRetrieval();
  return (
    <>
      <div className="section-heading" style={{ marginBottom: 18 }}>
        <div>
          <h2 id="retrieval-title" tabIndex={-1}>
            Search, context & memory
          </h2>
          <span className="muted">
            Search published source, assemble bounded context and retain cited
            evidence
          </span>
        </div>
        <Tabs.List
          aria-label="Source workbench views"
          className="workbench-tabs"
        >
          <Tabs.Trigger
            value="search"
            className={activeTab === "search" ? "primary" : ""}
          >
            Code search
          </Tabs.Trigger>
          <Tabs.Trigger
            value="context"
            className={activeTab === "context" ? "primary" : ""}
          >
            Context
          </Tabs.Trigger>
          <Tabs.Trigger
            value="memory"
            className={activeTab === "memory" ? "primary" : ""}
          >
            Memory
          </Tabs.Trigger>
        </Tabs.List>
      </div>
    </>
  );
}
