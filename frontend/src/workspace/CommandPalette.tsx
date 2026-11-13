import * as Dialog from "@radix-ui/react-dialog";
import { useId, useMemo, useState } from "react";
import { filterCommands, type Command } from "./commands";
import styles from "./CommandPalette.module.css";

interface Props {
  open: boolean;
  commands: readonly Command[];
  onClose: () => void;
}

export function CommandPalette({ open, commands, onClose }: Props) {
  return (
    <Dialog.Root open={open} onOpenChange={(next) => !next && onClose()}>
      <Dialog.Portal>
        <Dialog.Overlay className={styles.overlay} />
        <Dialog.Content
          className={styles.content}
          aria-describedby={undefined}
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            document.getElementById("workspace-content")?.focus();
          }}
        >
          <Dialog.Title className={styles.title}>Command palette</Dialog.Title>
          <PaletteBody commands={commands} onClose={onClose} />
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

function PaletteBody({
  commands,
  onClose,
}: {
  commands: readonly Command[];
  onClose: () => void;
}) {
  const ids = useId();
  const [query, setQuery] = useState("");
  const [index, setIndex] = useState(0);
  const results = useMemo(
    () => filterCommands(commands, query),
    [commands, query],
  );
  const current = results[Math.min(index, results.length - 1)];
  function run(command: Command | undefined) {
    if (!command) return;
    onClose();
    command.run();
  }
  return (
    <>
      <input
        autoFocus
        role="combobox"
        aria-label="Type a command"
        aria-expanded="true"
        aria-controls={`${ids}-list`}
        aria-autocomplete="list"
        aria-activedescendant={current ? `${ids}-${current.id}` : undefined}
        placeholder="Type a command"
        value={query}
        className={styles.input}
        onChange={(event) => {
          setQuery(event.target.value);
          setIndex(0);
        }}
        onKeyDown={(event) => {
          if (event.key === "ArrowDown") {
            setIndex(Math.min(results.length - 1, index + 1));
          } else if (event.key === "ArrowUp") {
            setIndex(Math.max(0, index - 1));
          } else if (event.key === "Home") {
            setIndex(0);
          } else if (event.key === "End") {
            setIndex(Math.max(0, results.length - 1));
          } else if (event.key === "Enter") {
            run(current);
          } else return;
          event.preventDefault();
        }}
      />
      <ul
        id={`${ids}-list`}
        role="listbox"
        aria-label="Commands"
        className={styles.list}
      >
        {results.map((command) => (
          <li
            key={command.id}
            id={`${ids}-${command.id}`}
            role="option"
            aria-selected={command === current}
            className={styles.option}
            onMouseMove={() => setIndex(results.indexOf(command))}
            onClick={() => run(command)}
          >
            <span>{command.title}</span>
            {command.shortcut && <kbd>{command.shortcut}</kbd>}
          </li>
        ))}
        {results.length === 0 && (
          <li role="presentation" className={styles.empty}>
            No matching commands
          </li>
        )}
      </ul>
      <p className={styles.live} role="status">
        {results.length} {results.length === 1 ? "command" : "commands"}
      </p>
    </>
  );
}
