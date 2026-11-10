"""Managed Kubernetes pool scaling and upgrades through native cloud APIs."""

import time
from typing import Any
from urllib.parse import quote

from app.modules.providers import http


class ManagedClusterActions:
    def _managed_resource(self: Any, external_id: str) -> tuple[str, dict[str, Any]]:
        kind = self.provider_type.value
        if kind == "aws":
            region, sep, name = external_id.partition("|")
            if not sep or region != self.cfg.region or not name:
                raise http.ProviderApiError("EKS identifier does not match this connection")
            current = self._eks_cluster(name)
            if not current or (current.get("tags") or {}).get("kubernetes-platform:managed") != "true":
                raise http.ProviderApiError("EKS cluster is missing or is not platform owned")
            return name, current
        account, sep, remaining = external_id.partition("/")
        location, sep2, name = remaining.partition("/")
        if not sep or not sep2 or not location or not name or "/" in name:
            raise http.ProviderApiError("Invalid managed cluster identifier")
        if kind == "azure":
            from app.modules.providers.implementations.azure import API_MANAGED_K8S, ARM

            if account != self.cfg.subscription_id:
                raise http.ProviderApiError("AKS subscription does not match this connection")
            base = (
                f"{ARM}/subscriptions/{account}/resourceGroups/{quote(location, safe='')}/"
                f"providers/Microsoft.ContainerService/managedClusters/{quote(name, safe='')}"
            )
            current = self._get(f"{base}?api-version={API_MANAGED_K8S}", "read AKS cluster")
            if (current.get("tags") or {}).get("kubernetes-platform:managed") != "true":
                raise http.ProviderApiError("AKS cluster is not platform owned")
            return base, current
        if account != self.project:
            raise http.ProviderApiError("GKE project does not match this connection")
        current = self._gke_get(location, name)
        if not current or (current.get("resourceLabels") or {}).get("kubernetes-platform-managed") != "true":
            raise http.ProviderApiError("GKE cluster is missing or is not platform owned")
        return (
            f"https://container.googleapis.com/v1/projects/{quote(account, safe='')}/"
            f"locations/{quote(location, safe='')}/"
            f"clusters/{quote(name, safe='')}",
            current,
        )

    def _eks_update(self: Any, name: str, response: dict[str, Any], pool: str | None = None) -> None:
        update_id = (response.get("update") or {}).get("id")
        if not update_id:
            raise http.ProviderApiError("EKS did not return an update ID")
        params = {"name": name, "updateId": update_id}
        if pool:
            params["nodegroupName"] = pool
        deadline = time.monotonic() + 3600
        while time.monotonic() < deadline:
            update = self._eks("DescribeUpdate", params, what="DescribeUpdate").get("update") or {}
            if update.get("status") == "Successful":
                return
            if update.get("status") in {"Failed", "Cancelled"}:
                raise http.ProviderApiError(f"EKS update failed: {update.get('errors', [])}")
            time.sleep(10)
        raise http.ProviderApiError("Timed out waiting for EKS update")

    def resize_managed_pool(self: Any, external_id: str, pool: str, count: int) -> None:
        if count < 0 or count > 1000 or not pool or "/" in pool:
            raise http.ProviderApiError("Pool name and node count are invalid")
        base, current = self._managed_resource(external_id)
        kind = self.provider_type.value
        if kind == "aws":
            group = (
                self._eks(
                    "DescribeNodegroup", {"clusterName": base, "nodegroupName": pool}, what="DescribeNodegroup"
                ).get("nodegroup")
                or {}
            )
            if (group.get("tags") or {}).get("kubernetes-platform:managed") != "true":
                raise http.ProviderApiError("EKS node group is not platform owned")
            config = group.get("scalingConfig") or {}
            if config.get("desiredSize") == count:
                return
            response = self._eks(
                "UpdateNodegroupConfig",
                {
                    "clusterName": base,
                    "nodegroupName": pool,
                    "scalingConfig": {
                        "desiredSize": count,
                        "minSize": min(int(config.get("minSize", 0)), count),
                        "maxSize": max(int(config.get("maxSize", 1)), count, 1),
                    },
                },
                what="UpdateNodegroupConfig",
            )
            self._eks_update(base, response, pool)
        elif kind == "azure":
            from app.modules.providers.implementations.azure import API_MANAGED_K8S

            pool = pool.replace("-", "")[:12]
            url = f"{base}/agentPools/{quote(pool, safe='')}?api-version={API_MANAGED_K8S}"
            existing = self._get(url, "read AKS agent pool")
            props = existing.get("properties", {})
            if props.get("mode") == "System" and count < 1:
                raise http.ProviderApiError("An AKS system pool must keep at least one node")
            if props.get("enableAutoScaling"):
                raise http.ProviderApiError("Disable autoscaling before manually resizing this AKS pool")
            if props.get("count") == count:
                return
            writable = {
                key: value
                for key, value in props.items()
                if key
                in {
                    "vmSize",
                    "osDiskSizeGB",
                    "osDiskType",
                    "vnetSubnetID",
                    "maxPods",
                    "type",
                    "mode",
                    "osType",
                    "orchestratorVersion",
                    "nodeLabels",
                    "nodeTaints",
                    "availabilityZones",
                    "enableAutoScaling",
                    "enableNodePublicIP",
                    "scaleSetPriority",
                    "scaleSetEvictionPolicy",
                    "spotMaxPrice",
                    "upgradeSettings",
                }
            }
            writable["count"] = count
            self._managed_request("PUT", url, "resize AKS agent pool", {"properties": writable})
            self._wait_aks_pool(url, count=count)
        else:
            if not any(p.get("name") == pool for p in current.get("nodePools", [])):
                raise http.ProviderApiError("GKE node pool does not exist")
            response = self._gke_request(
                "POST", f"{base}/nodePools/{quote(pool, safe='')}:setSize", "resize GKE pool", {"nodeCount": count}
            )
            location = external_id.split("/")[1]
            self._gke_wait_operation(location, str(response.get("name") or ""))

    def _wait_aks_pool(self: Any, url: str, *, count: int | None = None, version: str | None = None) -> None:
        deadline = time.monotonic() + 3600
        while time.monotonic() < deadline:
            props = self._get(url, "wait for AKS pool").get("properties", {})
            status = props.get("provisioningState")
            if status in {"Failed", "Canceled"}:
                raise http.ProviderApiError(f"AKS node pool update failed: {status}")
            if (
                status == "Succeeded"
                and (count is None or props.get("count") == count)
                and (
                    version is None
                    or props.get("currentOrchestratorVersion", props.get("orchestratorVersion")) == version
                )
            ):
                return
            time.sleep(10)
        raise http.ProviderApiError("Timed out waiting for AKS pool update")

    def upgrade_managed_cluster(self: Any, external_id: str, version: str) -> None:
        base, current = self._managed_resource(external_id)
        version = version.removeprefix("v")
        kind = self.provider_type.value
        if kind == "aws":
            # EKS control-plane updates accept a minor version, not a patch version.
            minor = ".".join(version.split(".")[:2])
            if current.get("version") != minor:
                self._eks_update(
                    base,
                    self._eks("UpdateClusterVersion", {"name": base, "version": minor}, what="UpdateClusterVersion"),
                )
            groups = self._eks_nodegroups(base)
            for group in groups:
                described = (
                    self._eks(
                        "DescribeNodegroup", {"clusterName": base, "nodegroupName": group}, what="DescribeNodegroup"
                    ).get("nodegroup")
                    or {}
                )
                if (described.get("tags") or {}).get("kubernetes-platform:managed") != "true":
                    raise http.ProviderApiError(f"Refusing to upgrade unmanaged node group {group}")
                if described.get("version") != minor:
                    self._eks_update(
                        base,
                        self._eks(
                            "UpdateNodegroupVersion",
                            {"clusterName": base, "nodegroupName": group, "version": minor},
                            what="UpdateNodegroupVersion",
                        ),
                        group,
                    )
        elif kind == "azure":
            from app.modules.providers.implementations.azure import API_MANAGED_K8S

            # Preserve networking, identity, pool sizing and all other writable cluster settings.
            props = current.get("properties", {})
            writable = {
                key: value
                for key, value in props.items()
                if key
                in {
                    "dnsPrefix",
                    "fqdnSubdomain",
                    "servicePrincipalProfile",
                    "networkProfile",
                    "addonProfiles",
                    "apiServerAccessProfile",
                    "enableRBAC",
                    "aadProfile",
                    "disableLocalAccounts",
                    "securityProfile",
                    "storageProfile",
                    "identityProfile",
                    "autoScalerProfile",
                    "nodeResourceGroup",
                }
            }
            writable["kubernetesVersion"] = version
            body = {
                "location": current["location"],
                "tags": current.get("tags", {}),
                "identity": current.get("identity"),
                "properties": writable,
            }
            self._managed_request("PUT", f"{base}?api-version={API_MANAGED_K8S}", "upgrade AKS control plane", body)
            self._wait_managed_cluster(f"{base}?api-version={API_MANAGED_K8S}")
            for pool in props.get("agentPoolProfiles", []):
                url = f"{base}/agentPools/{quote(pool['name'], safe='')}?api-version={API_MANAGED_K8S}"
                original = self._get(url, "read AKS pool")["properties"]
                allowed = {
                    key: value
                    for key, value in original.items()
                    if key
                    in {
                        "count",
                        "vmSize",
                        "osDiskSizeGB",
                        "osDiskType",
                        "vnetSubnetID",
                        "maxPods",
                        "type",
                        "mode",
                        "osType",
                        "nodeLabels",
                        "nodeTaints",
                        "availabilityZones",
                        "enableAutoScaling",
                        "minCount",
                        "maxCount",
                        "upgradeSettings",
                    }
                }
                allowed["orchestratorVersion"] = version
                self._managed_request("PUT", url, "upgrade AKS node pool", {"properties": allowed})
                self._wait_aks_pool(url, version=version)
        else:
            location = external_id.split("/")[1]
            if current.get("currentMasterVersion") != version:
                response = self._gke_request(
                    "PUT", base, "upgrade GKE control plane", {"update": {"desiredMasterVersion": version}}
                )
                self._gke_wait_operation(location, str(response.get("name") or ""))
            for pool in current.get("nodePools", []):
                if pool.get("version") == version:
                    continue
                response = self._gke_request(
                    "PUT",
                    f"{base}/nodePools/{quote(pool['name'], safe='')}",
                    "upgrade GKE node pool",
                    {"nodeVersion": version},
                )
                self._gke_wait_operation(location, str(response.get("name") or ""))
