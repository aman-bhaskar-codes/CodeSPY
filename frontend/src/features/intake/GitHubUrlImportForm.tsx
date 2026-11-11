import { useRef, useState } from "react";
import { Button } from "../../ui/Button";

const URL_PATTERN = /^https:\/\/github\.com\/[A-Za-z0-9-]+\/[A-Za-z0-9_.-]+(\.git)?(\/tree\/[A-Za-z0-9_./-]+)?\/?$/;

export function GitHubUrlImportForm({ pending, onImport }: { pending: boolean; onImport: (url: string) => void }) {
  const [url, setUrl] = useState("");
  const [invalid, setInvalid] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  return <form noValidate onSubmit={event => {
    event.preventDefault();
    const value = url.trim();
    if (!URL_PATTERN.test(value)) { setInvalid(true); input.current?.focus(); return; }
    setInvalid(false);
    onImport(value);
  }}>
    <p>Import a public GitHub repository. The default branch is resolved to one commit before admission; private repositories need an administrator-bound GitHub App installation.</p>
    <label htmlFor="github-url">Public repository URL</label>
    <input id="github-url" ref={input} value={url} maxLength={400} disabled={pending} placeholder="https://github.com/owner/repository" aria-invalid={invalid} aria-describedby={invalid ? "github-url-error" : undefined} onChange={event => setUrl(event.target.value)} />
    {invalid && <p id="github-url-error" role="alert">Enter a https://github.com/owner/repository URL.</p>}
    <Button type="submit" intent="primary" pending={pending}>Import public repository</Button>
  </form>;
}
