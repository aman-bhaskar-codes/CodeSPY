import type { ArchitectureGraphPageV1 } from "../../api/generated/types.gen";

interface Props {
  /** Component name -> layer (0 is the top: it imports, and is not imported by, the layers below). */
  layers: ReadonlyMap<string, number>;
  graph: ArchitectureGraphPageV1;
  fileCounts: ReadonlyMap<string, number>;
}

const W = 720;
const ROW = 84;
const BOX_W = 150;
const BOX_H = 34;

/** A layered SVG of component-to-component import counts (resolved edges only). */
export function ComponentGraph({ layers, graph, fileCounts }: Props) {
  const groupOf = new Map<string, string>();
  for (const node of graph.nodes)
    if (node.group) groupOf.set(node.id, node.group);
  const edges = new Map<string, number>();
  for (const link of graph.links) {
    if (link.resolution !== "resolved") continue;
    const a = groupOf.get(link.source);
    const b = groupOf.get(link.target);
    if (a && b && a !== b)
      edges.set(`${a}\u0000${b}`, (edges.get(`${a}\u0000${b}`) ?? 0) + 1);
  }
  const rows = new Map<number, string[]>();
  for (const [name, layer] of layers)
    rows.set(layer, [...(rows.get(layer) ?? []), name].sort());
  const ordered = [...rows.entries()].sort(([a], [b]) => a - b);
  if (ordered.length === 0)
    return <p className="muted">No component structure to draw.</p>;
  const position = new Map<string, { x: number; y: number }>();
  ordered.forEach(([, names], row) =>
    names.forEach((name, i) =>
      position.set(name, {
        x: ((i + 1) * W) / (names.length + 1),
        y: 24 + row * ROW + BOX_H / 2,
      }),
    ),
  );
  const height = 48 + ordered.length * ROW;
  const incomplete =
    graph.links_total > graph.links.length ||
    (graph.nodes_total ?? 0) > graph.nodes.length;
  return (
    <figure>
      <svg
        role="img"
        aria-label="Component dependency graph"
        viewBox={`0 0 ${W} ${height}`}
        width="100%"
      >
        {[...edges.entries()].map(([key, count]) => {
          const [a, b] = key.split("\u0000");
          const from = position.get(a);
          const to = position.get(b);
          if (!from || !to) return null;
          return (
            <g key={key}>
              <line
                x1={from.x}
                y1={from.y + BOX_H / 2}
                x2={to.x}
                y2={to.y - BOX_H / 2}
                stroke="currentColor"
                strokeOpacity={0.45}
                strokeWidth={Math.min(1 + Math.log2(count), 5)}
              />
              <title>{`${a} imports ${b}: ${count}`}</title>
            </g>
          );
        })}
        {[...position.entries()].map(([name, { x, y }]) => (
          <g key={name}>
            <rect
              x={x - BOX_W / 2}
              y={y - BOX_H / 2}
              width={BOX_W}
              height={BOX_H}
              rx={6}
              fill="var(--cs-panel)"
              stroke="currentColor"
            />
            <text
              x={x}
              y={y + 4}
              textAnchor="middle"
              fontSize="12"
              fill="currentColor"
            >
              {name.length > 20 ? `${name.slice(0, 19)}…` : name}
              {` (${fileCounts.get(name) ?? 0})`}
            </text>
          </g>
        ))}
      </svg>
      <figcaption className="muted">
        Arrows point from the importing component to the imported one; width
        grows with the number of resolved imports.
        {incomplete &&
          ` Showing ${graph.links.length} of ${graph.links_total} links.`}
      </figcaption>
    </figure>
  );
}
