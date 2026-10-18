import pytest
from app.db.guard import validate_target


@pytest.mark.parametrize(
    "override",
    [
        {"host": "example.com"},
        {"database": "codespy"},
        {"port": 5432},
        {"marker": ""},
        {"project": "codespy"},
        {"database": "codespy_p1_test_x;DROP"},
    ],
)
def test_refuse_unsafe_target(override):
    values = {
        "host": "127.0.0.1",
        "port": 55432,
        "database": "codespy_p1_test_abc",
        "marker": "a" * 32,
        "project": "codespy-phase1-tests",
    }
    values.update(override)
    with pytest.raises(ValueError):
        validate_target(**values)


def test_valid_disposable_target():
    validate_target(
        host="127.0.0.1",
        port=55432,
        database="codespy_p1_test_abc",
        marker="a" * 32,
        project="codespy-phase1-tests",
    )


def test_cleanup_recovers_partially_provisioned_cluster(tmp_path, monkeypatch):
    import sys

    from scripts import phase1_db as db

    path = tmp_path / "owned.env"
    path.write_text("synthetic-owner-file")
    called = []
    monkeypatch.setattr(sys, "argv", ["phase1_db.py", "cleanup", "--env-file", str(path)])
    monkeypatch.setattr(
        db, "read_env", lambda p: {"CODESPY_TEST_DATABASE": "codespy_p1_test_fixture"}
    )
    monkeypatch.setattr(db, "assert_container", lambda v: called.append("container-verified"))
    monkeypatch.setattr(
        db,
        "verify_marker",
        lambda *a: (_ for _ in ()).throw(ValueError("database marker not created yet")),
    )
    monkeypatch.setattr(db, "compose", lambda v, *a: called.append(a[0]))
    db.main()
    assert called == ["container-verified", "down"] and not path.exists()
