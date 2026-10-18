from app.db.base import Base
from sqlalchemy import ForeignKeyConstraint


def test_twelve_tables_and_scoped_edge_keys():
    assert len(Base.metadata.tables) == 12
    edges = Base.metadata.tables["cpg_edges"]
    fks = [c for c in edges.constraints if isinstance(c, ForeignKeyConstraint)]
    assert len(fks) == 3
    assert all(len(c.columns) == 4 for c in fks)
