"""Owned synthetic Linux toolchain probes; not the structural job runner."""

import argparse
import asyncio
import json
import re
import time
from pathlib import Path
from uuid import uuid4


class ProbeOutputLimit(ValueError):
    pass


def read_image(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError("regular owned image file required")
    image = path.read_text().strip()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        raise ValueError("immutable image ID required")
    return image


def validate_ownership(labels: dict[str, str], marker: str) -> None:
    if labels.get("codespy.phase3.probe") != marker:
        raise ValueError("probe ownership mismatch")


async def bounded_command(
    args: list[str],
    *,
    timeout: float,
    limit: int = 65536,
    started: asyncio.Event | None = None,
) -> bytes:
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )

    overflow = asyncio.Event()

    async def read(stream, notify=False):
        data = bytearray()
        discarded = False
        while chunk := await stream.read(8192):
            if notify and started is not None:
                started.set()
            if discarded or len(data) + len(chunk) > limit:
                discarded = True
                overflow.set()
                # Continue draining after the limit so termination cannot wait
                # forever on an asyncio pipe transport paused at high water.
                continue
            data.extend(chunk)
        return bytes(data)

    tasks = [
        asyncio.create_task(read(process.stdout, True)),
        asyncio.create_task(read(process.stderr)),
        asyncio.create_task(process.wait()),
    ]
    work = asyncio.gather(*tasks)
    exceeded = asyncio.create_task(overflow.wait())
    try:
        done, _ = await asyncio.wait(
            {work, exceeded}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
        if not done:
            raise TimeoutError("probe command deadline")
        if overflow.is_set():
            raise ProbeOutputLimit("probe output limit")
        stdout, _stderr, code = await work
        if code:
            raise ValueError(f"probe command failed with exit {code}")
        return stdout
    except BaseException:
        if process.returncode is None:
            process.kill()
        try:
            # Readers remain active and discard excess bytes while the killed
            # process closes its pipes. Cleanup has its own finite budget.
            await asyncio.wait_for(asyncio.shield(work), timeout=5)
        except (TimeoutError, OSError, asyncio.CancelledError):
            # Pinned CPython asyncio exposes no public Process.close(). Close
            # its transport only as a last resort, then retire owned readers.
            transport = getattr(process, "_transport", None)
            if transport is not None:
                transport.close()
            work.cancel()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        raise
    finally:
        exceeded.cancel()
        await asyncio.gather(exceeded, return_exceptions=True)


async def docker(*args: str) -> bytes:
    return await bounded_command(["docker", *args], timeout=15)


class OwnedProbe:
    def __init__(self, image: str, directory: Path):
        self.image = image
        self.directory = directory
        self.marker = uuid4().hex
        self.names: set[str] = set()

    def record(self):
        path = self.directory / "ownership.json"
        path.write_text(
            json.dumps(
                {
                    "marker": self.marker,
                    "image": self.image,
                    "containers": sorted(self.names),
                }
            )
        )
        path.chmod(0o600)

    async def remove(self, name: str):
        # Never infer ownership from a prefix or delete another container.
        labels = json.loads(
            await docker("inspect", "--format", "{{json .Config.Labels}}", name)
        )
        validate_ownership(labels, self.marker)
        await docker("rm", "--force", name)
        self.names.remove(name)
        self.record()

    async def run(self, mode: str, *, started: asyncio.Event | None = None) -> bytes:
        if mode not in {"roundtrip", "hold", "flood"}:
            raise ValueError("fixed probe required")
        name = "codespy-p3-probe-" + self.marker + "-" + mode
        self.names.add(name)
        self.record()  # Intent before create; retain it if cleanup is ambiguous.
        created = False
        try:
            await docker(
                "create",
                "--pull",
                "never",
                "--name",
                name,
                "--label",
                "codespy.phase3.probe=" + self.marker,
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                "--pids-limit",
                "64",
                "--memory",
                "1g",
                "--memory-swap",
                "1g",
                "--cpus",
                "2",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,size=67108864",
                self.image,
                mode,
            )
            created = True
            info = json.loads(
                await docker("inspect", "--format", "{{json .HostConfig}}", name)
            )
            assert info["NetworkMode"] == "none" and info["ReadonlyRootfs"]
            assert info["Memory"] == info["MemorySwap"] == 1073741824
            assert info["PidsLimit"] == 64 and "ALL" in info["CapDrop"]
            return await bounded_command(
                ["docker", "start", "--attach", name], timeout=30, started=started
            )
        finally:
            if created:
                await self.remove(name)
            # An interrupted create remains in ownership.json for inspection;
            # do not assume absence and erase its cleanup record.


async def qualify(image: str, output: Path):
    if output.exists() or output.is_symlink():
        raise ValueError("choose a new qualification output")
    image_labels = json.loads(
        await docker("image", "inspect", "--format", "{{json .Config.Labels}}", image)
    )
    if (
        image_labels.get("codespy.phase3.qualification")
        != "structural-toolchain-probe-v1"
    ):
        raise ValueError("trusted qualification image required")
    directory = output.parent / ("probe-" + uuid4().hex)
    directory.mkdir(mode=0o700, parents=True)
    probe = OwnedProbe(image, directory)
    before = time.monotonic()
    roundtrip = json.loads(await probe.run("roundtrip"))
    elapsed = time.monotonic() - before
    assert roundtrip["memory_max"] == "1073741824"
    assert roundtrip["pids_max"] == "64"
    assert roundtrip["native_peak_rss_kib"] > 0
    started = asyncio.Event()
    task = asyncio.create_task(probe.run("hold", started=started))
    try:
        await asyncio.wait_for(started.wait(), 10)
        before = time.monotonic()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        cancelled = time.monotonic() - before
        assert cancelled < 10
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    try:
        await probe.run("flood")
    except ProbeOutputLimit:
        output_capped = True
    else:
        raise AssertionError("synthetic output flood was not bounded")
    assert not probe.names
    result = {
        "image": image,
        "roundtrip": roundtrip,
        "roundtrip_seconds": elapsed,
        "cancellation_cleanup_seconds": cancelled,
        "stdout_limit_bytes": 65536,
        "output_cap_verified": output_capped,
        "owned_containers_remaining": 0,
        "scope": "trusted synthetic toolchain probes; not hostile analyzer qualification",
    }
    output.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")
    output.chmod(0o600)
    print(json.dumps(result, sort_keys=True))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(qualify(read_image(args.image_file), args.output))


if __name__ == "__main__":
    main()
