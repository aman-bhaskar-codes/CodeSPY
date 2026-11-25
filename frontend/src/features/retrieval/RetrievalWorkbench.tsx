import * as Tabs from "@radix-ui/react-tabs";
import { InlineNotice } from "../../ui/InlineNotice";
import { IndexBuildPanel } from "./IndexBuildPanel";
import { ContextTab } from "./parts/ContextTab";
import { MemoryTab } from "./parts/MemoryTab";
import { RetrievalHeading } from "./parts/RetrievalHeading";
import { SearchTab } from "./parts/SearchTab";
import { SourceEvidenceModal } from "./parts/SourceEvidenceModal";
import { RetrievalContext } from "./retrievalContext";
import {
  useRetrievalController,
  type RetrievalWorkbenchProps,
} from "./useRetrievalController";

export function RetrievalWorkbench(props: RetrievalWorkbenchProps) {
  return (
    <RetrievalWorkbenchState
      key={`${props.repositoryId}:${props.snapshotId}`}
      {...props}
    />
  );
}

/**
 * Search, context and memory for one snapshot. State and commands live in the controller hook;
 * each tab is a part that reads them from context.
 */
function RetrievalWorkbenchState(props: RetrievalWorkbenchProps) {
  const controller = useRetrievalController(props);
  const { activeTab, setActiveTab, copyStatus, selectedFileForView } =
    controller;
  return (
    <RetrievalContext value={controller}>
      <Tabs.Root
        value={activeTab}
        onValueChange={(value) => {
          if (value === "search" || value === "context" || value === "memory")
            setActiveTab(value);
        }}
        asChild
      >
        <section aria-labelledby="retrieval-title" style={{ marginTop: 24 }}>
          <RetrievalHeading />
          {copyStatus && <InlineNotice>{copyStatus}</InlineNotice>}
          <IndexBuildPanel
            repositoryId={props.repositoryId}
            snapshotId={props.snapshotId}
            csrfToken={props.csrfToken}
          />
          {activeTab === "search" && <SearchTab />}
          {activeTab === "context" && <ContextTab />}
          {activeTab === "memory" && <MemoryTab />}
          {selectedFileForView && <SourceEvidenceModal />}
        </section>
      </Tabs.Root>
    </RetrievalContext>
  );
}
