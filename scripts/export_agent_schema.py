"""Export the agent event protocol JSON Schema without credentials, services or network access."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.contracts.agent_events import export_schema

TARGET = ROOT / "docs/contracts/AgentEventV1.schema.json"


def render() -> str:
    return json.dumps(export_schema(), sort_keys=True, indent=2) + "\n"


def main() -> None:
    TARGET.write_text(render())


if __name__ == "__main__":
    main()
