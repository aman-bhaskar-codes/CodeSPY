"""Strict parsing of public GitHub repository URLs; no network access."""

import re
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

_OWNER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?")
_REPO = re.compile(r"[A-Za-z0-9_.-]{1,100}")
_REF = re.compile(r"[A-Za-z0-9_./-]{1,255}")


@dataclass(frozen=True)
class GitHubRepositoryUrl:
    owner: str
    repo: str
    ref: str | None

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"


def parse_github_url(value: str) -> GitHubRepositoryUrl:
    """Accept only https://github.com/<owner>/<repo>[.git][/tree/<ref>]."""
    if not value or len(value) > 400 or any(ord(c) < 33 or ord(c) > 126 for c in value):
        raise ValueError("invalid GitHub URL")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.query
        or parsed.fragment
        or "@" in parsed.netloc
        or "%" in parsed.path
        or "\\" in parsed.path
    ):
        raise ValueError("invalid GitHub URL")
    parts = [unquote(p) for p in parsed.path.split("/")]
    if parts and parts[0] == "":
        parts = parts[1:]
    if parts and parts[-1] == "":
        parts = parts[:-1]  # one trailing slash
    if len(parts) < 2 or len(parts) == 3 or (len(parts) > 3 and parts[2] != "tree"):
        raise ValueError("invalid GitHub URL")
    owner, repo = parts[0], parts[1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    ref = "/".join(parts[3:]) if len(parts) > 3 else None
    if (
        not _OWNER.fullmatch(owner)
        or not _REPO.fullmatch(repo)
        or repo in {".", ".."}
        or (
            ref is not None
            and (not _REF.fullmatch(ref) or ".." in ref.split("/") or "" in ref.split("/"))
        )
    ):
        raise ValueError("invalid GitHub URL")
    return GitHubRepositoryUrl(owner, repo, ref)
