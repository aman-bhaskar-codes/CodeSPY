"""Synthetic admitted lock metadata and frozen advisory inputs only."""

import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime
from uuid import UUID

from app.admission.zip_inventory import inspect_zip_bytes
from app.contracts.dependencies import DependencyArchiveV1, DependencyRequestV1
from app.contracts.ingestion import AdmissionPolicyV2
from app.contracts.structure import SourceDescriptorV1, SourceFileV1, SourcePolicyV1
from app.scanners.advisories import AdvisoryBundle, ExactAnswer, PackageQuery

NOW = datetime(2026, 10, 21, tzinfo=UTC)


def fixture_dependency(root, *, bundle=None, policy=None, contents=None):
    if contents is None:
        contents = {
            "package-lock.json": json.dumps(
                {
                    "lockfileVersion": 3,
                    "packages": {
                        "": {"name": "synthetic-fixture"},
                        "node_modules/example": {
                            "version": "1.2.3",
                            "resolved": "https://registry.npmjs.org/example/-/example-1.2.3.tgz",
                        },
                    },
                }
            ).encode()
        }
    policy = policy or AdmissionPolicyV2()
    if bundle is None:
        bundle = AdvisoryBundle(
            answers=(
                ExactAnswer(
                    query=PackageQuery(ecosystem="npm", name="example", version="1.2.3"),
                    fetched_at=NOW,
                    complete=True,
                    advisory_ids=(),
                ),
            ),
            records=(),
        )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, data in contents.items():
            archive.writestr(path, data)
    raw = buffer.getvalue()
    manifest = inspect_zip_bytes(raw, policy)
    manifest_bytes = manifest.model_dump_json().encode()
    (root / "archive.zip").write_bytes(raw)
    (root / "manifest.json").write_bytes(manifest_bytes)
    files = tuple(
        SourceFileV1(
            file_id=UUID(int=i + 100),
            path=e.path,
            sha256=e.sha256,
            size_bytes=e.size_bytes,
            eligible=e.eligible,
            exclusion_reason=e.reason,
            language=e.language_hint,
        )
        for i, e in enumerate(manifest.entries)
    )
    request = DependencyRequestV1.model_validate(
        {
            "scope": {"org_id": UUID(int=1), "snapshot_id": UUID(int=2), "run_id": UUID(int=3)},
            "repository_id": UUID(int=4),
            "config_hash": "a" * 64,
            "runtime_digest": "a" * 64,
            "bundle_digest": bundle.digest(),
            "evaluated_at": NOW,
            "files": tuple(
                {
                    "file_id": f.file_id,
                    "sha256": f.sha256,
                    "size_bytes": f.size_bytes,
                    "format": "uv-v1-r3" if f.path.endswith("uv.lock") else "package-lock-v3",
                }
                for f in files
            ),
        }
    )
    source = DependencyArchiveV1(
        request=request,
        files=files,
        policy=SourcePolicyV1(policy_hash=policy.digest(), admission=policy),
        archive=SourceDescriptorV1(
            path="archive.zip", sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw)
        ),
        manifest=SourceDescriptorV1(
            path="manifest.json",
            sha256=hashlib.sha256(manifest_bytes).hexdigest(),
            size_bytes=len(manifest_bytes),
        ),
    )
    return source, bundle
