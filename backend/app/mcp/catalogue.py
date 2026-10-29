"""Single typed catalogue shared by discovery, validation and result projection."""

from pydantic import BaseModel

from app.contracts.context import ContextPacketV1
from app.contracts.indexing import IndexStatusV1
from app.contracts.mcp import (
    AssembleContextInputV1,
    GetEvidenceInputV1,
    GetIndexStatusInputV1,
    GetInvestigationResultInputV1,
    GetInvestigationStatusInputV1,
    GetSourceViewInputV1,
    GraphNeighborsInputV1,
    GraphNeighborsResultV1,
    ListClaimsInputV1,
    ListClaimsResultV1,
    ListFindingsInputV1,
    ListFindingsResultV1,
    McpEvidenceResultV1,
    McpInvestigationResultV1,
    McpInvestigationStatusV1,
    QueryMemoryInputV1,
    SearchExactInputV1,
    SearchHybridInputV1,
    TraceFeatureInputV1,
)
from app.contracts.memory import MemoryQueryResponseV1
from app.contracts.search import HybridSearchResponseV1, SearchResponseV1
from app.contracts.source_view import SourceViewResultV1
from app.contracts.trace import FeatureTraceV1


class McpHybridResultV1(HybridSearchResponseV1):
    dense_coverage: str = "unavailable"
    fallback_mode: str = "lexical_only"


CONTRACTS: dict[str, tuple[type[BaseModel], type[BaseModel]]] = {
    "get_index_status": (GetIndexStatusInputV1, IndexStatusV1),
    "get_source_view": (GetSourceViewInputV1, SourceViewResultV1),
    "search_exact": (SearchExactInputV1, SearchResponseV1),
    "search_hybrid": (SearchHybridInputV1, McpHybridResultV1),
    "assemble_context": (AssembleContextInputV1, ContextPacketV1),
    "query_memory": (QueryMemoryInputV1, MemoryQueryResponseV1),
    "get_investigation_status": (GetInvestigationStatusInputV1, McpInvestigationStatusV1),
    "get_investigation_result": (GetInvestigationResultInputV1, McpInvestigationResultV1),
    "list_findings": (ListFindingsInputV1, ListFindingsResultV1),
    "get_evidence": (GetEvidenceInputV1, McpEvidenceResultV1),
    "graph_neighbors": (GraphNeighborsInputV1, GraphNeighborsResultV1),
    "trace_feature": (TraceFeatureInputV1, FeatureTraceV1),
    "list_claims": (ListClaimsInputV1, ListClaimsResultV1),
}
