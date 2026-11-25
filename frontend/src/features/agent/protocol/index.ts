import {
  AGENT_EVENT_TYPES,
  AGENT_PROTOCOL_VERSION,
  TRANSIENT_EVENT_TYPES,
  type AgentEvent,
  type AgentEventType,
  type KnownAgentEvent,
} from "./types.gen";

export * from "./types.gen";

const known: ReadonlySet<string> = new Set(AGENT_EVENT_TYPES);
const transient: ReadonlySet<string> = new Set(TRANSIENT_EVENT_TYPES);

/** Thrown for an event whose protocol major version this client does not speak. */
export class UnsupportedProtocolVersion extends Error {
  constructor(readonly version: unknown) {
    super(
      "This client does not support the agent protocol version the server sent.",
    );
    this.name = "UnsupportedProtocolVersion";
  }
}

export function isKnownEventType(type: string): type is AgentEventType {
  return known.has(type);
}

/** Narrows to an event whose payload this client can read. Unknown types are ignored, not errors. */
export function isKnownAgentEvent(event: AgentEvent): event is KnownAgentEvent {
  return known.has(event.type);
}

/** Events delivered to one connection and never stored: they carry no sequence number. */
export function isTransientEvent(event: AgentEvent): boolean {
  return transient.has(event.type);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * Checks the envelope of one event as it arrives. The payload of a known type is trusted to match
 * its type (the contract test proves the server's output does); an unknown type passes through.
 */
export function parseAgentEvent(value: unknown): AgentEvent {
  if (!isRecord(value)) throw new TypeError("An agent event is an object.");
  if (value.v !== AGENT_PROTOCOL_VERSION)
    throw new UnsupportedProtocolVersion(value.v);
  const { session_id, seq, type, ts, turn, payload } = value;
  if (
    typeof session_id !== "string" ||
    !Number.isInteger(seq) ||
    (seq as number) < 0 ||
    typeof type !== "string" ||
    typeof ts !== "string" ||
    !Number.isInteger(turn) ||
    !isRecord(payload)
  ) {
    throw new TypeError("The agent event envelope is malformed.");
  }
  if ((seq === 0) !== transient.has(type)) {
    throw new TypeError("Only transient events carry sequence 0.");
  }
  return value as unknown as AgentEvent;
}
