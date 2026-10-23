# Federated Admin Authentication

DASH supports one configured provider-neutral OpenID Connect provider or
Discord OAuth2 login in addition to local hashed tokens. Federation never
creates authority: an external issuer/subject must be explicitly mapped to an
existing enabled identity in `config/admin-access.json`, and that local
identity's current role/capabilities are resolved on every request.

The owner recovery token remains available. Do not remove it until the full
login, logout, role-change, disable, secret-rotation, and recovery paths have
been tested through the deployed HTTPS hostname.

## Protocol contract

DASH uses the server-side authorization-code flow. OIDC discovery, ID-token
issuer/audience/authorized-party/time/nonce checks, and RS256 signature
verification follow [OpenID Connect Core 1.0](https://openid.net/specs/openid-connect-core-1_0.html).
Discord uses its documented authorization-code grant and the `identify` scope,
then reads the stable user ID from `/users/@me`; see [Discord OAuth2](https://docs.discord.com/developers/topics/oauth2)
and the [Discord user resource](https://docs.discord.com/developers/resources/user).

Both modes add PKCE S256, a 256-bit state value, and a separate OIDC nonce.
State and the PKCE verifier live only in a signed, HttpOnly, SameSite=Lax flow
cookie that expires after ten minutes. State is single-use within the admin
process. Authorization codes, client secrets, provider access tokens, ID
tokens, PKCE verifiers, and session-cookie values are never written to the
audit log or returned to browser JavaScript.

Provider discovery, authorization, token, userinfo, and JWKS endpoints must be
credential-free HTTPS URLs on an explicitly allowed origin. HTTP redirects are
refused so a token request cannot move a client secret or authorization code to
another host. The callback URI must be HTTPS, except for explicit HTTP loopback
testing.

OIDC ID tokens are restricted to RS256. The verifier selects the exact JWK,
performs PKCS#1 v1.5 SHA-256 verification, then checks `iss`, `aud`, `azp` when
needed, `exp`, `iat`, `nonce`, and `sub`. Providers that only issue ES256 ID
tokens are not compatible with this dependency-free build.

## Local identity and subject map

Create or identify the local DASH user first:

```bash
./scripts/admin-access.py add night-ops --role operator \
  --display-name 'Night Operations'
```

Copy the example mapping to the ignored host-local file:

```bash
install -m 600 config/admin-auth-subjects.example.json \
  config/admin-auth-subjects.json
```

The format is:

```json
{
  "version": 1,
  "subjects": [
    {
      "issuer": "https://discord.com",
      "subject": "123456789012345678",
      "localUserId": "night-ops",
      "enabled": true,
      "label": "Night operator Discord identity"
    }
  ]
}
```

Use the provider's immutable subject/user ID, not an email, username, display
name, Discord tag, group name, or role name. Issuer/subject pairs must be
unique. Disabling either the mapping or local identity invalidates the session
on its next request. Local role/capability changes also take effect on the next
request without reissuing a federated session.

## Shared configuration

```dotenv
DUNE_ADMIN_FEDERATED_AUTH_ENABLED=true
DUNE_ADMIN_AUTH_PROVIDER=oidc
DUNE_ADMIN_AUTH_CLIENT_ID=<registered-client-id>
DUNE_ADMIN_AUTH_CLIENT_SECRET_FILE=/workspace-secret-files/admin-oauth-client.secret
DUNE_ADMIN_AUTH_REDIRECT_URI=https://admin.example.test/auth/callback
DUNE_ADMIN_AUTH_SUBJECTS_FILE=/workspace/config/admin-auth-subjects.json
DUNE_ADMIN_AUTH_SESSION_SECRET_FILE=/workspace-secret-files/admin-session.secret
DUNE_ADMIN_AUTH_SESSION_SECONDS=28800
DUNE_ADMIN_AUTH_COOKIE_SECURE=true
```

Write the client secret without a trailing explanation and lock it down:

```bash
install -m 600 /dev/null config/secrets/admin-oauth-client.secret
# Write the secret through your normal secret-management path.
```

`scripts/enable-feature-parity.sh .env --execute` enables the feature gate and
generates a 256-bit `config/secrets/admin-session.secret` if missing. It does
not create a third-party application, client secret, subject mapping, or login
session. Rotating the session secret immediately invalidates all federated flow
and login cookies. Rotating the provider client secret does not invalidate an
already-issued DASH session.

Keep `DUNE_ADMIN_AUTH_COOKIE_SECURE=true` on deployed HTTPS hostnames. Setting
it false is only for deliberate loopback HTTP testing; a Secure cookie is not
sent over the current plain-HTTP LAN hostname.

## Generic OIDC

```dotenv
DUNE_ADMIN_AUTH_PROVIDER=oidc
DUNE_ADMIN_AUTH_ISSUER=https://id.example.test/realms/arrakis
DUNE_ADMIN_AUTH_SCOPES=openid profile email
DUNE_ADMIN_AUTH_ALLOWED_ORIGINS=
```

DASH requests `/.well-known/openid-configuration` beneath the exact issuer and
requires the returned `issuer` to match. By default all endpoints must share
the issuer origin. If a provider legitimately publishes endpoints on another
origin, list exact comma-separated origins in
`DUNE_ADMIN_AUTH_ALLOWED_ORIGINS`; paths and wildcards are not accepted.

Register this exact callback:

```text
https://admin.example.test/auth/callback
```

No group-to-admin-role auto-promotion exists. Map each reviewed OIDC `sub`
explicitly.

## Discord

Create a Discord application and register the exact callback in its OAuth2
settings. Use:

```dotenv
DUNE_ADMIN_AUTH_PROVIDER=discord
DUNE_ADMIN_AUTH_ISSUER=https://discord.com
DUNE_ADMIN_AUTH_CLIENT_ID=<Discord application ID>
DUNE_ADMIN_AUTH_SCOPES=identify
DUNE_ADMIN_AUTH_REDIRECT_URI=https://admin.example.test/auth/callback
```

Put the Discord OAuth2 client secret—not the bot token—in
`admin-oauth-client.secret`. DASH requests only `identify`; it does not request
guild membership or infer admin authority from Discord guild roles. Obtain the
stable numeric Discord user ID for each operator and map it with issuer
`https://discord.com`.

The Discord bot and Discord login are separate credentials and threat
boundaries. The bot token remains in the bot service; the OAuth client secret
remains in the admin panel's private secret file.

## Browser and API behavior

When the gate, provider, client credentials, subject file, local access file,
and session secret are all ready, the dashboard header shows **Sign in with
SSO** or **Sign in with Discord**. Successful callback creates
`dash_admin_session`, a signed HttpOnly SameSite=Lax cookie. The default session
lifetime is eight hours and is bounded to five minutes through seven days.

Useful routes:

| Route | Authentication | Purpose |
| --- | --- | --- |
| `GET /api/auth/federated/status` | public, no secrets | Read provider readiness and current-browser session state. |
| `GET /auth/login` | public | Start state/nonce/PKCE flow and redirect to the provider. |
| `GET /auth/callback` | signed flow cookie | Validate state, exchange code, verify identity, require explicit mapping, and create session. |
| `GET /api/auth/me` | token or federated session | Return the resolved local principal and capabilities. |
| `POST /api/auth/logout` | token or federated session | Expire DASH flow/session cookies. |

Cookies are not bearer API credentials for other origins. Same-origin, Host
allowlist, Origin validation on writes, CSP, frame denial, and the existing
request throttling remain active. Local `X-Admin-Token` and `Authorization:
Bearer` clients continue to work unchanged.

## Failure and recovery

- Missing provider credentials or subject map leaves the gate enabled but
  reports `configured=false`; the sign-in button stays hidden.
- An unmapped external identity is rejected after provider authentication and
  receives no DASH session.
- Disabling/removing the local DASH identity or subject row invalidates access
  on the next request.
- A provider outage does not affect local token login.
- Use the owner recovery token to repair configuration or mappings.
- Restore `config/`, including its private secret/mapping files, from a verified
  DASH backup if host-local identity material is lost.

## Validation

```bash
make test-federated-auth
make test-admin-access-control
python3 scripts/test-admin-panel-safe-surfaces.py
docker compose --env-file .env config >/dev/null
```

The focused tests cover explicit unique mappings, signed-cookie tamper/expiry,
Discord authorization code + PKCE + state + replay, provider-token exclusion,
OIDC discovery, generated RS256 ID-token validation, nonce rejection, HTTPS
endpoint enforcement, exception redaction, and current local identity lookup.

An end-to-end canary requires operator-owned provider credentials and an HTTPS
callback. Validate login as a low-privilege observer first, confirm a forbidden
write, promote the local identity, confirm the capability takes effect without
re-login, disable it, confirm immediate rejection, then verify owner-token
recovery.
