// Moved verbatim from the workspace composition; behaviour is unchanged.
import { label } from "../problems";
import { useWorkspace } from "../workspaceContext";
import type { SnapshotPaneProps } from "./types";
export function InventoryPane({ snapshot }: SnapshotPaneProps) {
  const { inventory, selection } = useWorkspace();
  const { pages, setPages } = selection;
  return (
    <>
      <section aria-labelledby="inventory-title">
        <div className="section-heading">
          <h2 id="inventory-title" tabIndex={-1}>
            File inventory
          </h2>
          <span className="muted">Byte evidence · no semantic analysis</span>
        </div>
        {!snapshot && <p>The inventory appears after a source is admitted.</p>}
        {snapshot && inventory.isPending && (
          <p role="status">Loading inventory…</p>
        )}
        {inventory.isError && (
          <p role="alert">
            Inventory could not be loaded.{" "}
            <button onClick={() => void inventory.refetch()}>
              Reload inventory
            </button>
          </p>
        )}
        {inventory.data && (
          <>
            <p>
              {inventory.data.items.length} files on this page. Eligibility
              describes admission policy, not analysis coverage.
            </p>
            {inventory.data.items.length === 0 ? (
              <p>This snapshot contains no regular files.</p>
            ) : (
              <div
                className="table-scroll"
                tabIndex={0}
                role="region"
                aria-label="File inventory table"
              >
                <table>
                  <thead>
                    <tr>
                      <th scope="col">Path</th>
                      <th scope="col">Bytes</th>
                      <th scope="col">SHA-256</th>
                      <th scope="col">Eligibility</th>
                    </tr>
                  </thead>
                  <tbody>
                    {inventory.data.items.map((entry) => (
                      <tr key={entry.id}>
                        <th scope="row">{entry.path}</th>
                        <td>{entry.size_bytes.toLocaleString("en-US")}</td>
                        <td className="hash">{entry.sha256}</td>
                        <td>
                          {entry.eligible
                            ? "Eligible"
                            : `Excluded: ${label(entry.exclusion_reason ?? "unspecified")}`}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            <nav aria-label="Inventory pages">
              <button
                disabled={pages.length < 2 || inventory.isFetching}
                onClick={() => setPages((current) => current.slice(0, -1))}
              >
                Previous page
              </button>
              <span>Page {pages.length}</span>
              <button
                disabled={!inventory.data.next_cursor || inventory.isFetching}
                onClick={() =>
                  setPages((current) => [
                    ...current,
                    inventory.data.next_cursor ?? null,
                  ])
                }
              >
                Next page
              </button>
            </nav>
          </>
        )}
      </section>
    </>
  );
}
