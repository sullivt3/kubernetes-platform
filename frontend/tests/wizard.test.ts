import { describe, expect, it } from "vitest";

import { buildSpec, INITIAL } from "@/features/clusters/create-wizard";

describe("cluster wizard spec", () => {
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
