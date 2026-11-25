import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import Ajv2020 from "ajv/dist/2020";
import { describe, expect, it } from "vitest";
import {
  AGENT_EVENT_TYPES,
  UnsupportedProtocolVersion,
  isKnownAgentEvent,
  isKnownEventType,
  isTransientEvent,
  parseAgentEvent,
  type AgentEvent,
} from ".";

const here = dirname(fileURLToPath(import.meta.url));
const frontend = resolve(here, "../../../..");
const schema = JSON.parse(
  readFileSync(
    resolve(frontend, "../docs/contracts/AgentEventV1.schema.json"),
    "utf8",
  ),
);
const FIXTURES = ["happy-path", "rejection", "crash-resume"] as const;

interface Fixture {
  name: string;
  session_id: string;
  events: AgentEvent[];
}
const load = (name: string): Fixture =>
  JSON.parse(
    readFileSync(resolve(here, "../fixtures", `${name}.json`), "utf8"),
  );

function validators() {
  const ajv = new Ajv2020({ strict: false, allErrors: true });
  ajv.addKeyword("discriminator");
  const envelope = ajv.compile({ ...schema.envelope, $defs: schema.$defs });
  const payloads = new Map(
    Object.entries(schema.payloads).map(([type, body]) => [
      type,
      ajv.compile({ ...(body as object), $defs: schema.$defs }),
    ]),
  );
  return { envelope, payloads };
}

describe("generated protocol types", () => {
  it("are current with the backend schema", () => {
    expect(() =>
      execFileSync(
        process.execPath,
        ["scripts/generate-agent-types.mjs", "--check"],
        {
          cwd: frontend,
          stdio: "pipe",
        },
      ),
    ).not.toThrow();
  });

  it("list exactly the types the schema defines", () => {
    expect([...AGENT_EVENT_TYPES].sort()).toEqual(
      Object.keys(schema.payloads).sort(),
    );
    expect(isKnownEventType("tool.call.started")).toBe(true);
    expect(isKnownEventType("something.new")).toBe(false);
  });

  it("narrow a payload by event type", () => {
    const event = load("happy-path").events.find(
      (e) => e.type === "tool.call.proposed",
    )!;
    if (!isKnownAgentEvent(event)) throw new Error("expected a known event");
    if (event.type === "tool.call.proposed") {
      const tool: string = event.payload.tool;
      const klass:
        | "read"
        | "edit"
        | "exec"
        | "git"
        | "network"
        | "mcp"
        | "re"
        | undefined = event.payload.class;
      expect(tool).toBe("read_file");
      expect(klass).toBe("read");
    }
  });
});

describe.each(FIXTURES)("recorded session %s", (name) => {
  const fixture = load(name);

  it("validates against the schema: envelope and the payload of its type", () => {
    const { envelope, payloads } = validators();
    for (const event of fixture.events) {
      expect(envelope(event), JSON.stringify(envelope.errors)).toBe(true);
      const check = payloads.get(event.type);
      expect(check, `no schema for ${event.type}`).toBeDefined();
      expect(
        check!(event.payload),
        `${event.type}: ${JSON.stringify(check!.errors)}`,
      ).toBe(true);
    }
  });

  it("is a gap-free sequence for one session and every event parses", () => {
    expect(fixture.events.map((e) => e.seq)).toEqual(
      fixture.events.map((_, i) => i + 1),
    );
    for (const event of fixture.events) {
      expect(event.session_id).toBe(fixture.session_id);
      expect(parseAgentEvent(event)).toBe(event);
      expect(isKnownAgentEvent(event)).toBe(true);
      expect(isTransientEvent(event)).toBe(false);
    }
  });
});

describe("parseAgentEvent", () => {
  const base = {
    v: 1,
    session_id: "10000000-0000-4000-8000-000000000001",
    seq: 5,
    type: "session.status",
    ts: "2026-11-25T10:00:00.000Z",
    turn: 1,
    payload: { status: "idle" },
  };

  it("rejects a protocol version it does not speak", () => {
    expect(() => parseAgentEvent({ ...base, v: 2 })).toThrow(
      UnsupportedProtocolVersion,
    );
    expect(() => parseAgentEvent({ ...base, v: undefined })).toThrow(
      UnsupportedProtocolVersion,
    );
  });

  it("carries unknown event types through so the client can ignore them", () => {
    const event = parseAgentEvent({
      ...base,
      type: "future.thing",
      payload: { x: 1 },
    });
    expect(isKnownAgentEvent(event)).toBe(false);
  });

  it("rejects malformed envelopes and a misplaced sequence 0", () => {
    for (const bad of [
      null,
      [],
      { ...base, seq: -1 },
      { ...base, seq: 1.5 },
      { ...base, type: 7 },
      { ...base, payload: [] },
      { ...base, seq: 0 }, // only transient events carry 0
      { ...base, type: "stream.reset" }, // and transient events must carry 0
    ]) {
      expect(() => parseAgentEvent(bad)).toThrow(TypeError);
    }
    const reset = parseAgentEvent({
      ...base,
      seq: 0,
      type: "stream.reset",
      payload: { reason: "replay_limit" },
    });
    expect(isTransientEvent(reset)).toBe(true);
  });
});
