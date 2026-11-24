"""Disrupt only the local test platform to verify real interactive-session recovery.

Run from the repository root after sourcing scripts/dev-env.sh. Requires the
documented local Keycloak fixture and one API/worker/beat process. Never run this
against a shared management deployment. Credentials and terminal output are not
printed or written to the report. The Fedora Kubernetes API is never stopped.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import json
import os
import runpy
import select as io_select
import shlex
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import yaml
from app.core.config import get_settings
from app.core.secrets import get_secret_store
from app.db import models  # noqa: F401 -- register all ORM relationships
from app.db.session import get_session_factory
from app.modules.cluster_lifecycle.models import Cluster, ClusterOrigin
from app.modules.command_center import sessions
from app.modules.command_center.session_models import CommandSession
from app.modules.kubernetes import client as kc
from app.modules.kubernetes.service import connection_target
from app.orchestration.models import Operation, OperationStatus
from playwright.sync_api import expect, sync_playwright
from sqlalchemy import select
from sqlalchemy.engine import make_url


def until(check, seconds=90):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.5)
    raise AssertionError("Recovery condition did not become true before its deadline")


class LocalProcesses:
    def __init__(self, root):
        self.root = root
        self.commands = {}
        self.pids = {}
        self.stopped = set()
        self.children = []
        self.logs = root / ".tools/command-recovery"
        self.logs.mkdir(mode=0o700, exist_ok=True)
        for line in subprocess.check_output(
            ["ps", "-axo", "pid=,command="], text=True
        ).splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) != 2:
                continue
            pid, command = parts
            role = None
            try:
                argv = shlex.split(command)
            except ValueError:
                # ps renders unrelated process arguments without shell quoting.
                continue
            if len(argv) < 2:
                continue
            local_python = argv[0] in {"python", sys.executable}
            if local_python and argv[1:8] == [
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                "8000",
            ]:
                role = "api"
            elif (
                str(root / "backend/.venv/bin/celery")
                + " -A app.orchestration.celery_app "
                in command
            ) or (
                local_python
                and argv[1:5] == ["-m", "celery", "-A", "app.orchestration.celery_app"]
            ):
                if " worker " in command:
                    role = "worker"
                elif " beat " in command:
                    role = "beat"
            if role:
                assert role not in self.pids, (
                    "Expected exactly one local process per role"
                )
                self.pids[role] = int(pid)
        assert set(self.pids) == {"api", "worker", "beat"}, (
            "Local API/worker/beat processes are required"
        )
        self.commands = {
            "api": [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                "8000",
                "--timeout-graceful-shutdown",
                "10",
            ],
            "worker": [
                sys.executable,
                "-m",
                "celery",
                "-A",
                "app.orchestration.celery_app",
                "worker",
                "--pool=threads",
                "--concurrency=2",
                "--loglevel=INFO",
            ],
            "beat": [
                sys.executable,
                "-m",
                "celery",
                "-A",
                "app.orchestration.celery_app",
                "beat",
                "--loglevel=INFO",
                "--schedule",
                "../.tools/celerybeat-schedule",
            ],
        }

    def stop(self, role):
        os.kill(self.pids[role], signal.SIGKILL)
        self.stopped.add(role)

        def gone():
            try:
                os.kill(self.pids[role], 0)
                return False
            except ProcessLookupError:
                return True

        until(gone, 15)

    def restore(self):
        for role in ("api", "worker", "beat"):
            if role not in self.stopped:
                continue
            with (self.logs / (role + ".log")).open("ab") as log:
                child = subprocess.Popen(
                    self.commands[role],
                    cwd=self.root / "backend",
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    start_new_session=True,
                )
            self.children.append(child)
            self.pids[role] = child.pid
            self.stopped.remove(role)
        until(lambda: self.healthy("/api/v1/health/live"), 30)

    @staticmethod
    def healthy(path):
        try:
            return (
                httpx.get("http://127.0.0.1:8000" + path, timeout=3).status_code == 200
            )
        except httpx.HTTPError:
            return False


class Proxy(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, upstream):
        self.upstream = upstream
        self.enabled = threading.Event()
        self.enabled.set()
        super().__init__(("127.0.0.1", 0), Relay)


class Relay(socketserver.BaseRequestHandler):
    def handle(self):
        if not self.server.enabled.is_set():
            return
        with contextlib.closing(
            socket.create_connection(self.server.upstream, timeout=5)
        ) as upstream:
            streams = [self.request, upstream]
            try:
                while self.server.enabled.is_set():
                    for source in io_select.select(streams, [], [], 0.2)[0]:
                        data = source.recv(65536)
                        if not data:
                            return
                        (upstream if source is self.request else self.request).sendall(
                            data
                        )
            except OSError:
                return


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cluster", required=True)
    parser.add_argument("--allow-local-test-disruptions", action="store_true")
    parser.add_argument("--preflight-only", action="store_true")
    args = parser.parse_args()
    assert args.preflight_only or args.allow_local_test_disruptions, (
        "Explicit local test disruption acknowledgement is required"
    )
    root = Path(__file__).resolve().parents[1]
    os.chdir(root / "backend")
    settings = get_settings()
    database = make_url(settings.database_url)
    broker = urlsplit(settings.redis_url)
    assert database.host in {"127.0.0.1", "localhost"} and database.port == 55432, (
        "Only the isolated local PostgreSQL test stack on port 55432 is allowed"
    )
    assert broker.hostname in {"127.0.0.1", "localhost"} and broker.port == 56379, (
        "Only the isolated local Redis test stack on port 56379 is allowed"
    )
    assert (
        subprocess.check_output(
            [
                "docker",
                "inspect",
                "--format",
                '{{index .Config.Labels "com.docker.compose.project"}}',
                "kplatform-test-redis-1",
            ],
            text=True,
        ).strip()
        == "kplatform-test"
    ), "Redis must belong to the documented local test Compose project"
    assert os.environ.get("KPLATFORM_TEST_USER_PASSWORD"), (
        "Source scripts/dev-env.sh first"
    )
    processes = LocalProcesses(root)
    with get_session_factory()() as db:
        assert not db.scalar(
            select(CommandSession.id).where(CommandSession.status != "closed").limit(1)
        ), "End existing interactive sessions first"
        assert not db.scalar(
            select(Operation.id)
            .where(
                Operation.status.in_(
                    [OperationStatus.RUNNING, OperationStatus.CANCELLING]
                )
            )
            .limit(1)
        ), "Wait for current operations before disrupting test processes"
        cluster = db.scalars(select(Cluster).where(Cluster.name == args.cluster)).one()
        target = connection_target(db, get_secret_store(), cluster)
        if args.preflight_only:
            print(
                "Local test stack, process identities, target and idle-session checks passed; nothing changed"
            )
            return
        parts = urlsplit(target.server)
        api = kc.KubeClient(target)
        proxy = Proxy((parts.hostname, parts.port or 443))
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        fixture = Cluster(
            organization_id=cluster.organization_id,
            project_id=cluster.project_id,
            name="command-recovery-" + uuid.uuid4().hex[:12],
            distribution=cluster.distribution,
            origin=ClusterOrigin.IMPORTED,
            lifecycle_state=cluster.lifecycle_state,
            kubernetes_version=cluster.kubernetes_version,
            access_credential_id=cluster.access_credential_id,
            api_server_override="https://127.0.0.1:" + str(proxy.server_address[1]),
            tls_server_name=target.verify_name or parts.hostname,
            spec={},
            status_detail={},
        )
        db.add(fixture)
        db.commit()
        cid, name = str(fixture.id), fixture.name
    harness = runpy.run_path(str(root / "scripts/check-oidc.py"))
    path = harness["API"] + "/clusters/" + cid + "/command-sessions"
    created = []
    delegated = []
    redis_paused = False
    binding_finalizer = None
    results = []

    def record(sid):
        with get_session_factory()() as db:
            row = db.get(CommandSession, uuid.UUID(sid))
            return row.status

    def absent(row):
        ns = row["namespace"]
        assert api.request("GET", "/api/v1/namespaces/" + ns)[0] == 404
        assert (
            api.request(
                "GET", "/apis/rbac.authorization.k8s.io/v1/clusterrolebindings/" + ns
            )[0]
            == 404
        )

    def denied(client):
        try:
            return client.request("GET", "/api/v1/nodes")[0] >= 400
        except kc.ClusterAuthError:
            return True

    def passed(message):
        results.append(message)
        print("PASS: " + message, flush=True)

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                ctx, page, _ = harness["login"](
                    browser,
                    "platform-admin",
                    os.environ["KPLATFORM_TEST_USER_PASSWORD"],
                )
                headers = harness["mutation_headers"](ctx)

                def create():
                    response = ctx.request.post(
                        path,
                        headers=headers,
                        data={"confirm": name, "acknowledge_cluster_admin": True},
                    )
                    assert response.status == 202, "Session creation was not accepted"
                    sid = response.json()["id"]
                    created.append(uuid.UUID(sid))
                    until(lambda: record(sid) == "ready", 120)
                    row = next(
                        r
                        for r in ctx.request.get(path).json()["items"]
                        if r["id"] == sid
                    )
                    secret = kc.get_json(
                        api,
                        "/api/v1/namespaces/"
                        + row["namespace"]
                        + "/secrets/credentials",
                    )
                    token = yaml.safe_load(
                        base64.b64decode(secret["data"]["kubeconfig"])
                    )["users"][0]["user"]["token"]
                    client = kc.KubeClient(
                        replace(
                            target,
                            kubeconfig=replace(
                                target.kubeconfig,
                                token=token,
                                client_cert_pem=None,
                                client_key_pem=None,
                            ),
                            token_provider=None,
                        )
                    )
                    delegated.append(client)
                    assert client.request("GET", "/api/v1/nodes")[0] == 200
                    return row, client

                row, client = create()
                page.evaluate(
                    '(id)=>localStorage.setItem("kplatform.selectedCluster",id)', cid
                )
                page.goto(harness["BASE"] + "/ops/command-center")
                page.get_by_role(
                    "button", name="Open interactive terminal", exact=True
                ).click()
                expect(
                    page.get_by_text("Interactive session: open", exact=True)
                ).to_be_visible(timeout=30000)
                until(lambda: record(row["id"]) == "active")
                processes.stop("api")
                until(lambda: record(row["id"]) == "closed")
                absent(row)
                assert denied(client)
                processes.restore()
                passed(
                    "Abrupt API death: real heartbeat timeout reclaimed active session and revoked its token; API restarted"
                )

                row, client = create()
                processes.stop("beat")
                processes.stop("worker")
                response = ctx.request.post(
                    path + "/" + row["id"] + "/close", headers=headers
                )
                assert response.status == 202 and record(row["id"]) == "closing"
                assert (
                    api.request("GET", "/api/v1/namespaces/" + row["namespace"])[0]
                    == 200
                )
                processes.restore()
                until(lambda: record(row["id"]) == "closed")
                absent(row)
                assert denied(client)
                passed(
                    "Worker/scheduler loss: close remained durable while stopped; restart drained cleanup and revoked token"
                )

                row, client = create()
                subprocess.run(
                    ["docker", "pause", "kplatform-test-redis-1"],
                    check=True,
                    stdout=subprocess.DEVNULL,
                )
                redis_paused = True
                cookies = {c["name"]: c["value"] for c in ctx.cookies(harness["BASE"])}
                with ThreadPoolExecutor(max_workers=1) as pool:
                    request = pool.submit(
                        httpx.post,
                        path + "/" + row["id"] + "/close",
                        headers=headers,
                        cookies=cookies,
                        timeout=45,
                    )
                    try:
                        until(lambda: record(row["id"]) == "closing", 15)
                        assert (
                            api.request(
                                "GET", "/api/v1/namespaces/" + row["namespace"]
                            )[0]
                            == 200
                        )
                    finally:
                        subprocess.run(
                            ["docker", "unpause", "kplatform-test-redis-1"],
                            check=True,
                            stdout=subprocess.DEVNULL,
                        )
                        redis_paused = False
                    assert request.result().status_code == 202
                until(lambda: record(row["id"]) == "closed")
                absent(row)
                assert denied(client)
                passed(
                    "Real Redis interruption: close committed before enqueue; broker recovery completed cleanup without losing lease"
                )

                row, client = create()
                proxy.enabled.clear()
                response = ctx.request.post(
                    path + "/" + row["id"] + "/close", headers=headers
                )
                assert response.status == 202
                time.sleep(5)
                assert record(row["id"]) == "closing", (
                    "Unreachable cluster must not be reported as cleaned"
                )
                assert (
                    api.request("GET", "/api/v1/namespaces/" + row["namespace"])[0]
                    == 200
                )
                assert processes.healthy("/api/v1/health/live")
                proxy.enabled.set()
                until(lambda: record(row["id"]) == "closed")
                absent(row)
                assert denied(client)
                passed(
                    "Validation connection outage: cleanup stayed pending, platform stayed available, reconnect triggered cleanup"
                )

                row, client = create()
                binding_finalizer = (
                    "/apis/rbac.authorization.k8s.io/v1/clusterrolebindings/"
                    + row["namespace"]
                )
                binding = kc.get_json(api, binding_finalizer)
                binding["metadata"]["finalizers"] = ["kplatform.io/recovery-drill"]
                sessions.request(api, "PUT", binding_finalizer, body=binding)
                response = ctx.request.post(
                    path + "/" + row["id"] + "/close", headers=headers
                )
                assert response.status == 202
                until(lambda: not kc.get_json(api, binding_finalizer).get("subjects"))
                assert record(row["id"]) == "closing" and denied(client)
                binding = kc.get_json(api, binding_finalizer)
                assert binding["metadata"]["finalizers"] == [
                    "kplatform.io/recovery-drill"
                ]
                binding["metadata"]["finalizers"] = []
                sessions.request(api, "PUT", binding_finalizer, body=binding)
                binding_finalizer = None
                until(lambda: record(row["id"]) == "closed")
                absent(row)
                passed(
                    "Finalizer held cleanup pending while grant subjects were revoked; removing only drill finalizer allowed retry"
                )
            finally:
                browser.close()
    finally:
        proxy.enabled.set()
        if redis_paused:
            subprocess.run(
                ["docker", "unpause", "kplatform-test-redis-1"],
                check=True,
                stdout=subprocess.DEVNULL,
            )
        processes.restore()
        if binding_finalizer:
            status, raw = api.request("GET", binding_finalizer)
            if status == 200:
                binding = json.loads(raw)
                binding["metadata"]["finalizers"] = [
                    f
                    for f in binding["metadata"].get("finalizers", [])
                    if f != "kplatform.io/recovery-drill"
                ]
                sessions.request(api, "PUT", binding_finalizer, body=binding)
        for sid in created:
            sessions.mark_closing(sid, "recovery_drill_cleanup")
            until(lambda sid=sid: sessions.cleanup(sid))
        for client in delegated:
            client.close()
        with get_session_factory()() as db:
            assert not db.scalar(
                select(CommandSession.id).where(
                    CommandSession.cluster_id == uuid.UUID(cid),
                    CommandSession.status != "closed",
                )
            )
            db.delete(db.get(Cluster, uuid.UUID(cid)))
            db.commit()
        proxy.shutdown()
        proxy.server_close()
        api.close()
    report = {
        "passed": results,
        "cluster": args.cluster,
        "finished_at": sessions.now().isoformat(),
        "limits": "Local single API/worker/beat and Fedora only; no host shutdown, HA or cloud certification",
    }
    (processes.logs / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(
        "All recovery scenarios passed; test processes restored and fixture cluster/runtime grants removed",
        flush=True,
    )


if __name__ == "__main__":
    main()
