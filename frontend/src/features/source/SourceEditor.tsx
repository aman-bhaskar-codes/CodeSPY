import { useQuery } from "@tanstack/react-query";
import { api } from "../../api/client";
import { InlineNotice } from "../../ui/InlineNotice";
import { StatusBadge } from "../../ui/StatusBadge";
import { useOpenFiles } from "./useOpenFiles";
import styles from "./SourceEditor.module.css";

interface Props {
  repositoryId: string;
  snapshotId: string;
}

/** Read-only view of one file through the redacted source-view contract: line numbers, no editing. */
export function SourceEditor({ repositoryId, snapshotId }: Props) {
  const files = useOpenFiles((s) => s.files);
  const activeId = useOpenFiles((s) => s.activeId);
  const active = files.find((f) => f.fileId === activeId);
  const view = useQuery({
    queryKey: ["source-editor", repositoryId, snapshotId, activeId],
    queryFn: ({ signal }) =>
      api.sourceView(
        repositoryId,
        snapshotId,
        activeId!,
        { representation: "model_safe" },
        signal,
      ),
    enabled: !!activeId,
    retry: false,
  });
  if (!active) {
    return (
      <p className="muted">
        Select a file in the explorer to read it here. Source is shown read-only
        and with sensitive values redacted.
      </p>
    );
  }
  const data = view.data;
  const lines = (data?.content_text ?? "").replace(/\n$/, "").split("\n");
  return (
    <div className={styles.editor}>
      <p className={styles.path}>
        <code>{active.path}</code>
        {data && (
          <>
            {" "}
            <StatusBadge>{data.representation}</StatusBadge>{" "}
            <span className="muted">
              policy {data.policy_version} · sha{" "}
              {data.content_sha256.slice(0, 12)}
            </span>
          </>
        )}
      </p>
      {view.isLoading && <p role="status">Loading source…</p>}
      {view.isError && (
        <InlineNotice tone="danger">
          This file could not be read: {(view.error as Error).message}
        </InlineNotice>
      )}
      {data?.is_omitted && (
        <InlineNotice tone="info">
          This file is not shown ({data.omission_reason ?? "omitted by policy"}
          ).
        </InlineNotice>
      )}
      {data && !data.is_omitted && (
        <div
          className={styles.scroll}
          role="region"
          aria-label={`Source of ${active.path}`}
          tabIndex={0}
        >
          <table className={styles.code}>
            <tbody>
              {lines.map((line, index) => (
                <tr key={index}>
                  <th scope="row" className={styles.gutter}>
                    {index + 1}
                  </th>
                  <td>
                    <code>{line || " "}</code>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
