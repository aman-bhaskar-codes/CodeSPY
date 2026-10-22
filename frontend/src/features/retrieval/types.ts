export type SearchMode = "hybrid" | "lexical" | "dense" | "exact";

export type ChunkKind =
  | "function"
  | "class"
  | "method"
  | "module"
  | "block"
  | "statement"
  | "comment";

export interface SearchHitV1 {
  chunk_id: string;
  file_id: string;
  file_path: string;
  chunk_kind: ChunkKind;
  symbol_name: string | null;
  qualified_name: string | null;
  start_byte: number;
  end_byte: number;
  start_line: number;
  end_line: number;
  score: number;
  snippet: string;
  content_sha256: string;
  match_type?: string;
}

export interface SearchResponseV1 {
  query: string;
  total_candidates: number;
  hits: SearchHitV1[];
  execution_ms: number;
}

export interface DenseSearchResponseV1 {
  query: string;
  index_id: string;
  embedding_model: string;
  total_candidates: number;
  hits: SearchHitV1[];
  execution_ms: number;
}

export interface HybridSearchHitV1 {
  chunk_id: string;
  file_id: string;
  file_path: string;
  chunk_kind: ChunkKind;
  symbol_name: string | null;
  qualified_name: string | null;
  start_byte: number;
  end_byte: number;
  start_line: number;
  end_line: number;
  score: number;
  snippet: string;
  content_sha256: string;
  match_modalities: string[];
  modality_ranks: Record<string, number>;
}

export interface HybridSearchResponseV1 {
  query: string;
  total_candidates: number;
  hits: HybridSearchHitV1[];
  execution_ms: number;
}

export interface ContextItemV1 {
  chunk_id: string;
  file_id: string;
  file_path: string;
  symbol_name: string | null;
  qualified_name: string | null;
  chunk_kind: ChunkKind;
  start_line: number;
  end_line: number;
  start_byte: number;
  end_byte: number;
  token_count: number;
  byte_count: number;
  safe_content: string;
  citation_key: string;
  relevance_score: number;
  match_modalities: string[];
}

export interface ContextBudgetV1 {
  max_tokens: number;
  max_bytes?: number;
  reserved_overhead_tokens?: number;
  tokenizer_name?: string;
}

export interface ContextAssemblyRequestV1 {
  query: string;
  candidate_hits?: HybridSearchHitV1[];
  budget?: ContextBudgetV1;
  deduplicate_files?: boolean;
  system_preamble?: string;
}

export interface ContextPacketV1 {
  packet_id: string;
  query: string;
  system_preamble: string | null;
  items: ContextItemV1[];
  total_tokens: number;
  total_bytes: number;
  items_count: number;
  omitted_count: number;
  packet_sha256: string;
  created_at: string;
}

export type MemoryKind =
  | "triage_resolution"
  | "security_finding_investigation"
  | "architecture_invariant"
  | "developer_hypothesis"
  | "review_consensus";

export interface EvidencePointerV1 {
  file_path: string;
  start_line?: number | null;
  end_line?: number | null;
  symbol_name?: string | null;
  finding_id?: string | null;
  cpg_node_id?: number | null;
  notes?: string | null;
}

export interface MemoryEntryV1 {
  memory_id: string;
  memory_kind: MemoryKind;
  topic: string;
  summary: string;
  evidence_pointers: EvidencePointerV1[];
  confidence: number;
  superseded_by?: string | null;
  created_by?: string | null;
  created_at: string;
}

export interface MemoryCreateRequestV1 {
  memory_kind: MemoryKind;
  topic: string;
  summary: string;
  evidence_pointers: EvidencePointerV1[];
  confidence?: number;
}

export interface MemoryQueryResponseV1 {
  items: MemoryEntryV1[];
  total_candidates: number;
}

export interface SourceViewResultV1 {
  file_id: string;
  file_path: string;
  file_sha256: string;
  representation: "original" | "model_safe";
  requested_range: { start_byte: number; end_byte: number };
  actual_range: { start_byte: number; end_byte: number };
  content_text: string;
  content_sha256: string;
  total_file_size: number;
  policy_version: number;
  redaction_map_digest: string;
  interval_mappings: Array<{
    original_start: number;
    original_end: number;
    safe_start: number;
    safe_end: number;
    is_redacted: boolean;
  }>;
  remaining_privacy_risks: string[];
  is_omitted: boolean;
  omission_reason?: string | null;
}
