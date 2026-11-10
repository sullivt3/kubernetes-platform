"""Creation-time storage runs inside the cluster's existing exclusive operation."""

from typing import Any
from urllib.parse import quote

from app.modules.cluster_lifecycle.distributions.base import ClusterPlanInput
from app.modules.kubernetes import client as kc
from app.modules.kubernetes.phase6 import workflow_api
from app.modules.kubernetes.storage import InstallParams, apply_driver, wait_driver
from app.modules.kubernetes.workflows import _call
from app.orchestration.context import StepContext
from app.orchestration.workflow import StepDefinition, StepFailedError


def plan_storage(plan: ClusterPlanInput) -> list[StepDefinition]:
    if not (plan.spec.storage.csi_drivers or plan.spec.storage.default_storage_class):
        return []
    return [StepDefinition("storage", "Install and configure cluster storage", configure)]


def configure(ctx: StepContext) -> dict[str, Any]:
    plan: ClusterPlanInput = ctx.params  # type: ignore[assignment]
    spec = plan.spec.storage
    api = workflow_api(ctx, plan.cluster_id)

    def run() -> dict[str, Any]:
        if "nfs.csi.k8s.io" in spec.csi_drivers:
            params = InstallParams(
                cluster_id=plan.cluster_id,
                cluster_name=plan.cluster_name,
                storage_class=spec.default_storage_class if spec.nfs_server else None,
                server=spec.nfs_server,
                share=spec.nfs_share,
                make_default=bool(spec.nfs_server),
            )
            apply_driver(ctx, api, params)
            wait_driver(ctx, api)
        if spec.default_storage_class and not spec.nfs_server:
            path = "/apis/storage.k8s.io/v1/storageclasses"
            classes = kc.get_json(api, path).get("items", [])
            selected = next((sc for sc in classes if sc["metadata"]["name"] == spec.default_storage_class), None)
            if selected is None:
                raise StepFailedError(f"StorageClass '{spec.default_storage_class}' does not exist")
            for sc in classes:
                meta = sc["metadata"]
                if meta["name"] != spec.default_storage_class and any(
                    meta.get("annotations", {}).get(key) == "true"
                    for key in (
                        "storageclass.kubernetes.io/is-default-class",
                        "storageclass.beta.kubernetes.io/is-default-class",
                    )
                ):
                    raise StepFailedError(
                        f"StorageClass '{meta['name']}' is already the default; change it explicitly in Storage classes"
                    )
            selected["metadata"].setdefault("annotations", {})["storageclass.kubernetes.io/is-default-class"] = "true"
            api.request("PUT", f"{path}/{quote(spec.default_storage_class, safe='')}", body=selected)
        return {"drivers": spec.csi_drivers, "default_storage_class": spec.default_storage_class}

    try:
        return _call(run)
    finally:
        api.close()
