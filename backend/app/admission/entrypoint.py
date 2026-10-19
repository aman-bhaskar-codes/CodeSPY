"""Only this pinned program runs inside the networkless admission container."""

import hashlib
import json
import os
import resource
import signal
import socket
import stat
import sys

MAX_OUTPUT = 67108864


def emit_file(name: str, value: bytes) -> None:
    fd = os.open("/output/" + name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o644)  # Host broker can read; enclosing host directory remains0700.
        view = memoryview(value)
        while view:
            view = view[os.write(fd, view) :]
        os.fsync(fd)
    finally:
        os.close(fd)


def probe() -> dict[str, bool]:
    result = {
        "nonroot": os.getuid() != 0,
        "no_inherited_secrets": not any(
            k.startswith(("AWS_", "DATABASE_", "OPENAI_", "ANTHROPIC_", "GEMINI_"))
            for k in os.environ
        ),
    }
    for name, path in [
        ("readonly_root", "/app/write-test"),
        ("readonly_input", "/input/source.zip"),
    ]:
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
            os.close(fd)
            result[name] = False
        except OSError:
            result[name] = True
    with socket.socket() as connection:
        connection.settimeout(0.2)
        try:
            connection.connect(("198.51.100.1", 80))
            result["network_denied"] = False
        except OSError:
            result["network_denied"] = True
    with open("/proc/self/status") as status:
        result["no_capabilities"] = any(
            line.startswith("CapEff:") and int(line.split()[1], 16) == 0 for line in status
        )
    return result


def main() -> int:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_AS, (536870912, 536870912))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_OUTPUT, MAX_OUTPUT))
    resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
    mode = sys.argv[1] if len(sys.argv) == 2 else ""
    try:
        if mode in {"export_manifest", "export_probe"}:
            name = "manifest.json" if mode == "export_manifest" else "probe.json"
            fd = os.open("/output/" + name, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_OUTPUT:
                    return 70
                remaining = info.st_size
                while remaining:
                    block = stream.read(min(65536, remaining))
                    if not block:
                        return 70
                    sys.stdout.buffer.write(block)
                    remaining -= len(block)
                sys.stdout.buffer.flush()
            return 0
        if mode == "qualify":
            encoded = json.dumps(probe(), sort_keys=True).encode()
            name = "probe.json"
        elif mode == "inspect":
            from app.admission.zip_inventory import inspect_zip_bytes
            from app.contracts.ingestion import AdmissionPolicyV1
            from app.domain.canonical_json import canonical_bytes

            fd = os.open("/input/request.json", os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as request:
                raw = request.read(4097)
                if len(raw) > 4096:
                    raise ValueError("request bound")
                metadata = json.loads(raw)
            policy = AdmissionPolicyV1.model_validate(metadata["policy"])
            fd = os.open("/input/source.zip", os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as source:
                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    raise ValueError("input descriptor")
                data = source.read(policy.max_archive_bytes + 1)
            if (
                len(data) != metadata["size_bytes"]
                or hashlib.sha256(data).hexdigest() != metadata["sha256"]
            ):
                raise ValueError("input digest")
            manifest = inspect_zip_bytes(
                data,
                policy,
                source_revision=metadata.get("source_revision"),
                github_repository_id=metadata.get("github_repository_id"),
            )
            encoded = canonical_bytes(manifest.model_dump())
            name = "manifest.json"
        else:
            return 64
        if len(encoded) > MAX_OUTPUT:
            raise ValueError("output bound")
        emit_file(name, encoded)
        print(
            json.dumps(
                {
                    "status": "ok",
                    "sha256": hashlib.sha256(encoded).hexdigest(),
                    "rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                }
            ),
            flush=True,
        )
        # Keep bounded tmpfs alive until the broker copies the fixed output.
        signal.pause()
        return 0
    except Exception as error:
        from app.admission.zip_inventory import AdmissionRejected

        code = str(error) if isinstance(error, AdmissionRejected) else "admission_process_failed"
        print(
            json.dumps(
                {
                    "status": "rejected" if isinstance(error, AdmissionRejected) else "failed",
                    "code": code,
                }
            )
        )
        return 65 if isinstance(error, AdmissionRejected) else 70


if __name__ == "__main__":
    raise SystemExit(main())
