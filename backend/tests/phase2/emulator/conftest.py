"""Never connect to a local emulator unless its actual container ownership matches."""

import os

import pytest
from app.db.phase2_guard import validate_target
from scripts import phase2_db as harness


@pytest.fixture(autouse=True)
def owned_emulator():
    keys = ["HOST", "PORT", "DATABASE", "PROJECT", "MARKER", "ADMIN_PASSWORD"]
    values = {f"CODESPY_TEST_{key}": os.environ.get(f"CODESPY_TEST_{key}", "") for key in keys}
    validate_target(
        host=values["CODESPY_TEST_HOST"],
        port=int(values["CODESPY_TEST_PORT"] or "0"),
        database=values["CODESPY_TEST_DATABASE"],
        project=values["CODESPY_TEST_PROJECT"],
        marker=values["CODESPY_TEST_MARKER"],
    )
    harness.assert_container(values)
