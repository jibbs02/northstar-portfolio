# Cloudflare Access and Tunnel runbook

This example deployment exposes one authenticated hostname through an outbound-only Cloudflare
Tunnel. All `northstar.example.com` and `example.com` values are placeholders. Set
`NORTHSTAR_PRODUCTION_HOSTNAME` in the external production secrets file and use that same host
for the Tunnel route, Access application, DNS configuration, and Schwab callback registration.
The read-only Schwab callback is implemented and remains protected by Access; provider enablement
requires the separate integration acceptance procedure.

Do this procedure only on the production Mac mini, from a clean checkout of an approved commit.
Never paste a tunnel token, Access audience, team issuer, allowed email, encryption key, session key,
cookie, JWT, or recovery value into Git, a command argument, a support transcript, or Codex. The
development Mac must not hold the final runtime or tunnel credential.

## Fixed boundary

- Public origin: `https://northstar.example.com`
- Origin service: `http://127.0.0.1:8010`
- Tunnel name: `northstar-local`
- Exactly one published application route for the fixed hostname, followed by a catch-all HTTP 404
- No private-network route and no additional public hostname
- Access application covers every path, including `/healthz` and `/integrations/schwab/callback`
- Application authentication uses `Cf-Access-Jwt-Assertion`, not an identity header or cookie
- App, container, and cloudflared request/access logs remain disabled; cloudflared uses warning level

Cloudflare Access is the edge authorization layer, and Northstar is an independent authorization
layer. Northstar accepts only RS256 JWTs whose signature, exact team issuer, exact singleton
application audience, subject, email, `exp`, `iat`, and `nbf` validate. The normalized email must
equal the one external allow-list value. Signing keys come only from the issuer's fixed
`/cdn-cgi/access/certs` endpoint and are cached briefly; an unknown rotated key triggers a bounded
refresh. A missing claim, malformed token, wrong algorithm/key/issuer/audience/email, future token,
expired token, or JWKS outage fails closed with a generic response.

## 1. Host and account prerequisites

Before creating any Cloudflare object:

1. Confirm this is the final Mac mini, FileVault is on, its recovery key is stored separately, the
   repository is clean, and the reviewed branch/commit is checked out.
2. Confirm DNSSEC remains active for `example.com` and there is no existing A, AAAA, CNAME, Tunnel,
   Access, redirect, Worker, or Pages route for `northstar.example.com`.
3. Confirm Docker publishes `127.0.0.1:8010` only and the router has no Northstar port forward.
4. Do not set `NORTHSTAR_REPLICATION_DIR` unless an encrypted volume is mounted at
   `/Volumes/NorthstarRecovery` and its owner-only destination already exists at
   `/Volumes/NorthstarRecovery/northstar-backups`. An absent/unlocked volume means no secondary copy,
   not a fallback to another path.

## 2. Create Access before a tunnel route

In Cloudflare Zero Trust, create a self-hosted application before making the hostname reachable:

1. Name it `Northstar Local` and set the only application domain to
   `northstar.example.com`, with no path restriction. Do not add a bypass or service-token policy.
2. Set application and policy sessions to at most eight hours; one hour is preferred. Require MFA at
   every Access login, or use a maximum eight-hour MFA duration if every-login is impractical.
   Prefer a passkey or hardware security key over weaker factors when the chosen provider supports it.
3. Create one Allow policy. Its only Include selector is the exact single user email. Do not use
   `Everyone`, an email domain, `Emails ending in`, a login method, or all valid emails. Add an MFA
   requirement supported by the chosen identity provider or enable Access independent MFA.
4. Verify unmatched identities are denied and there are no higher-precedence Bypass or Service Auth
   policies. Save the application.
5. Copy the exact team issuer and application AUD only into the Mac mini's external `0600`
   `~/.northstar/secrets.env` through the production-runtime procedure below. Do not put either value
   in this repository. The issuer has exactly the form
   `https://<team>.cloudflareaccess.com`, with no trailing slash or path.

