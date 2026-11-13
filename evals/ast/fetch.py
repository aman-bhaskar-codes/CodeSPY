"""Pinned public-repository corpus: resolve commits and download archives.

Live network access. Uses the same fixed-origin, credential-less transport as the
public GitHub importer (api.github.com -> codeload.github.com only).
"""

import hashlib
import json
from pathlib import Path
from typing import Any

import httpx
from app.adapters.github.public import GitHubPublicProvider
from app.adapters.github.transport import FixedGitHubTransport

MANIFEST = Path(__file__).with_name("corpus.json")
CACHE = Path(__file__).resolve().parents[2] / ".local" / "ast-corpus"


def load() -> dict[str, Any]:
    value: dict[str, Any] = json.loads(MANIFEST.read_text())
    return value


def provider() -> tuple[GitHubPublicProvider, httpx.AsyncClient]:
    client = httpx.AsyncClient(
        transport=FixedGitHubTransport({"api.github.com", "codeload.github.com"}),
        trust_env=False,
        follow_redirects=False,
    )
    return GitHubPublicProvider(client), client


def archive_path(name: str, commit: str) -> Path:
    return CACHE / (name.replace("/", "__") + "-" + commit + ".zip")


async def resolve() -> dict[str, Any]:
    """Pin every repository without a commit to its current default-branch head."""
    manifest = load()
    github, client = provider()
    async with client:
        for repo in manifest["repos"]:
            if not repo.get("commit"):
                repo["commit"] = await github.resolve(repo["name"], None)
                print("pinned", repo["name"], repo["commit"])
    MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


async def fetch() -> list[tuple[str, str, Path, str]]:
    """Download missing archives; return (name, commit, path, sha256) per repository."""
    manifest = load()
    github, client = provider()
    CACHE.mkdir(parents=True, exist_ok=True)
    out = []
    async with client:
        for repo in manifest["repos"]:
            name, commit = repo["name"], repo.get("commit")
            if not commit:
                raise SystemExit(f"{name} is not pinned; run resolve first")
            path = archive_path(name, commit)
            if not path.exists():
                tmp = path.with_suffix(".part")
                with tmp.open("wb") as handle:
                    async for block in github.archive(name, commit):
                        handle.write(block)
                tmp.rename(path)
                print("downloaded", name, path.stat().st_size)
            out.append((name, commit, path, hashlib.sha256(path.read_bytes()).hexdigest()))
    return out
