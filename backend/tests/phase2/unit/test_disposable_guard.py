import pytest
from app.db.phase2_guard import validate_target


@pytest.mark.parametrize(
    "field,value",
    [
        ("host", "database.example.com"),
        ("port", 5432),
        ("port", 55432),
        ("project", "codespy"),
        ("database", "codespy"),
        ("marker", ""),
    ],
)
def test_phase2_rejects_nonowned_destination(field, value):
    request = {
        "host": "127.0.0.1",
        "port": 55433,
        "database": "codespy_p2_test_fixture",
        "marker": "a" * 32,
        "project": "codespy-phase2-tests",
    }
    request[field] = value
    with pytest.raises(ValueError):
        validate_target(**request)
