"""Safe, run-scoped debugger admission control for :class:`DagExecutor`.

The debugger is deliberately placed before Layer-A resource admission.  A
paused node therefore owns no new resource lease and has not entered the
device action queue.  Version one uses a global quiescent pause model:

* a pause request stops admitting new nodes;
* already-running physical actions are allowed to reach a terminal state;
* ``step`` admits exactly one logical ready node, then pauses again;
* ``step_over`` and ``step_into`` are explicit aliases of ``step`` until
  nested workflow frames are introduced.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any


DebugEventCallback = Callable[[str, dict[str, Any]], None]


class DebugCommandError(ValueError):
    """A debugger command is malformed or unsupported for the current run."""


class DebugController:
    """One debugger state machine owned by one running TaskDag."""

    _STEP_COMMANDS = frozenset({"step", "step_over", "step_into"})

    def __init__(
        self,
        *,
        run_id: str,
        node_ids: set[str],
        config: dict[str, Any] | None = None,
        on_event: DebugEventCallback | None = None,
    ) -> None:
        settings = dict(config or {})
        breakpoints = {
            str(node_id) for node_id in settings.get("breakpoints") or []
        }
        unknown = sorted(breakpoints - node_ids)
        if unknown:
            raise DebugCommandError(f"UNKNOWN_BREAKPOINT_NODE: {unknown[0]}")
        start_node_id = str(settings.get("start_node_id") or "")
        if start_node_id and start_node_id not in node_ids:
            raise DebugCommandError(f"UNKNOWN_START_NODE: {start_node_id}")
        self.run_id = run_id
        self._node_ids = set(node_ids)
        self._breakpoints = breakpoints
        self._start_node_id = start_node_id or None
        self._on_event = on_event
        self._condition = asyncio.Condition()
        self._pause_requested = bool(settings.get("pause_on_start", False))
        self._paused = False
        self._terminated = False
        self._step_budget = 0
        self._pause_after_node: str | None = None
        self._paused_before_node: str | None = None
        self._run_to_node: str | None = None
        self._stop_reason: str | None = None
        self._bypass_breakpoints_once: set[str] = set()
        self._status = "pause_pending" if self._pause_requested else "running"
        self._version = 1
        self._last_emitted_status = ""

    @property
    def enabled(self) -> bool:
        return True

    def projection(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "status": self._status,
            "breakpoints": sorted(self._breakpoints),
            "startNodeId": self._start_node_id,
            "pausedBeforeNodeId": self._paused_before_node,
            "runToNodeId": self._run_to_node,
            "stopReason": self._stop_reason,
            "stateVersion": self._version,
            "semantics": "global_quiescent_v2",
        }

    async def select_ready(
        self,
        ready_ids: list[str],
        *,
        has_inflight: bool,
    ) -> list[str]:
        """Return nodes allowed to proceed to resource admission.

        This method may wait only when the executor is quiescent.  If physical
        actions are still in flight it returns an empty admission set, allowing
        the executor to drain those actions before entering ``paused``.
        """

        while not self._terminated:
            hit = self._breakpoint_hit(ready_ids)
            if hit is not None:
                self._pause_requested = True
                self._paused_before_node = hit
            if self._run_to_node is not None and self._run_to_node in ready_ids:
                self._pause_requested = True
                self._paused_before_node = self._run_to_node
                self._run_to_node = None

            if self._pause_requested:
                if has_inflight:
                    self._set_status("pause_pending")
                    return []
                if self._paused_before_node is None and ready_ids:
                    # A quiescent pause is always reported at the next logical
                    # admission point, including pause-on-start and post-step.
                    self._paused_before_node = ready_ids[0]
                    self._bump()
                self._paused = True
                self._set_status("paused", force_event=True)
                async with self._condition:
                    await self._condition.wait_for(
                        lambda: (
                            self._terminated
                            or not self._paused
                            or self._step_budget > 0
                        )
                    )
                continue

            if self._step_budget > 0:
                self._set_status("stepping")
                return ready_ids[:1]
            self._set_status("running")
            return ready_ids
        return []

    def on_admitted(self, admitted_ids: list[str]) -> None:
        if not admitted_ids:
            return
        self._bypass_breakpoints_once.difference_update(admitted_ids)
        if self._step_budget <= 0:
            return
        node_id = admitted_ids[0]
        self._step_budget -= 1
        self._pause_after_node = node_id

    def on_terminal(self, node_id: str) -> None:
        if self._pause_after_node != node_id:
            return
        self._pause_after_node = None
        self._pause_requested = True
        self._paused_before_node = None
        self._set_status("pause_pending")

    def on_run_terminal(self, terminal: str = "completed") -> None:
        self._pause_requested = False
        self._paused = False
        self._terminated = True
        self._set_status(terminal, force_event=True)
        self._notify_waiters()

    def cancel_wait(self) -> None:
        """Wake a quiescent executor after an external cancellation."""

        self._terminated = True
        self._paused = False
        self._pause_requested = False
        self._notify_waiters()

    async def command(
        self,
        command: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        command = command.strip().lower()
        args = dict(payload or {})
        if command == "set_breakpoints":
            raw = args.get("node_ids")
            if not isinstance(raw, list):
                raise DebugCommandError("node_ids must be a list")
            breakpoints = {str(node_id) for node_id in raw}
            unknown = sorted(breakpoints - self._node_ids)
            if unknown:
                raise DebugCommandError(f"UNKNOWN_BREAKPOINT_NODE: {unknown[0]}")
            self._breakpoints = breakpoints
            self._bump()
            self._emit(
                "debug.breakpoints_changed",
                {"breakpoints": sorted(self._breakpoints)},
            )
            return self.projection()

        if command == "pause":
            self._pause_requested = True
            self._set_status("pause_pending", force_event=True)
            return self.projection()

        if command == "continue":
            self._bypass_current_breakpoints()
            self._pause_requested = False
            self._paused = False
            self._step_budget = 0
            self._pause_after_node = None
            self._paused_before_node = None
            self._set_status("running", force_event=True)
            self._notify_waiters()
            return self.projection()

        if command in self._STEP_COMMANDS:
            if not self._paused and self._status != "pause_pending":
                raise DebugCommandError("DEBUG_NOT_PAUSED")
            self._bypass_current_breakpoints()
            self._pause_requested = False
            self._paused = False
            self._step_budget = 1
            self._paused_before_node = None
            self._set_status("stepping", force_event=True)
            self._notify_waiters()
            return self.projection()

        if command == "run_to":
            node_id = str(args.get("node_id") or "")
            if node_id not in self._node_ids:
                raise DebugCommandError(f"UNKNOWN_RUN_TO_NODE: {node_id or '-'}")
            self._bypass_current_breakpoints()
            self._run_to_node = node_id
            self._pause_requested = False
            self._paused = False
            self._step_budget = 0
            self._paused_before_node = None
            self._set_status("running", force_event=True)
            self._notify_waiters()
            return self.projection()

        if command in {"terminate", "emergency_stop"}:
            self._stop_reason = command
            self._bump()
            self._emit(
                (
                    "debug.emergency_stop_requested"
                    if command == "emergency_stop"
                    else "debug.terminate_requested"
                ),
                self.projection(),
            )
            self._terminated = True
            self._paused = False
            self._pause_requested = False
            self._set_status("terminated", force_event=True)
            self._notify_waiters()
            return self.projection()

        raise DebugCommandError(f"UNSUPPORTED_DEBUG_COMMAND: {command or '-'}")

    def _breakpoint_hit(self, ready_ids: list[str]) -> str | None:
        for node_id in ready_ids:
            if (
                node_id in self._breakpoints
                and node_id not in self._bypass_breakpoints_once
            ):
                return node_id
        return None

    def _bypass_current_breakpoints(self) -> None:
        if self._paused_before_node is not None:
            self._bypass_breakpoints_once.add(self._paused_before_node)

    def _set_status(self, status: str, *, force_event: bool = False) -> None:
        changed = status != self._status
        if changed:
            self._status = status
            self._bump()
        if not changed and not force_event:
            return
        if not force_event and self._last_emitted_status == status:
            return
        self._last_emitted_status = status
        event_type = {
            "paused": "debug.paused",
            "pause_pending": "debug.pause_pending",
            "running": "debug.resumed",
            "stepping": "debug.stepping",
            "completed": "debug.completed",
            "cancelled": "debug.cancelled",
            "failed": "debug.failed",
            "terminated": "debug.terminated",
        }.get(status, "debug.state_changed")
        self._emit(event_type, self.projection())

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self._on_event is not None:
            self._on_event(event_type, payload)

    def _bump(self) -> None:
        self._version += 1

    def _notify_waiters(self) -> None:
        async def notify() -> None:
            async with self._condition:
                self._condition.notify_all()

        asyncio.ensure_future(notify())
