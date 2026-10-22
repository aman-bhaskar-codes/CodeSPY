"""In-memory async event streaming buffer for investigation replay and SSE broadcasting."""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from uuid import UUID

from app.contracts.investigation import (
    InvestigationEventType,
    InvestigationEventV1,
)
from app.services.investigation_checkpointer import FencedCheckpointer

logger = logging.getLogger(__name__)


class InvestigationStreamBuffer:
    """Manages multi-subscriber live event queues and SSE formatting for investigations."""

    def __init__(self) -> None:
        # Maps investigation_id -> list of subscriber queues
        self._listeners: dict[UUID, list[asyncio.Queue[InvestigationEventV1]]] = {}
        self._lock = asyncio.Lock()

    async def publish_event(
        self,
        org_id: UUID,
        investigation_id: UUID,
        event: InvestigationEventV1,
    ) -> None:
        """Pushes an event to all active live subscribers."""
        async with self._lock:
            queues = self._listeners.get(investigation_id, [])
            for q in list(queues):
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    logger.warning("Subscriber queue full for investigation %s", investigation_id)

    async def subscribe(
        self,
        org_id: UUID,
        investigation_id: UUID,
        checkpointer: FencedCheckpointer | None = None,
        after_step: int | None = None,
        timeout_seconds: float = 300.0,
    ) -> AsyncIterator[InvestigationEventV1]:
        """Streams stored events first (if after_step requested), then yields live events."""
        seen_event_ids: set[UUID] = set()

        # Step 1: Catch up on past events from persistent storage
        if checkpointer is not None:
            past_events = await checkpointer.list_events(
                org_id, investigation_id, after_step=after_step
            )
            for ev in past_events:
                seen_event_ids.add(ev.id)
                yield ev
                if ev.event_type in (
                    InvestigationEventType.VERDICT_REACHED.value,
                    InvestigationEventType.FAILED.value,
                    InvestigationEventType.CANCELLED.value,
                ):
                    return

        # Step 2: Register live queue
        queue: asyncio.Queue[InvestigationEventV1] = asyncio.Queue(maxsize=100)
        async with self._lock:
            if investigation_id not in self._listeners:
                self._listeners[investigation_id] = []
            self._listeners[investigation_id].append(queue)

        try:
            while True:
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=timeout_seconds)
                except TimeoutError:
                    logger.info("Stream subscription timed out for %s", investigation_id)
                    break

                if ev.id in seen_event_ids:
                    continue
                seen_event_ids.add(ev.id)
                yield ev

                # Terminal event marks completion of the stream
                if ev.event_type in (
                    InvestigationEventType.VERDICT_REACHED.value,
                    InvestigationEventType.FAILED.value,
                    InvestigationEventType.CANCELLED.value,
                ):
                    break
        finally:
            async with self._lock:
                if investigation_id in self._listeners:
                    if queue in self._listeners[investigation_id]:
                        self._listeners[investigation_id].remove(queue)
                    if not self._listeners[investigation_id]:
                        del self._listeners[investigation_id]

    @staticmethod
    def format_sse(event: InvestigationEventV1) -> str:
        """Serializes event to valid standard Server-Sent Events (SSE) format."""
        payload_str = json.dumps(
            {
                "id": str(event.id),
                "investigation_id": str(event.investigation_id),
                "step_index": event.step_index,
                "event_type": event.event_type,
                "node_name": event.node_name,
                "payload": event.payload,
                "created_at": event.created_at.isoformat(),
            }
        )
        return f"id: {event.id}\nevent: {event.event_type}\ndata: {payload_str}\n\n"
