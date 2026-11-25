import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api/client";
import type {
  EvidencePointerV1,
  SearchResponseV1,
  DenseSearchResponseV1,
  HybridSearchResponseV1,
  ContextPacketV1,
  HybridSearchHitV1,
  MemoryCreateRequestV1,
  MemoryKind,
  SearchHitV1,
  SearchMode,
} from "./types";

export interface RetrievalWorkbenchProps {
  repositoryId: string;
  snapshotId: string;
  csrfToken: string;
}

/** All state, queries and commands of the retrieval workbench. Tabs read it through context. */
export function useRetrievalController({
  repositoryId,
  snapshotId,
  csrfToken,
}: RetrievalWorkbenchProps) {
  const queryClient = useQueryClient();
  const proposalCommandRef = useRef<{ body: string; key: string } | null>(null);
  const [activeTab, setActiveTab] = useState<"search" | "context" | "memory">(
    "search",
  );
  const [searchMode, setSearchMode] = useState<SearchMode>("hybrid");
  const [query, setQuery] = useState("");
  const [copyStatus, setCopyStatus] = useState("");
  const searchInputRef = useRef<HTMLInputElement>(null);
  const composingSearchRef = useRef(false);
  const [limit, setLimit] = useState(10);
  const [denseIndexId, setDenseIndexId] = useState("");

  // Context states
  const [maxTokens, setMaxTokens] = useState(2048);
  const [overheadTokens, setOverheadTokens] = useState(128);
  const [preamble, setPreamble] = useState(
    "You are an expert security software engineer assisting with CodeSPY.",
  );
  const [includeMemory, setIncludeMemory] = useState(false);
  const [preview, setPreview] = useState<
    import("../../api/generated/types.gen").ContextPreviewV1 | null
  >(null);
  const [lastPacket, setLastPacket] = useState<ContextPacketV1 | null>(null);

  // Source View Modal
  const [selectedFileForView, setSelectedFileForView] = useState<{
    fileId: string;
    filePath: string;
    startLine?: number;
    endLine?: number;
  } | null>(null);

  // Memory states
  const [memoryQuery, setMemoryQuery] = useState("");
  const [memoryMode, setMemoryMode] = useState<
    "off" | "current_only" | "historical_hints"
  >("current_only");
  const [memoryDraft, setMemoryDraft] = useState("");
  const [newMemoryTopic, setNewMemoryTopic] = useState("");
  const [newMemoryKind, setNewMemoryKind] =
    useState<MemoryKind>("architecture_fact");
  const [newMemorySummary, setNewMemorySummary] = useState("");
  const [newMemoryEvidenceFile, setNewMemoryEvidenceFile] = useState("");
  const [newMemoryStartLine, setNewMemoryStartLine] = useState<
    number | undefined
  >();
  const [newMemoryEndLine, setNewMemoryEndLine] = useState<
    number | undefined
  >();
  const [newMemorySymbol, setNewMemorySymbol] = useState("");
  const [memorySuccessMsg, setMemorySuccessMsg] = useState("");
  const [memoryValidation, setMemoryValidation] = useState("");

  const [lastSearch, setLastSearch] = useState<
    SearchResponseV1 | DenseSearchResponseV1 | HybridSearchResponseV1 | null
  >(null);
  const [pinnedEvidence, setPinnedEvidence] =
    useState<EvidencePointerV1 | null>(null);
  const searchSequenceRef = useRef(0);
  const assembleSequenceRef = useRef(0);

  // Search Mutation
  const searchMutation = useMutation({
    mutationFn: async () => {
      if (!query.trim()) return null;
      const seq = ++searchSequenceRef.current;
      let result;
      if (searchMode === "exact") {
        result = await api.searchExact(
          repositoryId,
          snapshotId,
          query,
          limit,
          csrfToken,
        );
      } else if (searchMode === "lexical") {
        result = await api.searchLexical(
          repositoryId,
          snapshotId,
          query,
          limit,
          csrfToken,
        );
      } else if (searchMode === "dense") {
        result = await api.searchDense(
          repositoryId,
          snapshotId,
          query,
          denseIndexId || undefined,
          limit,
          csrfToken,
        );
      } else {
        result = await api.searchHybrid(
          repositoryId,
          snapshotId,
          query,
          limit,
          denseIndexId || undefined,
          csrfToken,
        );
      }
      return { result, seq };
    },
    onSuccess: (data) => {
      if (data && data.seq === searchSequenceRef.current && data.result) {
        setLastSearch(data.result);
      }
    },
  });

  // Context Assembly Mutation
  const assembleMutation = useMutation({
    mutationFn: async () => {
      const seq = ++assembleSequenceRef.current;
      const hits: HybridSearchHitV1[] | undefined = lastSearch?.hits.map(
        (hit) => ({
          ...hit,
          match_modalities:
            "match_modalities" in hit
              ? hit.match_modalities
              : ["match_type" in hit ? String(hit.match_type) : "dense"],
          modality_ranks: "modality_ranks" in hit ? hit.modality_ranks : {},
        }),
      );
      if (includeMemory) {
        const result = await api.previewContext(
          repositoryId,
          snapshotId,
          {
            query: lastSearch?.query || query || "CodeSPY retrieval inquiry",
            candidate_hits: hits,
            chunk_set_id: lastSearch?.chunk_set_id,
            budget: {
              max_tokens: maxTokens,
              reserved_overhead_tokens: overheadTokens,
            },
            memory_mode: memoryMode,
            system_preamble: preamble || undefined,
          },
          csrfToken,
        );
        return { res: result.packet, preview: result, seq };
      }
      const res = await api.assembleContext(
        repositoryId,
        snapshotId,
        {
          query: lastSearch?.query || query || "CodeSPY retrieval inquiry",
          candidate_hits: hits,
          chunk_set_id: lastSearch?.chunk_set_id,
          budget: {
            max_tokens: maxTokens,
            reserved_overhead_tokens: overheadTokens,
          },
          system_preamble: preamble || undefined,
        },
        csrfToken,
      );
      return { res, preview: null, seq };
    },
    onSuccess: (data) => {
      if (data && data.seq === assembleSequenceRef.current && data.res) {
        setLastPacket(data.res);
        setPreview(data.preview);
      }
    },
  });

  // Memory Query
  const memoriesQuery = useQuery({
    queryKey: [
      "repo_memories",
      repositoryId,
      snapshotId,
      memoryQuery,
      memoryMode,
    ],
    queryFn: ({ signal }) =>
      api.queryMemories(
        repositoryId,
        snapshotId,
        { query: memoryQuery || undefined, history_mode: memoryMode },
        signal,
      ),
  });

  // Memory Create Mutation
  const recordMemoryMutation = useMutation({
    mutationFn: async (payload: MemoryCreateRequestV1) => {
      const body = JSON.stringify({ ...payload, request_key: undefined });
      if (proposalCommandRef.current?.body !== body)
        proposalCommandRef.current = { body, key: crypto.randomUUID() };
      const key = proposalCommandRef.current.key;
      return await api.recordMemory(
        repositoryId,
        snapshotId,
        { ...payload, request_key: key },
        csrfToken,
        key,
      );
    },
    onSuccess: () => {
      proposalCommandRef.current = null;
      void queryClient.invalidateQueries({
        queryKey: ["memory-review", repositoryId, snapshotId],
      });
      setMemorySuccessMsg(
        "Memory candidate submitted for review. It is not used by models yet.",
      );
      setNewMemoryTopic("");
      setNewMemorySummary("");
      setNewMemoryEvidenceFile("");
      setNewMemoryStartLine(undefined);
      setNewMemoryEndLine(undefined);
      setNewMemorySymbol("");
      setPinnedEvidence(null);
      memoriesQuery.refetch();
    },
  });

  // Source View Query
  const sourceViewQuery = useQuery({
    queryKey: [
      "source_view",
      repositoryId,
      snapshotId,
      selectedFileForView?.fileId,
      selectedFileForView?.startLine,
      selectedFileForView?.endLine,
    ],
    queryFn: ({ signal }) => {
      if (!selectedFileForView) return null;
      return api.sourceView(
        repositoryId,
        snapshotId,
        selectedFileForView.fileId,
        {
          start_line: selectedFileForView.startLine,
          end_line: selectedFileForView.endLine,
          representation: "model_safe",
        },
        signal,
      );
    },
    enabled: !!selectedFileForView,
  });

  const handlePinAsMemory = (hit: SearchHitV1 | HybridSearchHitV1) => {
    setActiveTab("memory");
    setNewMemoryTopic(hit.symbol_name ?? hit.file_path);
    setNewMemorySummary(
      `Evidence identified in ${hit.file_path} via retrieval query '${lastSearch?.query ?? query}'.`,
    );
    setNewMemoryEvidenceFile(hit.file_path);
    setNewMemoryStartLine(hit.start_line);
    setNewMemoryEndLine(hit.end_line);
    setNewMemorySymbol(hit.symbol_name ?? "");
    setPinnedEvidence({
      file_path: hit.file_path,
      start_line: hit.start_line,
      end_line: hit.end_line,
      symbol_name: hit.symbol_name,
      chunk_id: hit.chunk_id,
      chunk_set_id: hit.chunk_set_id,
      config_hash: hit.config_hash,
      materialization_hash: hit.materialization_hash,
      safe_content_sha256: hit.safe_content_sha256,
      sha256: hit.content_sha256,
    });
  };

  const copyCitation = async (citation: string) => {
    try {
      await navigator.clipboard.writeText(citation);
      setCopyStatus("Citation copied.");
    } catch {
      setCopyStatus(
        "Copying is unavailable. Select the citation text to copy it manually.",
      );
    }
  };

  return {
    repositoryId,
    snapshotId,
    csrfToken,
    queryClient,
    proposalCommandRef,
    searchInputRef,
    composingSearchRef,
    searchSequenceRef,
    assembleSequenceRef,
    searchMutation,
    assembleMutation,
    memoriesQuery,
    recordMemoryMutation,
    sourceViewQuery,
    handlePinAsMemory,
    copyCitation,
    activeTab,
    setActiveTab,
    searchMode,
    setSearchMode,
    query,
    setQuery,
    copyStatus,
    setCopyStatus,
    limit,
    setLimit,
    denseIndexId,
    setDenseIndexId,
    maxTokens,
    setMaxTokens,
    overheadTokens,
    setOverheadTokens,
    preamble,
    setPreamble,
    includeMemory,
    setIncludeMemory,
    preview,
    setPreview,
    lastPacket,
    setLastPacket,
    selectedFileForView,
    setSelectedFileForView,
    memoryQuery,
    setMemoryQuery,
    memoryMode,
    setMemoryMode,
    memoryDraft,
    setMemoryDraft,
    newMemoryTopic,
    setNewMemoryTopic,
    newMemoryKind,
    setNewMemoryKind,
    newMemorySummary,
    setNewMemorySummary,
    newMemoryEvidenceFile,
    setNewMemoryEvidenceFile,
    newMemoryStartLine,
    setNewMemoryStartLine,
    newMemoryEndLine,
    setNewMemoryEndLine,
    newMemorySymbol,
    setNewMemorySymbol,
    memorySuccessMsg,
    setMemorySuccessMsg,
    memoryValidation,
    setMemoryValidation,
    lastSearch,
    setLastSearch,
    pinnedEvidence,
    setPinnedEvidence,
  };
}

export type RetrievalController = ReturnType<typeof useRetrievalController>;
