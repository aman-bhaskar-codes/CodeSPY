import type { ComponentPropsWithRef } from "react";
import styles from "./Button.module.css";

type Props = ComponentPropsWithRef<"button"> & {
  intent?: "primary" | "neutral" | "danger";
  pending?: boolean;
};

export function Button({
  intent = "neutral",
  pending = false,
  disabled,
  children,
  className = "",
  type = "button",
  ...props
}: Props) {
  return (
    <button
      {...props}
      type={type}
      disabled={disabled || pending}
      aria-busy={pending || undefined}
      data-intent={intent}
      data-pending={pending || undefined}
      className={`${styles.button} ${className}`}
    >
      <span className={styles.label}>{children}</span>
      {pending && <span className={styles.spinner} aria-hidden="true" />}
    </button>
  );
}
