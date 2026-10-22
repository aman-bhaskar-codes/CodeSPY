"""Reject actual container configuration drift before running a scanner."""

import json

import pytest
from app.scanners.docker import DockerController
from app.scanners.process import NativeProcessError, NativeResult


def configuration():
    return {
        "HostConfig": {
            "NetworkMode": "none",
            "ReadonlyRootfs": True,
            "Privileged": False,
            "Memory": 1073741824,
            "MemorySwap": 1073741824,
            "NanoCpus": 2000000000,
            "PidsLimit": 64,
            "CapDrop": ["ALL"],
            "LogConfig": {"Type": "none"},
            "SecurityOpt": ["no-new-privileges"],
            "PortBindings": {},
            "PidMode": "",
            "Tmpfs": {"/tmp": "rw,noexec,nosuid,size=67108864,mode=700,uid=65534,gid=65534"},  # noqa: S108 - verifies private container tmpfs
            "Ulimits": [
                {"Name": name, "Soft": limit, "Hard": limit}
                for name, limit in (("nofile", 128), ("cpu", 180), ("fsize", 2097152))
            ],
        },
        "Config": {"User": "65534:65534"},
        "Mounts": [
            {"Type": "bind", "Destination": "/input/" + name, "RW": False}
            for name in ("archive.zip", "manifest.json", "request.json")
        ],
    }


@pytest.mark.parametrize(
    "drift", ["user", "writable", "socket", "missing", "tmpfs", "exec", "size", "ulimit"]
)
async def test_actual_user_and_mount_drift_is_rejected(tmp_path, monkeypatch, drift):
    controller = DockerController(tmp_path / "docker", tmp_path / "socket", tmp_path / "config")
    value = configuration()
    if drift == "user":
        value["Config"]["User"] = "0"
    elif drift == "writable":
        value["Mounts"][0]["RW"] = True
    elif drift == "socket":
        value["Mounts"].append({"Type": "bind", "Destination": "/var/run/docker.sock", "RW": False})
    elif drift == "missing":
        value["Mounts"].pop()
    elif drift == "tmpfs":
        value["HostConfig"]["Tmpfs"] = {}
    elif drift == "exec":
        value["HostConfig"]["Tmpfs"]["/tmp"] = "rw,size=67108864"  # noqa: S108 - verifies private container tmpfs
    elif drift == "size":
        value["HostConfig"]["Tmpfs"]["/tmp"] = (  # noqa: S108 - verifies private container tmpfs
            "rw,noexec,nosuid,size=134217728,mode=700,uid=65534,gid=65534"
        )
    else:
        value["HostConfig"]["Ulimits"][0]["Hard"] = 4096

    async def inspect(args):
        # Support the old HostConfig-only query to expose its missing checks.
        payload = value["HostConfig"] if args[-1] == "{{json .HostConfig}}" else value
        return NativeResult(exit_code=0, output=json.dumps(payload).encode())

    monkeypatch.setattr(controller, "_run", inspect)
    with pytest.raises(NativeProcessError, match="scanner_controls_unavailable"):
        await controller.verify_controls("codespy-scan-" + "a" * 32)


async def test_native_timeout_exit_remains_timeout_at_controller_boundary(tmp_path, monkeypatch):
    controller = DockerController(tmp_path / "docker", tmp_path / "socket", tmp_path / "config")

    async def native(args, **kwargs):
        assert kwargs["accepted_exits"] == (0, 75)
        return NativeResult(exit_code=75, output=b"")

    monkeypatch.setattr(controller, "_run", native)
    with pytest.raises(NativeProcessError, match="scanner_timeout"):
        await controller.start("codespy-scan-" + "a" * 32, 65536)
