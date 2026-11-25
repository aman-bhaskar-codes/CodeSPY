export interface Command {
  id: string;
  title: string;
  /** Extra words that also match, e.g. "view", "toggle". */
  keywords?: string;
  shortcut?: string;
  run: () => void;
}

/** Subsequence match with a bonus for word starts and contiguous runs; null when it does not match. */
export function fuzzyScore(query: string, text: string): number | null {
  const q = query.trim().toLowerCase();
  if (!q) return 0;
  const t = text.toLowerCase();
  let score = 0;
  let from = 0;
  let previous = -2;
  for (const ch of q) {
    if (ch === " ") continue;
    const at = t.indexOf(ch, from);
    if (at < 0) return null;
    score += 1;
    if (at === previous + 1) score += 3;
    if (at === 0 || t[at - 1] === " ") score += 2;
    previous = at;
    from = at + 1;
  }
  return score - t.length / 100;
}

export function filterCommands(
  commands: readonly Command[],
  query: string,
): Command[] {
  const scored: { command: Command; score: number; index: number }[] = [];
  commands.forEach((command, index) => {
    const score = fuzzyScore(
      query,
      `${command.title} ${command.keywords ?? ""}`,
    );
    if (score !== null) scored.push({ command, score, index });
  });
  scored.sort((a, b) => b.score - a.score || a.index - b.index);
  return scored.map((s) => s.command);
}
