"""Bounded Docker control for owned offline scanner containers."""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.scanners.process import NativeProcessError, NativeResult, run_private


def _name(value: str) -> None:
    if not re.fullmatch(r"codespy-scan-[0-9a-f]{32}", value):
        raise ValueError("invalid scanner container name")


@dataclass(frozen=True)
class SandboxLaunch:
    name: str
    marker: str
    image: str
    input_root: Path
    mode: Literal["scan", "identities", "attest", "dependencies"] = "scan"


class DockerController:
    def __init__(self, executable: Path, socket: Path, config_root: Path):
        if (
            not executable.is_absolute()
            or not socket.is_absolute()
            or not config_root.is_absolute()
        ):
            raise ValueError("absolute Docker runtime paths required")
        config_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if config_root.is_symlink() or any(config_root.iterdir()):
            raise ValueError("empty owned Docker CLI configuration required")
        self.executable, self.socket, self.config_root = executable, socket, config_root

    async def _run(
        self,
        args: tuple[str, ...],
        *,
        seconds: float = 15,
        maximum: int = 65536,
        accepted_exits: tuple[int, ...] = (0,),
    ) -> NativeResult:
        try:
            result = await run_private(
                (
                    str(self.executable),
                    "--host",
                    "unix://" + str(self.socket),
                    "--config",
                    str(self.config_root),
                    *args,
                ),
                deadline=seconds,
                stdout_limit=maximum,
                stderr_limit=65536,
            )
        except OSError:
            raise NativeProcessError("scanner_runtime_unavailable") from None
        if result.exit_code not in accepted_exits:
            raise NativeProcessError("scanner_runtime_unavailable")
        return result

    async def inspect_owned(self, name: str) -> dict[str, str] | None:
        _name(name)
        listed = await self._run(
            (
                "container",
                "ls",
                "--all",
                "--no-trunc",
                "--filter",
                "name=^/" + name + "$",
                "--format",
                "{{.ID}}",
            )
        )
        ids = listed.output.decode("ascii").split()
        if not ids:
            return None
        if len(ids) != 1 or not re.fullmatch(r"[0-9a-f]{64}", ids[0]):
            raise ValueError("ambiguous scanner container identity")
        actual = await self._run(
            (
                "container",
                "inspect",
                ids[0],
                "--format",
                '{{.Id}} {{.Image}} {{index .Config.Labels "codespy.scanner.owner"}}',
            )
        )
        values = actual.output.decode("ascii").split()
        if len(values) != 3:
            raise ValueError("invalid scanner ownership metadata")
        return dict(zip(("id", "image", "marker"), values, strict=True))

    async def remove(self, identity: str) -> None:
        if not re.fullmatch(r"[0-9a-f]{64}", identity):
            raise ValueError("exact scanner container ID required")
        await self._run(("container", "rm", "--force", identity))

    async def image_entrypoint(self, image: str) -> tuple[str, ...]:
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
            raise ValueError("pinned local image ID required")
        result = await self._run(
            ("image", "inspect", image, "--format", "{{json .Config.Entrypoint}}")
        )
        value = json.loads(result.output)
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ValueError("invalid scanner image entrypoint")
        return tuple(value)

    async def create(self, launch: SandboxLaunch) -> None:
        _name(launch.name)
        if not re.fullmatch(r"[0-9a-f]{32}", launch.marker) or not re.fullmatch(
            r"sha256:[0-9a-f]{64}", launch.image
        ):
            raise ValueError("invalid scanner launch identity")
        args = [
            "container",
            "create",
            "--name",
            launch.name,
            "--label",
            "codespy.scanner.owner=" + launch.marker,
            "--network",
            "none",
            "--read-only",
            "--user",
            "65534:65534",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--memory",
            "1073741824",
            "--memory-swap",
            "1073741824",
            "--cpus",
            "2",
            "--pids-limit",
            "64",
            "--ulimit",
            "nofile=128:128",
            "--ulimit",
            "cpu=180:180",
            "--ulimit",
            "fsize=2097152:2097152",
            "--log-driver",
            "none",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=67108864,mode=700,uid=65534,gid=65534",  # noqa: S108 - private container tmpfs
        ]
        for filename in ("archive.zip", "manifest.json", "request.json"):
            path = launch.input_root / filename
            if (
                not path.is_absolute()
                or path.is_symlink()
                or not path.is_file()
                or any(c in str(path) for c in (",", "\n", "\r", "\0"))
            ):
                raise ValueError("owned regular scanner input required")
            path.chmod(0o644)
            args.extend(("--mount", f"type=bind,src={path},dst=/input/{filename},readonly"))
        if launch.mode not in {"scan", "identities", "attest", "dependencies"}:
            raise ValueError("invalid scanner launch mode")
        suffix = (launch.image,) + ((launch.mode,) if launch.mode != "scan" else ())
        await self._run(tuple(args) + suffix)

    async def verify_controls(self, name: str) -> None:
        _name(name)
        projection = (
            '{"HostConfig":{{json .HostConfig}},'
            '"Config":{"User":{{json .Config.User}}},"Mounts":{{json .Mounts}}}'
        )
        result = await self._run(("container", "inspect", name, "--format", projection))
        actual = json.loads(result.output)
        host = actual["HostConfig"]
        mounts = actual["Mounts"]
        expected = {"/input/" + name for name in ("archive.zip", "manifest.json", "request.json")}
        tmpfs = host.get("Tmpfs") or {}
        options = set(tmpfs.get("/tmp", "").split(","))  # noqa: S108 - verifies private container tmpfs
        limits = {
            (limit["Name"], limit["Soft"], limit["Hard"]) for limit in host.get("Ulimits") or ()
        }
        if (
            actual["Config"]["User"] != "65534:65534"
            or len(mounts) != 3
            or {m["Destination"] for m in mounts} != expected
            or any(m["Type"] != "bind" or m["RW"] for m in mounts)
            or set(tmpfs) != {"/tmp"}  # noqa: S108 - verifies private container tmpfs
            or options
            != {"rw", "noexec", "nosuid", "size=67108864", "mode=700", "uid=65534", "gid=65534"}
            or limits != {("nofile", 128, 128), ("cpu", 180, 180), ("fsize", 2097152, 2097152)}
            or host["NetworkMode"] != "none"
            or not host["ReadonlyRootfs"]
            or host["Privileged"]
            or host["Memory"] != 1073741824
            or host["MemorySwap"] != 1073741824
            or host["NanoCpus"] != 2000000000
            or host["PidsLimit"] != 64
            or "ALL" not in host["CapDrop"]
            or host["LogConfig"]["Type"] != "none"
            or not any(x.split(":", 1)[0] == "no-new-privileges" for x in host["SecurityOpt"])
            or host.get("PortBindings")
            or host.get("PidMode") == "host"
        ):
            raise NativeProcessError("scanner_controls_unavailable")

    async def start(self, name: str, maximum: int) -> NativeResult:
        _name(name)
        result = await self._run(
            ("container", "start", "--attach", name),
            seconds=180,
            maximum=maximum,
            accepted_exits=(0, 75),
        )
        if result.exit_code == 75:
            raise NativeProcessError("scanner_timeout")
        return result
