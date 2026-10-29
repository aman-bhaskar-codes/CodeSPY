import type { ReactNode } from "react";

export function InlineNotice({ children, tone = "info" }: { children: ReactNode; tone?: "info" | "danger" | "success" }) {
  return <div className={`inline-notice inline-notice--${tone}`} role={tone === "danger" ? "alert" : "status"}>{children}</div>;
}
