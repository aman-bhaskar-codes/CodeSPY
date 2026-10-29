import type { ComponentPropsWithoutRef, ReactNode } from "react";
import styles from "./Panel.module.css";

type Props = Omit<ComponentPropsWithoutRef<"section">, "title"> & {
  title: string;
  id: string;
  actions?: ReactNode;
};
export function Panel({
  title,
  id,
  actions,
  children,
  className = "",
  ...props
}: Props) {
  return (
    <section
      {...props}
      id={id}
      aria-labelledby={`${id}-heading`}
      className={`${styles.panel} ${className}`}
    >
      <div className={styles.heading}>
        <h2 id={`${id}-heading`}>{title}</h2>
        {actions}
      </div>
      {children}
    </section>
  );
}
