from app.db.base import Base
from app.models import tables
from sqlalchemy import ForeignKeyConstraint, Table


def test_twelve_tables_and_scoped_edge_keys():
    # persistence keeps twelve original mappings; ingestion registers additive metadata.
    assert len([v for v in vars(tables).values() if isinstance(v, Table)]) == 12
    edges = Base.metadata.tables["cpg_edges"]
    fks = [c for c in edges.constraints if isinstance(c, ForeignKeyConstraint)]
    assert len(fks) == 3
    assert all(len(c.columns) == 4 for c in fks)
