import { useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { api, mutationHeaders, request } from "../api/client";
import type { SubmissionRecordV1 } from "../api/generated/types.gen";
import type { WorkspaceDeps } from "./controllerDeps";

/** Adding a codebase: the upload form's state and the three ways to submit a source. */
export function useIntake(
  { client, session, selection, retryKeys }: WorkspaceDeps,
  refetchRepositories: () => unknown,
) {
  const {
    repository,
    setRepository,
    setSubmissionId,
    setJobId,
    setRestoredSnapshot,
    setPages,
    responseIsCurrent,
  } = selection;
  const [creatingRepository, setCreatingRepository] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [validation, setValidation] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  const uploadKeyRef = useRef(crypto.randomUUID());
  const acquire = useMutation({
    mutationFn: async (ref: string) => {
      if (!session.data) throw new Error("missing session");
      const intent = `github:${repository}:${ref}`;
      if (!retryKeys.current.has(intent))
        retryKeys.current.set(intent, crypto.randomUUID());
      const isCurrent = responseIsCurrent();
      const record = await api.acquireGitHub(
        repository,
        ref,
        session.data.csrf_token,
        retryKeys.current.get(intent)!,
      );
      return { record, isCurrent };
    },
    onSuccess: ({ record, isCurrent }) => {
      if (!isCurrent()) return;
      setSubmissionId(record.id);
      setJobId(record.job_id ?? null);
      setRestoredSnapshot(null);
      setPages([null]);
    },
  });
  const importUrl = useMutation({
    mutationFn: async (url: string) => {
      if (!session.data) throw new Error("missing session");
      const intent = `github-url:${url}`;
      if (!retryKeys.current.has(intent))
        retryKeys.current.set(intent, crypto.randomUUID());
      const isCurrent = responseIsCurrent();
      const record = await api.importGitHubUrl(
        url,
        session.data.csrf_token,
        retryKeys.current.get(intent)!,
      );
      return { record, isCurrent };
    },
    onSuccess: ({ record, isCurrent }) => {
      if (!isCurrent()) return;
      setRepository(record.repository_id);
      setSubmissionId(record.id);
      setJobId(record.job_id ?? null);
      setRestoredSnapshot(null);
      setPages([null]);
      void refetchRepositories();
    },
  });
  const upload = useMutation({
    mutationFn: async () => {
      const isCurrent = responseIsCurrent();
      if (!file || !repository || !session.data)
        throw new Error("missing input");
      setRestoredSnapshot(null);
      const reserved = await request<SubmissionRecordV1>(
        `/repositories/${repository}/uploads`,
        {
          method: "POST",
          headers: mutationHeaders(
            session.data.csrf_token,
            uploadKeyRef.current,
          ),
          body: JSON.stringify({
            repository_id: repository,
            filename: file.name,
            declared_size: file.size,
          }),
        },
      );
      if (isCurrent()) setSubmissionId(reserved.id);
      const result = await request<SubmissionRecordV1>(
        `/submissions/${reserved.id}/content`,
        {
          method: "PUT",
          headers: {
            "Content-Type": "application/zip",
            "X-CSRF-Token": session.data.csrf_token,
          },
          body: file,
        },
      );
      if (isCurrent()) setJobId(result.job_id ?? null);
      await client.invalidateQueries({ queryKey: ["submission", reserved.id] });
    },
  });
  return {
    creatingRepository,
    setCreatingRepository,
    file,
    setFile,
    validation,
    setValidation,
    inputRef,
    uploadKeyRef,
    acquire,
    importUrl,
    upload,
  };
}
