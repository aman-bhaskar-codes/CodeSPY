"""Build-only pinned Linux artifact acquisition; never invoked on target content."""

import hashlib
import io
import platform
import tarfile
import urllib.request
from pathlib import Path

VERSION = "8.30.1"
SHA256 = "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb"
URL = f"https://github.com/gitleaks/gitleaks/releases/download/v{VERSION}/gitleaks_{VERSION}_linux_x64.tar.gz"


def main() -> None:
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise RuntimeError("qualified scanner platform requires Linux x86_64")
    with urllib.request.urlopen(URL, timeout=60) as response:
        raw = response.read(33554433)
    if len(raw) > 33554432 or hashlib.sha256(raw).hexdigest() != SHA256:
        raise RuntimeError("scanner artifact identity differs")
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        member = archive.getmember("gitleaks")
        if not member.isfile() or not 0 < member.size <= 33554432:
            raise RuntimeError("scanner artifact member invalid")
        stream = archive.extractfile(member)
        if stream is None:
            raise RuntimeError("scanner artifact member missing")
        binary = stream.read(33554433)
        if len(binary) != member.size:
            raise RuntimeError("scanner artifact member invalid")
        notice = archive.getmember("LICENSE")
        if not notice.isfile() or notice.size > 65536:
            raise RuntimeError("scanner license member invalid")
        license_stream = archive.extractfile(notice)
        if license_stream is None:
            raise RuntimeError("scanner license member missing")
        notices = Path("/usr/local/share/licenses/gitleaks")
        notices.mkdir(parents=True, exist_ok=True)
        (notices / "LICENSE").write_bytes(license_stream.read(65537))
        target = Path("/usr/local/bin/gitleaks")
        with target.open("xb") as output:
            output.write(binary)
        target.chmod(0o755)


if __name__ == "__main__":
    main()
