"""Versioned prompt assets.

A prompt is a directory ``<name>/<version>/`` of files. Its identity is a digest over every file,
recorded with each run as the prompt revision. Changing any file without adding a new version
directory fails the pinned-digest test, so a prompt never changes under an existing version.
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from string import Template

ROOT = Path(__file__).parent


class PromptError(ValueError):
    """A prompt asset is missing or a rendering was refused."""


@dataclass(frozen=True)
class PromptAsset:
    name: str
    version: str
    files: dict[str, str]
    digest: str

    @property
    def revision(self) -> str:
        """The string recorded on a spec: name, version and the first 16 digest characters."""
        return f"{self.name}.{self.version}.{self.digest[:16]}"

    def text(self, filename: str) -> str:
        try:
            return self.files[filename]
        except KeyError:
            raise PromptError(f"{self.name}/{self.version} has no {filename}") from None


def load_prompt(name: str, version: str) -> PromptAsset:
    directory = (ROOT / name / version).resolve()
    if ROOT.resolve() not in directory.parents or not directory.is_dir():
        raise PromptError(f"unknown prompt {name}/{version}")
    files = {
        p.name: p.read_text(encoding="utf-8") for p in sorted(directory.iterdir()) if p.is_file()
    }
    if not files:
        raise PromptError(f"prompt {name}/{version} is empty")
    material = b"".join(
        name_.encode() + b"\0" + body.encode("utf-8") + b"\0"
        for name_, body in sorted(files.items())
    )
    return PromptAsset(name, version, files, hashlib.sha256(material).hexdigest())


def boundary_marker(*parts: str) -> str:
    """A marker the repository cannot predict: built from ids the run created, not from content."""
    return "=====" + hashlib.sha256(":".join(parts).encode()).hexdigest()[:20].upper() + "====="


def render(asset: PromptAsset, filename: str, *, marker: str, data: str, **values: str) -> str:
    """Fill a template; refuse if the data could close its own boundary."""
    if marker in data:
        raise PromptError("the data contains the boundary marker")
    return Template(asset.text(filename)).substitute(marker=marker, page=data, **values)


def compact_schema(asset: PromptAsset) -> str:
    return json.dumps(
        json.loads(asset.text("output.schema.json")), separators=(",", ":"), sort_keys=True
    )
