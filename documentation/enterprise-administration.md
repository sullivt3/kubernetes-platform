# Phase 7: enterprise administration

Phase 7 is in progress. The first implementation adds OIDC authentication,
database-controlled memberships, application permissions, and administration UI.
Dedicated automated coverage now exercises authentication, memberships, application
permissions, project isolation, operations and worker scope in `backend/tests/test_identity.py`.
The local Keycloak/browser harness and setup are documented in
[local testing](local-testing.md). On 2026-11-11 it passed real Keycloak logins for
the administrator, viewer and operator, role checks, cookie assertions and UI
logout/session revocation. This does not make Phase 7 complete or establish
compatibility with every production identity provider.

## Current implementation

- OIDC authorization code flow with PKCE S256, nonce, and a browser-bound,
  single-use, five-minute login attempt.
- Signature, issuer, audience, expiry, issued-at and subject validation with PyJWT;
  cached discovery/JWKS and key rotation. Only configured asymmetric algorithms
  are accepted. JWT role/group claims do not assign platform privileges.
- Browser sessions use random opaque tokens, with only hashes stored in PostgreSQL.
  Session cookies are HttpOnly, host-only, SameSite=Lax, and Secure on HTTPS.
  Provider access/refresh tokens are not stored or returned to the browser.
- Cookie-authenticated changes require a CSRF token bound to the server session;
  a supplied Origin must match `PUBLIC_URL`. API bearer access tokens use the
  separate API audience and do not require CSRF tokens.
- Organization/project membership checks on every authenticated request. Disabled
  users are rejected immediately. Operation streams and terminals recheck access
  periodically; already accepted asynchronous jobs continue under their recorded
  identity and scope. Membership removal does not implicitly cancel those jobs.
- Administration pages for users, tenant records, role assignments and effective
  permissions. Navigation and write-only routes reflect permissions. Viewer pages
  display a read-only notice; the API independently rejects every write, Secret
  reveal and terminal attempt. Existing resource action menus still need the
  remaining control-by-control visibility pass.
- Membership changes, user changes, administrative recovery and permission denials
  are audited. Existing encrypted credential storage and rotation remain in use.

## Configure an identity provider

Run `alembic upgrade head` from `backend` before starting the updated services.
The local development installation can continue using `AUTH_MODE=dev`.

For Keycloak or a compatible OIDC provider, configure:

```dotenv
AUTH_MODE=oidc
PUBLIC_URL=https://platform.example.com
OIDC_ISSUER_URL=https://identity.example.com/realms/platform
OIDC_CLIENT_ID=kubernetes-platform-web
OIDC_CLIENT_SECRET=<confidential-client-secret>
OIDC_AUDIENCE=kubernetes-platform-api
AUTH_SESSION_SECONDS=28800
```

Use a client with the authorization code/standard flow and PKCE S256 enabled.
Its exact valid redirect URI is
`https://platform.example.com/api/v1/auth/callback` (adjust for a custom API prefix).
Public clients may omit the secret. Confidential clients must support
`client_secret_post`. Configure the access-token audience to include
`kubernetes-platform-api`; this must differ from the browser client ID so an ID
token cannot be used as an API access token. JWT access tokens must include
`iss`, `sub`, `aud`, `exp`, `iat` and a `kid` header. Opaque access tokens are not
supported. Discovery and JWKS endpoints must be reachable by the backend without
redirects; TLS certificates must be trusted by its HTTP client.

Issuer and public URLs require HTTPS in staging/production. Development/test allow
HTTP only on loopback hosts. The backend never derives callback URLs from a
caller-supplied Host header. Configure `BACKEND_URL` in the frontend to reach the
private API, and register the public frontend origin. Login, callback, cookies,
CSRF-protected API calls, SSE and WebSocket traffic all pass through that origin.

Login starts are limited to 30 per minute per directly connected client IP using
shared Redis. Login fails closed if Redis is unavailable. Behind the Next.js proxy
the address may be shared; the public reverse proxy should enforce its own limits
using the trusted client address. Forwarded client-IP headers are not blindly trusted.
General API rate limiting is still pending.

### First administrator and recovery

The first login creates a user without tenant membership. The waiting screen shows
the stable `issuer|sub` identity. On the backend host, grant administrator access:

```powershell
.\.venv\Scripts\python.exe -m app.cli grant-admin --subject 'https://identity.example.com/realms/platform|subject-id'
```

