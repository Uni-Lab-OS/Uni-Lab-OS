"""Durable Inventory outbox replay through the public service boundary."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from unilabos.app.scheduler.inventory.domain import InventoryEvent
from unilabos.app.scheduler.inventory.service import InventoryService

logger = logging.getLogger(__name__)

SyncSender = Callable[[list[dict[str, Any]]], int]


def _event_to_envelope(event: InventoryEvent) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "edge_id": event.edge_id,
        "lab_id": event.lab_id,
        "sequence": event.sequence,
        "aggregate_type": event.aggregate_type,
        "aggregate_id": event.aggregate_id,
        "aggregate_version": event.aggregate_version,
        "event_type": event.event_type,
        "occurred_at": event.occurred_at,
        "causation_id": event.causation_id,
        "payload": dict(event.payload),
    }


class OutboxWorker:
    """Replay Inventory events and advance the one Scheduler acknowledgement."""

    def __init__(
        self,
        inventory: InventoryService,
        sender: SyncSender,
        batch_size: int = 100,
        poll_interval: float = 1.0,
        base_backoff: float = 1.0,
        max_backoff: float = 60.0,
    ) -> None:
        self._inventory = inventory
        self._sender = sender
        self._batch_size = batch_size
        self._poll_interval = poll_interval
        self._base_backoff = base_backoff
        self._max_backoff = max_backoff
        self._failures = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def flush_once(self) -> int:
        """Send one ordered batch and durably advance its acknowledged watermark."""

        acknowledged_from = self._inventory.get_acknowledged_sequence(consumer="cloud")
        events = self._inventory.read_outbox(
            after_sequence=acknowledged_from,
            limit=self._batch_size,
        )
        if not events:
            return 0
        envelopes = [_event_to_envelope(event) for event in events]
        try:
            acknowledged_sequence = int(self._sender(envelopes))
        except Exception:
            self._failures += 1
            raise
        self._failures = 0
        if acknowledged_sequence > acknowledged_from:
            self._inventory.acknowledge(
                acknowledged_sequence,
                consumer="cloud",
            )
        return sum(1 for event in events if event.sequence <= acknowledged_sequence)

    def flush_all(self, max_batches: int = 1000) -> int:
        """Replay until no newly acknowledged event remains."""

        total = 0
        for _ in range(max_batches):
            sent = self.flush_once()
            if sent == 0:
                break
            total += sent
        return total

    def backlog(self) -> int:
        return self._inventory.outbox_status(consumer="cloud")["backlog"]

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="inventory-outbox",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                sent = self.flush_once()
                wait = self._poll_interval if sent == 0 else 0.0
            except Exception as exc:  # noqa: BLE001 - retry keeps durable events
                wait = min(
                    self._base_backoff * (2 ** (self._failures - 1)),
                    self._max_backoff,
                )
                logger.warning(
                    "outbox flush failed (attempt %s, retry in %.1fs): %s",
                    self._failures,
                    wait,
                    exc,
                )
            self._stop.wait(wait)


def build_snapshot(inventory: InventoryService) -> dict[str, Any]:
    """Build one canonical snapshot without exposing persistence internals."""

    return inventory.inventory_snapshot()


class CloudProjectionReference:
    """Reference inbox semantics for replay/deduplication contract tests."""

    def __init__(self) -> None:
        self.seen: set[tuple[str, str]] = set()
        self.versions: dict[str, int] = {}
        self.state: dict[str, dict[str, Any]] = {}
        self.acked_sequence = 0

    def ingest(self, events: list[dict[str, Any]]) -> int:
        for event in sorted(events, key=lambda item: item["sequence"]):
            dedupe_key = (event["edge_id"], event["event_id"])
            if dedupe_key not in self.seen:
                self.seen.add(dedupe_key)
                aggregate_key = f"{event['aggregate_type']}:{event['aggregate_id']}"
                if event["aggregate_version"] > self.versions.get(aggregate_key, 0):
                    self.versions[aggregate_key] = event["aggregate_version"]
                    self.state[aggregate_key] = {
                        "event_type": event["event_type"],
                        "payload": event["payload"],
                        "version": event["aggregate_version"],
                    }
            if event["sequence"] == self.acked_sequence + 1:
                self.acked_sequence = event["sequence"]
        return self.acked_sequence

    def load_snapshot(self, snapshot: dict[str, Any]) -> None:
        self.state.clear()
        self.versions.clear()
        for material in snapshot.get("materials", []):
            key = f"material:{material['uuid']}"
            self.versions[key] = int(material["version"])
            self.state[key] = {
                "event_type": "snapshot",
                "payload": dict(material),
                "version": int(material["version"]),
            }
        for site in snapshot.get("sites", []):
            key = f"site:{site['uuid']}"
            self.versions[key] = int(site["version"])
            self.state[key] = {
                "event_type": "snapshot",
                "payload": dict(site),
                "version": int(site["version"]),
            }
        for lot in snapshot.get("inventory_lots", []):
            key = f"lot:{lot['lot_id']}"
            self.versions[key] = int(lot["version"])
            self.state[key] = {
                "event_type": "snapshot",
                "payload": dict(lot),
                "version": int(lot["version"]),
            }
        self.acked_sequence = int(snapshot.get("snapshot_sequence", 0))
