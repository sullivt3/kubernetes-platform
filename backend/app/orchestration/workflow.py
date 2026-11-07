"""Workflow definitions.

A *workflow* turns validated parameters into an ordered *plan* of steps. The plan
is computed when the operation is created and persisted as ``OperationStep`` rows,
so users can review every step before and during execution (the cluster wizard's
review page shows exactly this plan).

Contract for step functions
---------------------------
* Signature: ``fn(ctx: StepContext) -> dict | None``. The returned dict is stored as
  the step's checkpoint output and is visible to later steps via ``ctx.outputs``.
* Steps MUST be idempotent. After a worker crash the interrupted step is re-run;
  after a manual retry the failed step is re-run. Completed steps are never re-run.
* ``plan(params)`` MUST be deterministic for the same params: on resume the engine
  recomputes the plan and matches step functions to persisted rows by ``key``.
* Raise :class:`RetryableStepError` for transient failures (network blips, API
  throttling); the engine retries up to ``max_attempts`` with exponential backoff.
* Raise :class:`StepFailedError` for expected, explained failures (bad input,
  unsupported OS...). Any other exception is reported as ``unexpected_error``.
* Call ``ctx.check_cancelled()`` at safe points inside long steps.
"""

from __future__ import annotations

import builtins
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from app.core.errors import ValidationFailedError

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.orchestration.context import StepContext
    from app.orchestration.models import Operation

StepFn = Callable[["StepContext"], dict[str, Any] | None]

_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,127}$")


class RetryableStepError(Exception):
    """Transient failure; the step may succeed if attempted again."""


class StepFailedError(Exception):
    """Expected failure with a user-facing explanation."""

    def __init__(self, message: str, *, code: str = "step_failed", details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


class OperationCancelledError(Exception):
    """Raised inside a step when cancellation has been requested."""


@dataclass(frozen=True, slots=True)
class StepDefinition:
    key: str
    name: str
    fn: StepFn
    description: str = ""
    max_attempts: int = 1
    retry_backoff_seconds: float = 5.0

    def __post_init__(self) -> None:
        if not _KEY_RE.match(self.key):
            raise ValueError(f"Invalid step key '{self.key}'")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")


@dataclass(frozen=True, slots=True)
class OperationTarget:
    """What an operation acts on. ``exclusive`` targets allow only one active operation at a time."""

    type: str
    id: str
    name: str | None = None
    exclusive: bool = True

    @property
    def concurrency_key(self) -> str | None:
        return f"{self.type}:{self.id}" if self.exclusive else None


@dataclass(frozen=True)
class WorkflowDefinition[P: BaseModel]:
    type: str
    title: str
    description: str
    params_model: builtins.type[P]
    plan: Callable[[P], Sequence[StepDefinition]]
    target: Callable[[P], OperationTarget | None] = field(default=lambda _p: None)
    title_for: Callable[[P], str] | None = None
    # Destructive workflows require an explicit confirmation token from the caller.
    destructive: bool = False
    # Exposed to API clients for direct submission (internal workflows are started by services).
    user_startable: bool = False
    # Called when the operation starts running or reaches a terminal status (see hooks.py).
    on_status: Callable[[Session, Operation], None] | None = None

    def validate_params(self, raw: dict[str, Any]) -> P:
        from pydantic import ValidationError

        try:
            return self.params_model.model_validate(raw)
        except ValidationError as exc:
            raise ValidationFailedError(
                f"Invalid parameters for '{self.type}'",
                details={"errors": exc.errors(include_url=False, include_context=False)},
            ) from exc

    def build_plan(self, params: P) -> list[StepDefinition]:
        steps = list(self.plan(params))
        if not steps:
            raise ValueError(f"Workflow '{self.type}' produced an empty plan")
        keys = [s.key for s in steps]
        if len(keys) != len(set(keys)):
            raise ValueError(f"Workflow '{self.type}' produced duplicate step keys")
        return steps

    def render_title(self, params: P) -> str:
        return self.title_for(params) if self.title_for else self.title


class WorkflowRegistry:
    def __init__(self) -> None:
        self._workflows: dict[str, WorkflowDefinition[Any]] = {}

    def register(self, workflow: WorkflowDefinition[Any]) -> WorkflowDefinition[Any]:
        if workflow.type in self._workflows:
            raise ValueError(f"Workflow '{workflow.type}' is already registered")
        self._workflows[workflow.type] = workflow
        return workflow

    def unregister(self, workflow_type: str) -> None:
        self._workflows.pop(workflow_type, None)

    def get(self, workflow_type: str) -> WorkflowDefinition[Any]:
        try:
            return self._workflows[workflow_type]
        except KeyError:
            raise ValidationFailedError(f"Unknown operation type '{workflow_type}'") from None

    def all(self) -> list[WorkflowDefinition[Any]]:
        return list(self._workflows.values())


workflow_registry = WorkflowRegistry()
