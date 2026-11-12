import { describe, expect, it } from "vitest";

import { buildSpec, INITIAL, stepErrors } from "@/features/clusters/create-wizard";

describe("cluster wizard spec", () => {
  it("only grants local kubectl access when explicitly enabled for kubeadm", () => {
    expect(buildSpec({ ...INITIAL, distribution: "kubeadm", kubectlUsername: "alice" }).options).not.toHaveProperty("kubectl_username");
    expect(buildSpec({ ...INITIAL, distribution: "kubeadm", configureKubectl: true, kubectlUsername: " alice " }).options).toHaveProperty("kubectl_username", "alice");
    expect(buildSpec({ ...INITIAL, distribution: "k3s", configureKubectl: true, kubectlUsername: "alice" }).options).not.toHaveProperty("kubectl_username");
  });
  it("requires a valid Linux username before review and submission", () => {
    for (const kubectlUsername of ["", "../root", "alice;id", "a".repeat(33)]) {
      const state = { ...INITIAL, distribution: "kubeadm", configureKubectl: true, kubectlUsername };
      expect(stepErrors(5, state)).toContain("Kubectl access needs a Linux username: lowercase letters, digits, '_' or '-' (max 32)");
      expect(stepErrors(6, state)).toContain("Kubectl access needs a Linux username: lowercase letters, digits, '_' or '-' (max 32)");
    }
    expect(stepErrors(5, { ...INITIAL, distribution: "kubeadm", configureKubectl: true, kubectlUsername: "root" })).toEqual([]);
  });
  it("maps local clusters without SSH machine provisioning", () => {
    const spec = buildSpec({ ...INITIAL, distribution: "kind", provider: "local", version: "1.34.2", cni: "provider-default", connectionId: "docker", cpNew: 3 });
    expect(spec.provider_connection_id).toBe("docker");
    expect(spec.machine_credential_id).toBeNull();
    expect(spec.control_plane).toMatchObject({ count: 3, existing_machine_ids: [], load_balancer: "external", endpoint: "127.0.0.1" });
  });

  it("carries cloud image, sizing, network and storage choices", () => {
    const spec = buildSpec({ ...INITIAL, distribution: "k3s", provider: "aws", version: "1.34.2", cpNew: 1, connectionId: "cloud", credentialId: "key", cpSize: { cpu: 4, memory: 8192, disk: 60, image: " ami-123 ", options: { instance_type: "t3.large", subnet_id: "subnet-1", security_group_ids: "sg-1, sg-2" } }, nfsDriver: true, storageClass: "nfs", nfsServer: "nfs.internal", nfsShare: "/exports" });
    expect(spec.control_plane.machine_template).toMatchObject({ image: "ami-123", disk_gb: 60, provider_options: { instance_type: "t3.large", security_group_ids: ["sg-1", "sg-2"] } });
    expect(spec.storage).toEqual({ default_storage_class: "nfs", csi_drivers: ["nfs.csi.k8s.io"], nfs_server: "nfs.internal", nfs_share: "/exports" });
  });

  it("uses managed pool disk and machine type choices with labels and taints", () => {
    const spec = buildSpec({ ...INITIAL, distribution: "eks", provider: "aws", connectionId: "cloud", pools: [{ name: "apps", machines: [], newCount: 2, size: { cpu: 2, memory: 4096, disk: 80, options: { machine_type: "m5.large" } }, labels: [{ key: "tier", value: "apps" }], taints: [{ key: "dedicated", effect: "PreferNoSchedule" }] }] });
    expect(spec.control_plane.count).toBe(0);
    expect(spec.worker_pools[0]).toMatchObject({ count: 2, labels: { tier: "apps" }, taints: [{ key: "dedicated", effect: "PreferNoSchedule" }], machine_template: { disk_gb: 80, provider_options: { machine_type: "m5.large" } } });
  });
  it("maps existing machines to a single control plane without an endpoint", () => {
    const spec = buildSpec({ ...INITIAL, name: "a", provider: "ssh", distribution: "kubeadm", version: "1.37", cni: "calico", cpMachines: ["m1"] });
    expect(spec.control_plane).toMatchObject({ count: 1, existing_machine_ids: ["m1"], load_balancer: "none", endpoint: null, machine_template: null });
    expect(spec.provider_connection_id).toBeNull();
    expect(spec.worker_pools).toEqual([]);
  });

  it("maps provisioning and kube-vip", () => {
    const spec = buildSpec({
      ...INITIAL,
      name: "ha",
      provider: "local",
      distribution: "kubeadm",
      version: "1.37",
      cni: "flannel",
      connectionId: "conn",
      credentialId: "key",
      cpNew: 3,
      lbMode: "kube-vip",
      cpEndpoint: " 172.28.0.200 ",
      pools: [{ name: "workers", machines: ["w1"], newCount: 2, size: { cpu: 4, memory: 8192, disk: 80 }, labels: [], taints: [] }],
    });
    expect(spec.provider_connection_id).toBe("conn");
    expect(spec.machine_credential_id).toBe("key");
    expect(spec.control_plane).toMatchObject({
      count: 3,
      load_balancer: "kube-vip",
      endpoint: "172.28.0.200",
      machine_template: { cpu_cores: 2, memory_mb: 2048, disk_gb: 20 },
    });
    expect(spec.worker_pools[0]).toMatchObject({
      count: 3,
      existing_machine_ids: ["w1"],
      machine_template: { cpu_cores: 4, memory_mb: 8192, disk_gb: 80 },
    });
  });
});