Alternatively set `OIDC_BOOTSTRAP_SUBJECT` to the exact trusted provider `sub` before
that user's first sign-in. It only affects new user insertion; it does not restore
privileges removed later. Remove that bootstrap setting after initial setup.
The recovery command activates the existing account, grants platform administrator
access and records an audit event. The development identity cannot authenticate in
OIDC mode and does not count toward the last OIDC platform-administrator safeguard.

Login again or reload the page, then use **Administration > Users** and
**Organizations & projects**. User email is informational, must be verified by the
provider to be recorded, and is never used as an authorization identifier. Users
cannot create their own memberships or gain access through role claims.

Local logout revokes the current browser session. It does not terminate the
identity provider's SSO session; a subsequent sign-in may not ask for a password.
Sessions have an absolute lifetime; this implementation does not silently refresh
provider tokens. Disabling a user revokes all of their platform sessions and blocks
subsequent bearer requests. IdP-side account disablement alone does not revoke an
already established local session; disable the platform account when immediate
platform revocation is required.

## Roles and project boundaries

| Role | Scope |
|---|---|
| `platform-admin` | All organizations, user administration and platform actions |
| `org-admin` | Memberships, projects, audit records and resource operations in one organization |
| `operator` | Read/write resources, credentials, Secret reveals and terminals |
| `viewer` | Read resources and redacted credential metadata; no writes, reveals or terminals |
| `member` | No resource access until an explicit project operator/viewer grant is selected |

Organization operator/viewer grants cover **all** projects in that organization.
For restricted access, grant organization `member`, then project `operator` or
`viewer`. Projects cannot grant organization/platform administration.

Select the organization and project in the top bar or **Context & permissions** in
the account menu. Switching context reloads the app to clear query/router caches
and reconnect streams. API clients can use `X-Organization-ID` and `X-Project-ID`;
these are selectors, never authority: membership is checked before they are used.

API sessions filter tenant models by organization and the selected project,
including detail lookups. New resources inherit the project. Linked credentials,
connections, machines and clusters must share a project. Worker-created resources
inherit their target operation's project. Project streams filter other projects'
operation notifications. Resource names remain unique across the organization.
Existing resources with a null project remain organization-wide; they are not
automatically moved into the default project. Select **Entire organization** to
manage them. Moving existing resource groups between projects is not implemented.

A project contains whole clusters. A project operator can administer those clusters
with the platform's Kubernetes credentials; namespace-level isolation requires
separate Kubernetes policies. Application roles and Kubernetes RoleBindings are
different layers of authorization.

## API additions

- `GET /auth/session`: public session state, available contexts and effective access.
- `GET /auth/login`, `GET /auth/callback`: browser OIDC flow.
- `POST /auth/logout`, `POST /auth/context`: CSRF-protected session actions.
- `GET /administration/users?limit=100&offset=0`, `PUT /administration/users/{id}`.
- `GET/POST /administration/organizations`, `PUT /administration/organization`.
- `POST /administration/projects`, `PUT/DELETE /administration/projects/{id}`.
- `GET/PUT /administration/members`, `DELETE /administration/members/{user_id}`.
- `PUT /administration/projects/{id}/members`, `DELETE /administration/projects/{id}/members/{user_id}`.
- `GET /administration/permissions`.

Paths are relative to `/api/v1`. Project deletion requires `confirm=<slug>` and
refuses projects with resource references; the default project cannot be deleted.
At least one active OIDC platform administrator and one existing organization
administrator must remain when their administrative privileges are removed.

## Remaining Phase 7 work

- Extend authentication, membership, isolation and permission regression coverage,
  including identity-provider outages, key rotation and concurrent administration;
  verify OIDC behavior against the intended deployment's identity provider.
- Finish hiding or disabling individual mutation controls for restricted roles.
- Kubernetes Roles/ClusterRoles/bindings, ServiceAccounts and security-policy views.
- Security posture reporting and further audit filtering/reporting.
- Backup and restore workflows for platform and cluster state, including key recovery.
- General API rate limiting and production TLS/reverse-proxy deployment guidance.
- Credential access review and any additional secrets-store integration selected
  for this phase; a Vault adapter is not implemented.

Protocol references: [OIDC Core](https://openid.net/specs/openid-connect-core-1_0.html),
[OAuth security best current practice](https://www.rfc-editor.org/rfc/rfc9700.html),
and [PyJWT validation documentation](https://pyjwt.readthedocs.io/en/stable/usage.html).
