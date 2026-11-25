import { ApiError } from "../api/client";

/** Job states after which polling stops. */
export const terminal = new Set([
  "succeeded",
  "partial",
  "failed",
  "cancelled",
]);

export const label = (value: string) => value.replaceAll("_", " ");

/** The one message shown for a failed workspace request. Never includes server detail. */
export function problem(error: unknown): string {
  if (error instanceof ApiError && error.status === 401)
    return "Your session expired. Sign in again before resubmitting sources.";
  if (error instanceof ApiError && error.status === 403)
    return "Access changed. Reload your session and repository list.";
  if (error instanceof ApiError && error.code === "analysis_unavailable")
    return "Structural analysis is unavailable. Start the qualified local runtime before retrying.";
  if (error instanceof ApiError && error.code === "scan_unavailable")
    return "Scanner analysis is unavailable. Start the qualified local runtime before retrying.";
  if (error instanceof ApiError && error.status === 409)
    return "This request conflicts with existing work. Reload its status before retrying.";
  if (error instanceof ApiError && error.status === 429)
    return "The work limit has been reached. Wait for current work to finish.";
  return "The request could not be confirmed. Reload its status before starting new work.";
}
