---
status: accepted
---

# Keep inferred debug material state separate from inventory facts

Debug launch preflight may derive an explainable Material State Suggestion when a skipped node has an explicit skip/passthrough contract, but it never writes inventory or presents the suggestion as fact. Only a deterministic same-Material passthrough proven by the Inventory Authority may be prefilled, and even that value remains visibly subject to user confirmation; movement, transformation, creation, destruction, or an unproven identity always requires an explicit selection or confirmation frozen as a task-scoped Debug Launch Override.
