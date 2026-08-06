"""Small deterministic SZLab-shaped device used through the public CLI seam."""

from typing import TypedDict

from unilabos.registry.annotations import JSONValue
from unilabos.registry.decorators import action, device


class PrepareResult(TypedDict):
    payload: dict[str, JSONValue]


class FinishResult(TypedDict):
    summary: dict[str, JSONValue]


@device(
    id="r2e_szlab_mixer",
    category=["test"],
    displayname="R2E SZLab mixer",
)
class R2ESZLabMixer:
    @action(description="Prepare one deterministic SZLab batch")
    def prepare(self, batch: int = 1) -> PrepareResult:
        return {"payload": {"batch": batch}}

    @action(description="Finish the prepared SZLab batch")
    def finish(self, payload: dict[str, JSONValue]) -> FinishResult:
        return {"summary": dict(payload)}
