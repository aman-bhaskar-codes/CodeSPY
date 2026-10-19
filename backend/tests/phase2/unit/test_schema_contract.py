from app.models.phase2_tables import PHASE2_TABLES


def test_phase2_has_seventeen_scoped_tables():
    assert len(PHASE2_TABLES) == 17
    for name in ["source_submissions", "jobs", "inventory_entries", "object_records"]:
        assert "org_id" in PHASE2_TABLES[name].c
    assert {"reserved_bytes", "reservation_released_at", "retry_of_submission_id"} <= set(
        PHASE2_TABLES["source_submissions"].c.keys()
    )
    assert {"retry_of_job_id", "publication_checked_at"} <= set(PHASE2_TABLES["jobs"].c.keys())
