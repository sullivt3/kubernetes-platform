"""Exercise the local Keycloak/browser flow and test-user application roles.

Run with scripts/dev-env.sh sourced and the API/UI running. This grants the
documented local operator/viewer fixtures access and creates/deletes one test
credential. It never touches infrastructure or managed Kubernetes clusters.
"""

from __future__ import annotations

import os
import time
import uuid

from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

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
