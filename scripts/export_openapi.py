"""Export the application schema without credentials, services or network access."""

import json
import sys
from pathlib import Path
from typing import cast

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.api.application import ApiSettings, ApplicationPorts, create_app
from app.ports.object_store import ObjectStore
from sqlalchemy.ext.asyncio import async_sessionmaker


def main() -> None:
    app = create_app(
        ApiSettings(),
        ApplicationPorts(factory=async_sessionmaker(), store=cast(ObjectStore, None)),
    )
    (ROOT / "backend/openapi.json").write_text(
        json.dumps(app.openapi(), sort_keys=True, indent=2) + "\n"
    )


if __name__ == "__main__":
    main()
