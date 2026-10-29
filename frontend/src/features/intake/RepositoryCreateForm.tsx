import { useEffect, useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { api, ApiError } from "../../api/client";
import type { RepositoryCreate, RepositoryRecord } from "../../api/generated/types.gen";
import { Button } from "../../ui/Button";
import { InlineNotice } from "../../ui/InlineNotice";

export function RepositoryCreateForm({ csrfToken, onCreated, disabled = false, onPendingChange }: { csrfToken: string; onCreated: (record: RepositoryRecord) => void; disabled?: boolean; onPendingChange?: (pending: boolean) => void }) {
  const [name, setName] = useState("");
  const [invalid, setInvalid] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const intent = useRef<{ name: string; request: RepositoryCreate; key: string } | null>(null);
  const create = useMutation({ mutationFn: () => {
    const displayName = name.trim();
    if (intent.current?.name !== displayName) intent.current = { name: displayName, key: crypto.randomUUID(), request: { display_name: displayName, source_kind: "archive", repository_key: `archive-${crypto.randomUUID()}` } };
    return api.createRepository(intent.current.request, csrfToken, intent.current.key);
  }, onSuccess: (record) => { intent.current = null; setName(""); onCreated(record); } });
  useEffect(() => { onPendingChange?.(create.isPending); }, [create.isPending, onPendingChange]);
  return <details className="repository-create"><summary>Create a repository for an archive</summary>
    <p className="muted">Organization owners and administrators can create a workspace repository. This does not create a remote GitHub repository.</p>
    <form noValidate onSubmit={event => { event.preventDefault(); if (disabled || create.isPending) return; if (!name.trim() || name.trim().length > 200) { setInvalid(true); input.current?.focus(); return; } setInvalid(false); create.mutate(); }}>
      <label htmlFor="repository-name">Repository name</label>
      <input id="repository-name" ref={input} value={name} maxLength={200} disabled={disabled || create.isPending} aria-invalid={invalid} aria-describedby={invalid ? "repository-name-error" : undefined} onChange={event => { setName(event.target.value); setInvalid(false); }} />
      {invalid && <p id="repository-name-error" role="alert">Enter a repository name, up to 200 characters.</p>}
      {create.isError && <InlineNotice tone="danger">{create.error instanceof ApiError && create.error.status === 403 ? "An organization owner or administrator must create the repository." : "Creation could not be confirmed. Retry with the same name to recover the original result."}</InlineNotice>}
      <Button type="submit" intent="primary" disabled={disabled} pending={create.isPending}>Create repository</Button>
    </form>
  </details>;
}
