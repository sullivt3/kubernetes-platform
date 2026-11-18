"""Exercise the local Keycloak/browser flow and test-user application roles.

Run with scripts/dev-env.sh sourced and the API/UI running. This grants the
documented local operator/viewer fixtures access and creates/deletes one test
credential. It never touches infrastructure or managed Kubernetes clusters.
"""

from __future__ import annotations

import os
import re
import time
import uuid

from playwright.sync_api import Browser, BrowserContext, Page, Route, expect, sync_playwright

BASE = "http://localhost:3000"
API = BASE + "/api/v1"


def login(
    browser: Browser, username: str, password: str
) -> tuple[BrowserContext, Page, dict]:
    context = browser.new_context(base_url=BASE)
    page = context.new_page()
    page.set_default_timeout(90_000)
    page.goto(BASE)
    page.get_by_role("link", name="Sign in", exact=True).click()
    page.locator("#username").fill(username)
    page.locator("#password").fill(password)
    page.locator("#kc-login").click()
    page.wait_for_url(BASE + "/")
    session = context.request.get(API + "/auth/session").json()
    assert session["authenticated"], f"{username} did not establish a platform session"
    cookies = {cookie["name"]: cookie for cookie in context.cookies(BASE)}
    assert cookies["kplatform_session"]["httpOnly"]
    assert cookies["kplatform_session"]["sameSite"] == "Lax"
    assert not cookies["kplatform_csrf"]["httpOnly"]
    return context, page, session


def mutation_headers(context: BrowserContext) -> dict[str, str]:
    cookies = {cookie["name"]: cookie["value"] for cookie in context.cookies(BASE)}
    return {"X-CSRF-Token": cookies["kplatform_csrf"], "Origin": BASE}


def check_resource_actions(page: Page, *, can_write: bool) -> None:
    pages = (
        (
            "/infrastructure/machines",
            "Machines",
            (("button", "Register machine"), ("link", "Provision machines")),
        ),
        (
            "/kubernetes/clusters",
            "Clusters",
            (("link", "Create cluster"), ("link", "Import cluster")),
        ),
        (
            "/infrastructure/providers",
            "Infrastructure providers",
            (("button", "Add connection"),),
        ),
    )
    for path, title, actions in pages:
        page.goto(BASE + path)
        main = page.get_by_role("main")
        expect(main.get_by_role("heading", name=title, exact=True)).to_be_visible()
        for role, label in actions:
            control = main.get_by_role(role, name=label, exact=True)
            if can_write:
                expect(control.first).to_be_visible()
            else:
                expect(control).to_have_count(0)

    page.get_by_role("button", name=re.compile("Search pages and operations")).click()
    search = page.get_by_role("dialog", name="Search")
    expect(search).to_be_visible()
    create = search.get_by_role("option", name="Create cluster", exact=True)
    if can_write:
        expect(create).to_be_visible()
    else:
        expect(create).to_have_count(0)

    page.goto(BASE + "/workloads/deployments/new")
    heading = "New deployment" if can_write else "Access restricted"
    expect(page.get_by_role("heading", name=heading, exact=True)).to_be_visible()


