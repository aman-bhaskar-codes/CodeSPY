import * as Dialog from "@radix-ui/react-dialog";
import { useId, useRef, type ReactNode } from "react";
import { Button } from "./Button";
import styles from "./Modal.module.css";

interface Props {
  open: boolean;
  title: string;
  description?: string;
  children: ReactNode;
  onClose: () => void;
  busy?: boolean;
  variant?: "dialog" | "drawer";
}

export function Modal({
  open,
  title,
  description,
  children,
  onClose,
  busy = false,
  variant = "dialog",
}: Props) {
  const previousFocus = useRef<HTMLElement | null>(null);
  const descriptionId = useId();
  return (
    <Dialog.Root
      open={open}
      onOpenChange={(next) => {
        if (!next && !busy) onClose();
      }}
    >
      <Dialog.Portal>
        <Dialog.Overlay className={styles.overlay} />
        <Dialog.Content
          className={`${styles.content} ${variant === "drawer" ? styles.drawer : ""}`}
          aria-describedby={description ? descriptionId : undefined}
          onOpenAutoFocus={() => {
            previousFocus.current =
              document.activeElement instanceof HTMLElement
                ? document.activeElement
                : null;
          }}
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            if (
              previousFocus.current?.isConnected &&
              !previousFocus.current.closest("[hidden]")
            )
              previousFocus.current.focus();
            else document.getElementById("workspace-content")?.focus();
          }}
          onEscapeKeyDown={(event) => {
            if (busy) event.preventDefault();
          }}
          onPointerDownOutside={(event) => {
            if (busy) event.preventDefault();
          }}
        >
          <div className={styles.heading}>
            <Dialog.Title>{title}</Dialog.Title>
            <Button
              disabled={busy}
              onClick={onClose}
              aria-label={`Close ${title}`}
            >
              Close
            </Button>
          </div>
          {description && (
            <Dialog.Description id={descriptionId}>
              {description}
            </Dialog.Description>
          )}
          {children}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
