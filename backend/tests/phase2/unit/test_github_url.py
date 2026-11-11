import pytest
from app.security.github_url import parse_github_url


@pytest.mark.parametrize(
    ("url", "name", "ref"),
    [
        ("https://github.com/expressjs/express", "expressjs/express", None),
        ("https://github.com/expressjs/express/", "expressjs/express", None),
        ("https://github.com/tiangolo/fastapi.git", "tiangolo/fastapi", None),
        ("https://github.com/owner/repo/tree/main", "owner/repo", "main"),
        ("https://github.com/owner/repo/tree/feature/x-1", "owner/repo", "feature/x-1"),
        ("https://GitHub.com/owner/re.po_1", "owner/re.po_1", None),
    ],
)
def test_accepts_public_repository_urls(url, name, ref):
    parsed = parse_github_url(url)
    assert (parsed.full_name, parsed.ref) == (name, ref)


@pytest.mark.parametrize(
    "url",
    [
        "",
        "http://github.com/owner/repo",
        "git://github.com/owner/repo",
        "ssh://git@github.com/owner/repo",
        "https://user@github.com/owner/repo",
        "https://user:pw@github.com/owner/repo",
        "https://github.com:8443/owner/repo",
        "https://github.com@evil.test/owner/repo",
        "https://evil.test/github.com/owner/repo",
        "https://github.com.evil.test/owner/repo",
        "https://www.github.com/owner/repo",
        "https://gist.github.com/owner/repo",
        "https://github.example.com/owner/repo",
        "https://github.com/owner",
        "https://github.com/owner/repo/issues",
        "https://github.com/owner/repo/blob/main/x",
        "https://github.com/owner/repo/tree",
        "https://github.com/owner/repo?tab=readme",
        "https://github.com/owner/repo#readme",
        "https://github.com/owner/repo/tree/../x",
        "https://github.com/owner/repo/tree/a//b",
        "https://github.com/owner/%2e%2e",
        "https://github.com/-owner/repo",
        "https://github.com/owner/..",
        "https://github.com/owner/re po",
        "https://github.com/owner\\repo",
        "https://github.com/ow\nner/repo",
        "https://github.com/" + "a" * 40 + "/repo",
        "https://github.com/owner/" + "r" * 101,
    ],
)
def test_rejects_everything_else(url):
    with pytest.raises(ValueError, match="invalid GitHub URL"):
        parse_github_url(url)