def check_inventory_discovery(page: Page, *, can_write: bool) -> None:
    # Use a fixture in the browser so this check never triggers discovery on a real provider.
    identifier = str(uuid.uuid4())
    connection = {
        "id": identifier,
        "name": "Browser check provider",
        "provider_type": "local",
        "provider_name": "Local Docker",
        "category": "local",
        "status": "connected",
        "capabilities": ["discover"],
    }
    discovery = API + f"/provider-connections/{identifier}/discover"
    history = re.compile(re.escape(API) + r"/operations\?.*\btype=provider\.discover(?:&|$)")
    discovery_calls: list[str] = []

    def handle_discovery(route: Route) -> None:
        discovery_calls.append(route.request.method)
        route.fulfill(status=202, json={"id": identifier})

    page.route(API + "/provider-connections", lambda route: route.fulfill(json=[connection]))
    page.route(
        history,
        lambda route: route.fulfill(json={"items": [], "total": 0, "limit": 100, "offset": 0}),
    )
    page.route(discovery, handle_discovery)
    page.route(
        API + f"/operations/{identifier}",
        lambda route: route.fulfill(
            json={"id": identifier, "status": "succeeded", "result": {"machines": []}},
        ),
    )

    with page.expect_response(history) as loaded_history:
        page.goto(BASE + "/infrastructure/virtual-machines")
    assert loaded_history.value.ok
    expect(page.get_by_text(connection["name"], exact=True)).to_be_visible()
    if can_write:
        expect(page.get_by_text("No machines found", exact=True)).to_be_visible()
        assert discovery_calls == ["POST"], discovery_calls
        expect(page.get_by_role("button", name="Discover again", exact=True)).to_be_enabled()
    else:
        expect(page.get_by_text("Not discovered yet.", exact=True)).to_be_visible()
        expect(page.get_by_role("button", name=re.compile(r"^Discover( again)?$"))).to_have_count(0)
        assert not discovery_calls, discovery_calls
        response = page.context.request.post(discovery, headers=mutation_headers(page.context), data={})
        assert response.status == 403, response.status


def main() -> None:
    password = os.environ.get("KPLATFORM_TEST_USER_PASSWORD")
    if not password:
        raise SystemExit("Source scripts/dev-env.sh before running this check")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            admin, admin_page, session = login(browser, "platform-admin", password)
            assert session["user"]["is_platform_admin"]
            admin_headers = mutation_headers(admin)
            response = admin.request.post(
                API + "/system/self-check", headers=admin_headers
            )
            assert response.status == 202
            operation_id = response.json()["id"]
            deadline = time.monotonic() + 60
            while True:
                operation = admin.request.get(
                    API + f"/operations/{operation_id}"
                ).json()
                if operation["status"] in {"succeeded", "failed", "cancelled"}:
                    assert operation["status"] == "succeeded", (
                        "Real worker self-check failed"
                    )
                    break
                assert time.monotonic() < deadline, "Real worker self-check timed out"
                time.sleep(0.5)
            print("Real API, Redis, worker and operation pipeline passed")
            for role in ("viewer", "operator"):
                context, page, session = login(browser, role, password)
                grant = admin.request.put(
                    API + "/administration/members",
                    headers=admin_headers,
                    data={"subject": session["user"]["subject"], "role": role},
                )
                assert grant.status == 200, f"Could not grant local {role} membership"
                page.reload()
                assert context.request.get(API + "/clusters").status == 200
                assert context.request.get(API + "/administration/users").status == 403
                page.goto(BASE + "/security/credentials")
                if role == "viewer":
                    page.get_by_text("Viewer access:", exact=False).wait_for()
                    assert (
                        page.get_by_role(
                            "button", name="Add credential", exact=True
                        ).count()
                        == 0
                    )
                else:
                    page.get_by_role(
                        "button", name="Add credential", exact=True
                    ).first.wait_for()
                name = "oidc-check-" + uuid.uuid4().hex[:12]
                headers = mutation_headers(context)
                created = context.request.post(
                    API + "/credentials",
                    headers=headers,
                    data={
                        "name": name,
                        "kind": "ssh_password",
                        "secret_value": "disposable-test-value",
                    },
                )
                assert created.status == (403 if role == "viewer" else 201)
                if role == "operator":
                    identifier = created.json()["id"]
                    deleted = context.request.delete(
                        API + f"/credentials/{identifier}",
                        headers=headers,
                        params={"confirm": name},
                    )
                    assert deleted.status == 204
                check_resource_actions(page, can_write=role == "operator")
                check_inventory_discovery(page, can_write=role == "operator")
                context.close()
                print(f"Real OIDC login and {role} access checks passed")
            admin_page.get_by_role("button", name="Account", exact=True).click()
            admin_page.get_by_role("menuitem", name="Sign out", exact=True).click()
            admin_page.get_by_role("link", name="Sign in", exact=True).wait_for()
            assert (
                admin.request.get(API + "/auth/session").json()["authenticated"]
                is False
            )
            admin.close()
            print("Administrator browser logout revoked the platform session")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
