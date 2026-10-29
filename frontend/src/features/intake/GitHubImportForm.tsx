import { useRef, useState } from "react";
import { Button } from "../../ui/Button";

export function GitHubImportForm({ pending, onImport }: { pending: boolean; onImport: (ref: string) => void }) {
  const [ref, setRef] = useState("");
  const [invalid, setInvalid] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  return <form noValidate onSubmit={event => { event.preventDefault(); if (!ref.trim()) { setInvalid(true); input.current?.focus(); return; } setInvalid(false); onImport(ref.trim()); }}>
    <p>This repository must already be bound to an authorized GitHub App installation by an administrator. Import pins the resolved commit before admission.</p>
    <label htmlFor="github-reference">Git reference</label>
    <input id="github-reference" ref={input} value={ref} maxLength={255} disabled={pending} placeholder="Branch, tag or full commit SHA" aria-invalid={invalid} aria-describedby={invalid ? "github-ref-error" : undefined} onChange={event => setRef(event.target.value)} />
    {invalid && <p id="github-ref-error" role="alert">Enter a branch, tag or commit reference.</p>}
    <Button type="submit" intent="primary" pending={pending}>Import from GitHub</Button>
  </form>;
}
