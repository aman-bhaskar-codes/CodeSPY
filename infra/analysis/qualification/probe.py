"""Fixed synthetic probes; never loads target repositories or configuration."""

import importlib
import importlib.metadata
import json
import os
import resource
import subprocess
import sys
import time
from pathlib import Path

from tree_sitter import Language, Parser


def main():
    mode = sys.argv[1]
    if mode == "hold":
        print('{"started":true}', flush=True)
        time.sleep(60)
    elif mode == "flood":
        while True:
            os.write(1, b"x" * 65536)
    elif mode == "roundtrip":
        pins = {
            "tree-sitter": "0.26.0",
            "tree-sitter-python": "0.25.0",
            "tree-sitter-javascript": "0.25.0",
            "tree-sitter-typescript": "0.23.2",
        }
        assert {name: importlib.metadata.version(name) for name in pins} == pins
        cases = [
            ("tree_sitter_python", "language", b"def example():\n    return 1\n"),
            ("tree_sitter_javascript", "language", b"function example() {}"),
            (
                "tree_sitter_typescript",
                "language_typescript",
                b"const value: number = 1;",
            ),
            ("tree_sitter_typescript", "language_tsx", b"const element = <div/>;"),
        ]
        abis = {}
        for module, factory, source in cases:
            language = Language(getattr(importlib.import_module(module), factory)())
            tree = Parser(language).parse(source)
            assert not tree.root_node.has_error
            abis[f"{module}.{factory}"] = language.abi_version
        node = subprocess.run(
            [
                "node",
                "--max-old-space-size=256",
                "--test",
                "/typescript/tests/capabilities.test.cjs",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert len(node.stdout) < 65536 and len(node.stderr) < 65536
        callback = subprocess.run(
            [sys.executable, "/probe/progress_probe.py"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        sensitive = [
            key
            for key in os.environ
            if key.startswith(("AWS_", "ANTHROPIC_", "OPENAI_", "GOOGLE_"))
        ]
        assert not sensitive
        assert os.getuid() == 65534
        result = {
            "pins": pins,
            "abis": abis,
            "uid": os.getuid(),
            "native_peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "memory_max": Path("/sys/fs/cgroup/memory.max").read_text().strip(),
            "pids_max": Path("/sys/fs/cgroup/pids.max").read_text().strip(),
            "node_tests_passed": 3,
            "progress_callback_returncode": callback.returncode,
            "progress_callback_enabled": False,
            "cancellation_strategy": "external-container-process-lifecycle",
        }
        print(json.dumps(result, sort_keys=True))
    else:
        raise ValueError("unknown fixed probe")


if __name__ == "__main__":
    main()
