"""Machine workflows: inspection (connectivity, host key, OS, hardware, prerequisites)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.modules.infrastructure import facts as facts_mod
from app.modules.infrastructure.access import connect, run_script
from app.modules.infrastructure.host_access import CommandOptions
from app.modules.infrastructure.models import Machine, MachineConnectivity, MachineLifecycleState
from app.orchestration.context import StepContext
from app.orchestration.models import LogLevel, Operation, OperationStatus
from app.orchestration.workflow import (
    OperationTarget,
    StepDefinition,
    StepFailedError,
    WorkflowDefinition,
    workflow_registry,
)

MACHINE_INSPECT = "machine.inspect"


class InspectParams(BaseModel):
    machine_id: uuid.UUID
    machine_name: str
    accept_new_host_key: bool = False


def step_connect(ctx: StepContext) -> dict[str, Any]:
    p: InspectParams = ctx.params  # type: ignore[assignment]
    with connect(ctx, p.machine_id, accept_new_host_key=p.accept_new_host_key) as executor:
        ctx.info(f"Connected to {executor.endpoint.host}:{executor.endpoint.port} as {executor.endpoint.username}")
        sudo = executor.run("true", CommandOptions(sudo=True, timeout_seconds=30))
        sudo_ok = sudo.ok
        if sudo_ok:
            ctx.info("Administrative access verified")
        else:
            ctx.warning(f"Administrative access check failed: {sudo.stderr.strip() or 'sudo refused'}")
        fingerprint = executor.observed_host_key.fingerprint if executor.observed_host_key else None
    return {"sudo_ok": sudo_ok, "host_key_fingerprint": fingerprint}


def step_inspect(ctx: StepContext) -> dict[str, Any]:
    p: InspectParams = ctx.params  # type: ignore[assignment]
    with connect(ctx, p.machine_id) as executor:
        result = run_script(executor, facts_mod.detect_script(), sudo=False, timeout_seconds=120)
    if not result.ok:
        raise StepFailedError(f"Inspection script failed: {result.stderr.strip()[-500:]}", code="inspection_failed")
    facts = facts_mod.parse_facts(result.stdout)
    checks = facts_mod.evaluate(facts, sudo_ok=bool(ctx.outputs.get("connect", {}).get("sudo_ok")))
    os_info, hardware, addresses = facts_mod.summarize(facts)
    failures = [c for c in checks if c.status is facts_mod.CheckStatus.FAIL]

    for check in checks:
        ctx.log(
            f"[{check.status.value.upper():4}] {check.label}: {check.message}",
            LogLevel.INFO if check.status is facts_mod.CheckStatus.PASS else LogLevel.WARNING,
        )

    now = datetime.now(UTC)
    with ctx.db() as session:
        machine = session.get(Machine, p.machine_id)
        if machine is None:
            raise StepFailedError("Machine was deleted during inspection", code="machine_not_found")
        machine.os_info = os_info
        machine.hardware = hardware
        machine.addresses = addresses
        machine.checks = facts_mod.checks_to_json(checks)
        machine.inspected_at = now
        machine.last_seen_at = now
        machine.connectivity_status = MachineConnectivity.REACHABLE
        machine.connectivity_checked_at = now
        machine.connectivity_error = None
        if failures:
            machine.lifecycle_state = MachineLifecycleState.ERROR
            machine.last_error = "; ".join(f"{c.label}: {c.message}" for c in failures)
        else:
            machine.lifecycle_state = MachineLifecycleState.READY
            machine.last_error = None

    summary = {
        "os": os_info.get("pretty_name"),
        "cpu_cores": hardware.get("cpu_cores"),
        "memory_mb": hardware.get("memory_mb"),
        "primary_ip": facts.get("primary_ip"),
        "failed_checks": [c.key for c in failures],
    }
    ctx.set_result(summary)
    if failures:
        ctx.warning(f"{len(failures)} prerequisite check(s) failed; the machine cannot be used for clusters yet")
    return summary


def on_status(session: Session, op: Operation) -> None:
    if op.status is not OperationStatus.FAILED:
        return
    machine = session.get(Machine, uuid.UUID(op.params["machine_id"]))
    if machine is None:
        return
    code = (op.error or {}).get("code")
    machine.lifecycle_state = (
        MachineLifecycleState.UNREACHABLE if code == "retries_exhausted" else MachineLifecycleState.ERROR
    )
    machine.last_error = (op.error or {}).get("message")


inspect_workflow = workflow_registry.register(
    WorkflowDefinition(
        type=MACHINE_INSPECT,
        title="Inspect machine",
        description="Connect over SSH, verify the host key and inventory OS, hardware and prerequisites.",
        params_model=InspectParams,
        plan=lambda _p: [
            StepDefinition(
                "connect",
                "Connect over SSH and verify host key",
                step_connect,
                max_attempts=3,
                retry_backoff_seconds=5,
            ),
            StepDefinition(
                "inspect",
                "Inspect OS, hardware and prerequisites",
                step_inspect,
                max_attempts=2,
                retry_backoff_seconds=5,
            ),
        ],
        target=lambda p: OperationTarget(type="machine", id=str(p.machine_id), name=p.machine_name),
        title_for=lambda p: f"Inspect machine {p.machine_name}",
        on_status=on_status,
    )
)
