"""Administrator-approved automation and generation-bound execution guards."""

from pydantic import BaseModel, Field, model_validator

from app.core.errors import InvalidStateError


class Policy(BaseModel):
    enabled: bool = False
    prune: bool = False
    allow_empty: bool = False
    interval_seconds: int = Field(default=300, ge=60, le=3600)

    @model_validator(mode="after")
    def empty_requires_pruning(self):
        if self.allow_empty and not self.prune:
            raise ValueError("Allow empty requires pruning to be enabled")
        return self


class PolicyInput(Policy):
    confirm: str
    generation: int = Field(ge=1)


class ReviewInput(BaseModel):
    prune: bool = False
    allow_empty: bool = False

    @model_validator(mode="after")
    def empty_requires_pruning(self):
        if self.allow_empty and not self.prune:
            raise ValueError("Allow empty requires an explicit pruning review")
        return self


def validate(row, generation, *, automatic=False):
    if row is None or row.generation != generation:
        raise InvalidStateError("Application configuration or policy changed; execution stopped")
    if automatic and (
        not Policy.model_validate(row.automation).enabled
        or row.automation.get("approved_generation") != row.generation
        or not row.automation.get("approved_by")
    ):
        raise InvalidStateError("Automatic reconciliation is paused or approval was revoked")
