import { useInfiniteQuery, useMutation, useQuery } from "@tanstack/react-query";
import { api } from "../api/client";

/** The signed-in session and sign-out. */
export function useSessionData() {
  const session = useQuery({
    queryKey: ["session"],
    queryFn: ({ signal }) => api.session(signal),
    retry: false,
  });
  const logout = useMutation({
    mutationFn: () => api.logout(session.data!.csrf_token),
    onSuccess: () => window.dispatchEvent(new Event("codespy:access-required")),
  });
  return { session, logout };
}

export type SessionQuery = ReturnType<typeof useSessionData>["session"];

/** Repositories the principal can reach, plus the selected one when it is outside the loaded pages. */
export function useRepositories(repository: string, session: SessionQuery) {
  const repositories = useInfiniteQuery({
    queryKey: ["repositories"],
    initialPageParam: null as string | null,
    queryFn: ({ signal, pageParam }) => api.repositories(signal, pageParam),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    enabled: !!session.data,
  });
  const pagedRepositories =
    repositories.data?.pages.flatMap((page) => page.items) ?? [];
  const selectedMetadata = useQuery({
    queryKey: ["repository-metadata", repository],
    queryFn: ({ signal }) => api.repositoryMetadata(repository, signal),
    enabled:
      !!repository &&
      !!session.data &&
      repositories.isSuccess &&
      !pagedRepositories.some((item) => item.id === repository),
    retry: false,
  });
  const repositoryItems =
    selectedMetadata.data?.id === repository &&
    !pagedRepositories.some((item) => item.id === repository)
      ? [...pagedRepositories, selectedMetadata.data]
      : pagedRepositories;
  return { repositories, selectedMetadata, repositoryItems };
}
