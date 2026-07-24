"""Generic declarative PLC macro interpreter.

Profiles own macro names and step sequences.  The driver only serializes calls
to an injected PLC transport and resolves explicit input references.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class MacroResult:
    macro: str
    terminal: str
    outputs: dict[str, Any]


class DeclarativePLCMacroDriver:
    """Execute trusted declarative call steps against one injected transport."""

    def __init__(self, *, plc: Any, driver_config: Mapping[str, Any]) -> None:
        macros = driver_config.get("macros")
        if not isinstance(macros, Mapping) or not macros:
            raise ValueError("driver_config.macros must be a non-empty mapping")
        self.plc = plc
        self._macros = dict(macros)
        self._write_guard = asyncio.Lock()

    async def run_macro(
        self,
        macro: str,
        *,
        inputs: Mapping[str, Any],
    ) -> MacroResult:
        raw_steps = self._macros.get(macro)
        if not isinstance(raw_steps, list):
            raise ValueError(f"macro is not declared by the active profile: {macro}")
        async with self._write_guard:
            for raw_step in raw_steps:
                await self._run_step(raw_step, inputs=inputs)
        return MacroResult(
            macro=macro,
            terminal="succeeded",
            outputs={"terminal": "succeeded"},
        )

    async def _run_step(
        self,
        raw_step: Any,
        *,
        inputs: Mapping[str, Any],
    ) -> None:
        if not isinstance(raw_step, Mapping):
            raise ValueError("macro step must be an object")
        call_name = str(raw_step.get("call") or "")
        if (
            not call_name
            or call_name.startswith("_")
            or not call_name.replace("_", "").isalnum()
        ):
            raise ValueError(f"invalid PLC call name: {call_name!r}")
        operation = getattr(self.plc, call_name, None)
        if operation is None or not callable(operation):
            raise ValueError(f"PLC transport does not provide call: {call_name}")
        raw_args = raw_step.get("args") or []
        raw_kwargs = raw_step.get("kwargs") or {}
        if not isinstance(raw_args, list) or not isinstance(raw_kwargs, Mapping):
            raise ValueError("macro step args/kwargs are malformed")
        args = [self._resolve(value, inputs=inputs) for value in raw_args]
        kwargs = {
            str(name): self._resolve(value, inputs=inputs)
            for name, value in raw_kwargs.items()
        }
        result = operation(*args, **kwargs)
        if not hasattr(result, "__await__"):
            raise TypeError(f"PLC call must be async: {call_name}")
        await result

    def _resolve(self, value: Any, *, inputs: Mapping[str, Any]) -> Any:
        if isinstance(value, Mapping):
            if set(value) == {"input"}:
                name = str(value["input"])
                if name not in inputs:
                    raise ValueError(f"macro input is missing: {name}")
                return inputs[name]
            return {
                str(key): self._resolve(item, inputs=inputs)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self._resolve(item, inputs=inputs) for item in value]
        return value