Cloudflare documents the policy precedence and dangerous broad Include rules in its
[Access policy reference](https://developers.cloudflare.com/cloudflare-one/access-controls/policies/),
MFA options in [Enforce MFA](https://developers.cloudflare.com/cloudflare-one/access-controls/policies/mfa-requirements/),
and application session behavior in
[Session management](https://developers.cloudflare.com/cloudflare-one/access-controls/access-settings/session-management/).

## 3. Disable caching and transformations for the hostname

In the `example.com` zone:

1. Create a Cache Rule matching only `http.host eq "northstar.example.com"` and set Cache
   eligibility to **Bypass cache**. Put it after any broader cache-enabling rule so it wins.
2. Set browser caching to bypass or respect origin for the same host; never override Northstar's
   `Cache-Control: no-store, private, max-age=0` response.
3. Disable APO, Zaraz, Rocket Loader, email-address obfuscation, analytics injection, Automatic
   Signed Exchanges, and any HTML,
   image, script, URL, query-string, or response-body transformation for this hostname.
4. Do not add a Worker, redirect, origin rule, cache key, analytics script, Zaraz integration, or
   third-party application to the hostname.
5. Do not enable zone-wide HSTS or `includeSubDomains` for this change. Northstar itself sends the
   exact-host header with a conservative one-day starting duration; raise it only after stable live
   operation and a separate review.

Cloudflare's [Cache Rule settings](https://developers.cloudflare.com/cache/how-to/cache-rules/settings/)
define the explicit bypass action. A live response may show `CF-Cache-Status: DYNAMIC` rather than
the word `BYPASS`; verify both the rule trace and Northstar's own no-store header.

## 4. Create the remotely managed tunnel

In Zero Trust **Networks > Connectors > Cloudflare Tunnels**:

1. Create a remotely managed `cloudflared` tunnel named exactly `northstar-local`.
2. Add one published application route:
   - hostname: `northstar.example.com`
   - service: `http://127.0.0.1:8010`
   - HTTP Host Header: `northstar.example.com`
   - Protect with Access: enabled, selecting the existing `Northstar Local` application
3. Add the final catch-all service `http_status:404`. Confirm there are exactly those two entries,
   in that order, and no private-network routes.
4. Confirm the generated DNS record is proxied and targets this tunnel. It must not disclose or point
   to the home public IP.

The exact Host override is required because Northstar does not trust generic forwarded headers.
Northstar accepts the public Host only through its production boundary, denies protected routes over
the local Host even if a caller forges proxy headers, and allows unauthenticated local `/healthz`
only for Docker health checks. Cloudflare Tunnel is
[outbound-only](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/);
Cloudflare's ingress documentation requires a final
[catch-all rule](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/local-management/configuration-file/).

## 5. Create the final runtime and recovery checkpoint

Run this on the Mac mini. Prompts are hidden and values are never echoed:

```bash
.venv/bin/python scripts/create_production_runtime.py --root "$HOME/.northstar"
```

The command refuses non-Mac-mini hosts, repository paths, symlinks, unsafe permissions, and an
existing invalid runtime. It creates four independent random 32-byte encryption/index keys and a
separate session key, but never overwrites an existing `secrets.env`.

Copy these exact entries from the final file into the password manager without using a shell command
that prints them:

- `NORTHSTAR_DATABASE_KEY`
- `NORTHSTAR_DATA_KEY`
- `NORTHSTAR_BACKUP_KEY`
- `NORTHSTAR_BLIND_INDEX_KEY`
- `NORTHSTAR_SESSION_KEY`
- `NORTHSTAR_KEY_VERSION`

Then reconstruct only those entries from the password manager into a new owner-only external file,
using a local editor with history/backup/synchronization disabled for that file. Do not copy from the
runtime file. Run the checkpoint; the command always deletes the reconstructed regular file it owns,
including on failure:

```bash
.venv/bin/python scripts/reconstructed_key_drill.py \
  --production-secrets "$HOME/.northstar/secrets.env" \
  --reconstructed-secrets "$HOME/.northstar/reconstructed-recovery.env" \
  --delete-reconstructed
```

Success proves the independently reconstructed values match and can authenticate the newest backup,
open SQLCipher, decrypt field records, and validate the current schema in an isolated temporary
directory without modifying production. It does not prove the password manager itself is available
from every disaster scenario; test its account-recovery process separately.

## 6. Install the pinned connector without exposing its token

Gate 4B pins the native macOS `cloudflared` `2026.7.3` build for the final Mac mini. The reviewed
official archive SHA-256 is
`90c5a4f914d705fd70c135dba6d80b1791d254b08d6d4136301941f88330dd09` for Apple silicon
(`darwin-arm64`) and
`70d1c8684fa6d14b5843787ec8d1ea8e18b23650e424f4ea43d849a506487c3b` for Intel
(`darwin-amd64`). The installer rejects every other operating system or architecture.
The installer uses system HTTPS with HTTPS-only redirects, bounds the archive, checks that digest
before extraction, permits exactly one `cloudflared` member, installs atomically, disables
self-update, and verifies the binary's reported version:

```bash
.venv/bin/python scripts/install_cloudflared.py
.venv/bin/python scripts/install_cloudflared.py --verify-only
```

In the Cloudflare dashboard, reveal/copy the tunnel run token only when ready to install it. In a
private local terminal on the Mac mini, create `~/.northstar/tunnel-token` as a regular `0600` file
without placing the token in command history or a command argument. Clear the clipboard immediately.
Never use `cloudflared tunnel login`; its account-wide certificate is unnecessary for a remotely
managed tunnel.

```bash
umask 077
mkdir -p "$HOME/.northstar"
read -r -s NORTHSTAR_TUNNEL_TOKEN
printf '%s\n' "$NORTHSTAR_TUNNEL_TOKEN" > "$HOME/.northstar/tunnel-token"
unset NORTHSTAR_TUNNEL_TOKEN
chmod 600 "$HOME/.northstar/tunnel-token"
```

Paste once at the silent `read` prompt and press Return. The token is not displayed; do not enable
shell tracing. Clear the clipboard before continuing.

Install the credential-free LaunchAgent and start it:

```bash
.venv/bin/python scripts/install_cloudflared_launchd.py \
  --cloudflared "$HOME/.local/bin/cloudflared-2026.7.3" \
  --token-file "$HOME/.northstar/tunnel-token"
launchctl bootstrap "gui/$(id -u)" \
  "$HOME/Library/LaunchAgents/com.northstar.local.tunnel.plist"
launchctl print "gui/$(id -u)/com.northstar.local.tunnel"
```

The installer checks the pinned executable and token file type, ownership, size, path, and
permissions. The plist contains paths only. It passes the credential by file, uses warning-level
logs, has no output file, environment secret, self-update, or debug/request logging, and applies
resource limits. The run token can operate this tunnel; treat it as a secret even though it cannot
administer the Cloudflare account.

## 7. Mandatory live verification

Complete every item from a separate network/device as well as locally. Record pass/fail and UTC time,
never tokens, emails, issuer, AUD, cookies, paths, account data, or full headers.

- Local: the container publishes only `127.0.0.1:8010`; `/healthz` returns only healthy status; `/`
  without a JWT is 401; a forged Host/forwarded-header request does not gain access.
- Edge unauthenticated: `/`, `/healthz`, a nonexistent path, and `/integrations/schwab/callback` all
  encounter
  Access before any origin response.
- Identity: the exact allowed identity plus MFA reaches Northstar; a different identity is denied.
- Application JWT: a valid Access assertion reaches a protected page. Missing, truncated, expired,
  wrong-audience, wrong-issuer, wrong-email, wrong-algorithm, unknown-key, and invalid-signature test
  tokens fail closed locally and never disclose validation details.
- Browser: the Northstar cookie is `Secure`, `HttpOnly`, `SameSite=Lax`, host-only, and begins
  `__Host-`; no response contains a token or sensitive identifier in a URL or HTML attribute.
- Headers/cache: HTTPS replies have CSP, frame denial, nosniff, restrictive referrer/permissions
  policy, HSTS, and `Cache-Control: no-store, private, max-age=0`. Cloudflare's cache trace confirms
  the hostname bypass rule.
- Tunnel: exactly one connector is healthy, exactly one hostname route plus catch-all 404 exists, no
  private route exists, and stopping cloudflared makes the external origin unavailable while local
  loopback health remains available.
- Network: no router port is open, no process listens on a wildcard/LAN address for Northstar, DNS
  does not reveal the home IP, and DNSSEC remains valid.
- Privacy: app and cloudflared access logs are absent; warning/Access logs contain no query secrets,
  JWTs, cookies, account data, or financial values. Access logs may intentionally contain the allowed
  identity, source IP, decision, application, and timestamp.
- Recovery: production preflight, encrypted backup, isolated restore, password-manager reconstructed
  key drill, and optional encrypted-volume replication all pass.

Do not merge or deploy Gate 4B as complete if any dashboard state or live behavior is inferred rather
than observed.

## Session revocation, shutdown, and DNS rollback

- To revoke every Northstar Access application token, open **Zero Trust > Access controls >
  Applications > Northstar Local > Configure** and choose **Revoke existing tokens**. To revoke the
  one identity across the account, first disable/recover it at the identity provider, then use **Team
  & Resources > Users > Action > Revoke**. The user logout URL is
  `https://northstar.example.com/cdn-cgi/access/logout`. Repeat an incognito MFA login test after any
  revocation or identity recovery. See Cloudflare's
  [session-revocation procedure](https://developers.cloudflare.com/cloudflare-one/access-controls/access-settings/session-management/#revoke-user-sessions).
- If the identity-provider account is lost or compromised, unload cloudflared and remove the public
  route first. Recover/disable the IdP identity and its MFA credentials, revoke Cloudflare sessions,
  review IdP and Access audit events, and restore the exact-email/MFA policy before reconnecting.
- For a planned public shutdown, unload `com.northstar.local.tunnel`, confirm the connector is down,
  remove the tunnel's published application route, and remove its generated DNS record. Keep the
  Access application in default-deny state until DNS no longer resolves. Rotate/revoke the tunnel
  token, delete its local file, and delete the tunnel only after confirming no other route uses it.
  Never replace the Tunnel record with an A/AAAA record.
- DNS rollback is complete only when authoritative and external resolvers show no A, AAAA, or CNAME
  for the hostname and the home IP was never exposed. Local `127.0.0.1:8010` operation and encrypted
  backups remain independent of that rollback.
- During a Cloudflare outage, remote access fails closed. Do not add a Bypass policy, alternate DNS
  record, router port, or public origin. Use only local Mac mini maintenance until Cloudflare and JWKS
  retrieval recover.
- During a Mac mini outage, the tunnel has no healthy origin and remote access is unavailable. Do not
  redirect the hostname to the development Mac. Recover the final Mac or a reviewed replacement from
  authenticated encrypted backups and password-manager keys, then repeat the entire live gate.
- The LaunchAgent begins only after FileVault unlock and the production user session starts. A reboot
  test must cover unlock/login, Docker recovery, container health, connector health, and authenticated
  external access; pre-login remote availability is intentionally not promised.

## Trust boundary and visible metadata

Cloudflare terminates public TLS and can observe the hostname, path/query, source IP, Access identity
and MFA/session metadata, timing, sizes, and the plaintext HTTP request/response it proxies. Cloudflare
must therefore be trusted not to retain or transform authenticated portfolio pages beyond configured
Access/security logging. The tunnel is encrypted between cloudflared and Cloudflare, but the fixed
loopback hop from cloudflared to Northstar is HTTP and is protected by the host boundary and the Mac's
local process boundary.

Unauthenticated Internet clients can learn that the hostname uses Cloudflare and can observe Access
login behavior. The Mac exposes local runtime filenames, file sizes, timestamps, and backup cadence to
an attacker who defeats FileVault. Northstar decrypts database pages and requested fields in process
memory while serving them. Access/JWKS availability is an accepted dependency: outage fails closed and
temporarily prevents remote use; local maintenance and recovery remain available on the Mac mini.

## Incident actions

- **Tunnel token exposed:** rotate the token in the tunnel dashboard, replace the owner-only token
  file without printing it, restart the LaunchAgent, and confirm the old connector cannot connect.
  For suspected active misuse, remove the public route or delete/recreate the tunnel so an attacker's
  existing connector is disconnected; token rotation alone blocks new connections but does not end an
  already-connected replica. Cloudflare documents this behavior in
  [Tunnel tokens](https://developers.cloudflare.com/tunnel/advanced/tunnel-tokens/).
- **Access policy broadened or bypassed:** stop/unload cloudflared immediately, restore the exact-email
  plus MFA policy, inspect Access audit events, revoke Access sessions, and repeat live verification.
- **Issuer/AUD/email changed:** keep the tunnel stopped until the external secrets file and application
  are updated together and valid/invalid JWT tests pass. Never relax application checks.
- **Mac lost or suspected unlocked compromise:** disconnect the connector and revoke its token, revoke
  Access sessions, secure the Cloudflare/password-manager accounts, preserve encrypted off-host
  backups, and rotate all potentially exposed runtime keys through a separately reviewed recovery
  procedure. FileVault protects only a powered-off, locked Mac.
- **Recovery values unavailable:** do not overwrite the runtime or backups. Restore password-manager
  access first. There is no encryption bypass and lost keys make the corresponding data unrecoverable.
- **JWKS/Cloudflare outage:** accept remote fail-closed downtime. Do not add a bypass, public origin, or
  locally forged identity header.
