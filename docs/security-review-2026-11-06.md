# Security review and branch reconciliation — 6 November 2026

All available local and GitHub branch tips are ancestors of main after merging
PR #10 (`codex/disable-codeql-20261031`). The GitLab remote could not be fetched:
the project was unavailable or access was denied. Its cached branch tips were
already merged. No branch history was rewritten or deleted.

CodeQL default setup is `not-configured`; workflows contain no CodeQL actions.
Semgrep and Trivy remain enabled, with scan artifacts retained. The initial
GitHub inventory contained 92 open Semgrep alerts and no open Dependabot alerts.
The four historical Rust `rustls` Dependabot alerts were already fixed.

## Repairs

- Restrict the Docker API broker socket from `0666` to owner-only `0600`.
  The broker and its Admin consumer run as root; unrelated UIDs need no access.
- Keep all Nginx response security headers on `directory-entry.json`. Select
  resource-policy and CORS values with maps in the HTTP context instead of
  overriding inherited headers inside a location. An isolated Nginx instance
  confirmed both the ordinary page and directory JSON receive CSP, permissions,
  framing, referrer, content-type, cache, and resource-policy headers.
- Validate configurable Admin, RabbitMQ and Discord HTTP transport URLs before
  urllib receives them. Reject local-file and non-HTTP schemes, URL credentials,
  control characters, malformed ports and missing hosts; preserve private HTTP
  endpoints. Addon redirects are host-validated before contacting a destination.
- Use psycopg2 SQL identifier composition in database bootstrap. Values in other
  reviewed database queries were already bound separately from SQL syntax.
- Split Compose file lists without changing the shell's global IFS.
- Mark SHA-1 uses as `usedforsecurity=False`: Unreal pak headers, WebSocket
  negotiation and SPDX verification codes require this algorithm. Release
  integrity hashes continue to use SHA-256.
- Quote malformed YAML issue-template descriptions. Remove the admin chat
  credential helper's undefined `FILE_ENV` lookup in favor of the shared secret
  reader. Preserve last-maintenance detail alongside scheduled maintenance.
  Refresh stale public snapshot tests for its existing native Docker interface;
  public rendering does not load private game environment files.

These source repairs do not deploy or restart production services.

## Evidence-based false-positive review

| Finding | Evidence and disposition |
| --- | --- |
| Owner-only directory permissions | `0700` grants no group or other-user access and is intentional for private state, keys and recovery evidence. Preserve these permissions. The world-writable broker socket was repaired separately. |
| SQL construction outside bootstrap | Dynamic fragments are internal constant clauses or generated placeholder counts. Identifiers/values from callers are passed as bound parameters. No caller-provided SQL text is interpolated. |
| HTTP audit warnings | Configurable transports now reject non-HTTP schemes. Research/catalog requests use fixed HTTPS API bases plus encoded query parameters; screenshot discovery uses a constructed loopback HTTP endpoint. Addons enforce HTTPS and a GitHub host allowlist, including redirects. |
| IFS inside command substitutions/subshells | The assignment cannot alter the caller's shell environment. Global assignments in the lifecycle scripts were removed. |
| Fixed host build checker subprocess | The executable and env-file argument are host constants, shell mode is disabled, and admission only permits the all-farm read-only phase with no service overrides or lifecycle flags. Request data cannot select a command. |
| Inline favicon integrity | The favicon is an inline SVG data URI, not a fetched third-party resource. |
| Python 3.6 compatibility warning | The project requires modern Python syntax and runs with Python 3.10 or newer. The subprocess errors argument is supported. |
| Protocol SHA-1 | Required wire/archive/SPDX formats, explicitly non-security uses. It is not used for password storage, signatures or artifact trust. |

False positives are recorded against individual GitHub alerts with their
specific rationale. They are not removed through broad scanner exclusions.
Raw Semgrep/Trivy reports, the initial alert inventory and Nginx header evidence
are retained privately in `backups/security-reviews/20261106/`.

Semgrep reports parser limitations on some valid Bash constructs. Bash syntax
checks independently cover those shell files; the malformed YAML issue template
was repaired. Scanner coverage limitations remain in the raw evidence.

## Verification results

The final scan of source commit `b10a3c0` reported zero Trivy dependency
vulnerabilities and zero secrets. Trivy detected no supported configuration
scanner targets, so the Nginx header behavior was checked independently in an
isolated container. Semgrep returned 77 unsuppressed audit findings plus one
existing in-source-suppressed owner-only directory finding in SARIF. Each was
reviewed against the rationale above; no broad scanner rule was disabled.
The 12 remaining Semgrep parser diagnostics concern Bash constructs accepted
by Bash; all 269 tracked shell files passed `bash -n`.

Targeted checks cover 398 unit/regression tests across Admin, the broker,
HTTP/security boundaries, chat/Discord commands, character slots, release
packaging, restart/API routing, Compose discovery and public-site rendering.
The public-site asset checks and Nginx configuration/header checks also passed.
Raw evidence, individual GitHub dismissal rationales and scan-upload receipts
are retained in the private evidence directory above.

## Validation reconciliation

Full local validation target groups were exercised in an isolated worktree.
Repairs retain network-isolated broker admission, update stale secret-handling
fixtures without permitting plaintext command-line credentials, and stage the
real map-catalog helper in the public-IP canary. That canary only permits a
mock read-only Compose model query and still rejects any lifecycle Docker call.
Its signed inputs and deployment support lists bind the new helper. Timer
verification follows the current OnUnitInactiveSec schedule.

The README images were recaptured from sanitized fixtures in an isolated,
headed browser on the persistent GUI VM, visually checked at 1440 by 1000,
and bound to the current UI/capture-script hashes. The browser session and
fixture server were closed afterward.

A concurrent branch restored the former private-only runner policy. Its history
was merged, while the November 3 repo-specific hosted-runner owner directive
supplied in this session remains authoritative. CodeQL stays disabled.

Hosted validation also exposed older Compose parsing of uninterpolated short
bind mounts. Four parameterized mounts now use long syntax. Compose 2.33.0
renders all 43 services without reading the environment file, and comparison
with Compose 5.6.1 confirms unchanged bind sources, targets and read-only flags.
