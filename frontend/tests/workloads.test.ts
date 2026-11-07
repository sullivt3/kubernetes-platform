import { describe, expect, it } from "vitest";
import { parseAllDocuments } from "yaml";

import { buildManifest, validateDeployment, type DeploymentInput } from "@/features/workloads/deployment-manifest";

const base: DeploymentInput = {
  name: "web", namespace: "apps", image: "nginx:1.27", replicas: 3, ports: [80],
  env: [{ key: "LOG_LEVEL", value: "debug" }],
  requests: { cpu: "100m", memory: "128Mi" }, limits: { cpu: "", memory: "256Mi" },
  readiness: { type: "http", path: "/healthz", port: 80 }, liveness: { type: "none", path: "/", port: 80 },
  volumes: [{ name: "cfg", type: "configMap", source: "web-config", mountPath: "/etc/web" }],
  pullSecrets: "regcred", nodeSelector: [{ key: "tier", value: "apps" }], spreadAcrossNodes: true, service: "NodePort",
};

describe("deployment manifest", () => {
  it("builds a deployment and service", () => {
    const docs = parseAllDocuments(buildManifest(base)).map((d) => d.toJS());
    expect(docs.map((d) => d.kind)).toEqual(["Deployment", "Service"]);
    const [dep, svc] = docs;
    const container = dep.spec.template.spec.containers[0];
    expect(dep.metadata).toMatchObject({ name: "web", namespace: "apps" });
    expect(dep.spec.replicas).toBe(3);
    expect(container.env).toEqual([{ name: "LOG_LEVEL", value: "debug" }]);
    expect(container.resources).toEqual({ requests: { cpu: "100m", memory: "128Mi" }, limits: { memory: "256Mi" } });
    expect(container.readinessProbe.httpGet).toEqual({ path: "/healthz", port: 80 });
    expect(container.livenessProbe).toBeUndefined();
    expect(container.volumeMounts).toEqual([{ name: "cfg", mountPath: "/etc/web" }]);
    expect(dep.spec.template.spec.volumes).toEqual([{ name: "cfg", configMap: { name: "web-config" } }]);
    expect(dep.spec.template.spec.imagePullSecrets).toEqual([{ name: "regcred" }]);
    expect(dep.spec.template.spec.nodeSelector).toEqual({ tier: "apps" });
    expect(dep.spec.template.spec.topologySpreadConstraints[0].topologyKey).toBe("kubernetes.io/hostname");
    expect(svc.spec).toMatchObject({ type: "NodePort", selector: { app: "web" } });
  });

  it("omits optional sections and the service when not requested", () => {
    const docs = parseAllDocuments(buildManifest({ ...base, service: "none", volumes: [], env: [], pullSecrets: "", nodeSelector: [], spreadAcrossNodes: false })).map((d) => d.toJS());
    expect(docs).toHaveLength(1);
    const spec = docs[0].spec.template.spec;
    expect(Object.keys(spec)).toEqual(["containers"]);
  });

  it("validates input", () => {
    expect(validateDeployment(base)).toEqual([]);
    const errors = validateDeployment({ ...base, name: "Bad_Name", image: "", replicas: -1, ports: [70000], requests: { cpu: "lots", memory: "1Gi" } });
    expect(errors.join(" | ")).toMatch(/Name/);
    expect(errors.join(" | ")).toMatch(/image/);
    expect(errors.join(" | ")).toMatch(/Replicas/);
    expect(errors.join(" | ")).toMatch(/Ports/);
    expect(errors.join(" | ")).toMatch(/lots/);
  });
});
