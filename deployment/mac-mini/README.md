# Mac mini operational deployment

Gate 4A prepares a continuously running, loopback-only Mac mini deployment. Gate 4B adds the separate
[Cloudflare Access and Tunnel procedure](CLOUDFLARE.md). Phase 6 adds integrated production onboarding
from a fresh encrypted database after the Phase 5 read-only Schwab, Bitcoin pricing, and account
lifecycle implementation passes. This runbook does not place provider credentials in the repository
or launchd configuration.

Before any personal data is entered, record only pass/fail for: clean approved checkout; preflight and
privacy-safe status; independent recovery-key reconstruction; new encrypted backup plus isolated
restore; authenticated secondary recovery point when replication is enabled; reboot recovery of the
app, jobs, and Tunnel; post-reboot preflight; and exact-identity Cloudflare Access with MFA from a
second device while the origin remains loopback-only. Do not put paths, secrets, identifiers, headers,
or financial values in GitHub.

Do not continue personal-data onboarding in the rehearsal database. After Phase 5 is deployed,
preserve a final encrypted rehearsal backup, initialize a fresh production database, and use the
authenticated UI in this order: connect/map eligible Schwab accounts → remaining manual cash and
Bitcoin → account targets → envelopes → household policy → Plan. Create and isolated-restore a new
encrypted backup after onboarding. The hosted Northstar remains authoritative through reconciliation
and the Phase 7 parallel comparison.

## Host boundary

1. Use a reviewed, pinned Git commit. Never make a production job pull or execute an unreviewed branch.
2. Enable FileVault and store its recovery key away from the Mac.
3. Keep the runtime, backups, operational status, replication destination, and `secrets.env` outside
   the repository. Runtime/backup/operations/replication directories are `0700`; secrets, database
   artifacts, status, locks, and backups are `0600` (or stricter while owner-readable where required).
4. Put host paths in the external `secrets.env`. Compose overrides database, backup, and secret paths
   with their container mount points; the same file remains usable by host-side maintenance commands.
5. For Gate 4B, configure `NORTHSTAR_REPLICATION_DIR` only as
   `/Volumes/NorthstarRecovery/northstar-backups`, and only while the owner-only destination is on a
   mounted encrypted volume. An absent or locked volume disables replication; it never changes the
   destination. Provider credentials do not belong in Northstar or its launchd files.

## Initial constrained container start

Before a real deployment, complete the recovery-key checklist in [RECOVERY.md](RECOVERY.md). From a
clean checkout at the approved commit, use Docker Compose 2.30.0 or newer. The minimum is required
for the Compose [`env_file.format: raw`](https://docs.docker.com/reference/compose-file/services/#format)
protection. Verify the version and fully resolve the
configuration before any build or restart:

```bash
export NORTHSTAR_SECRET_FILE="$HOME/.northstar/secrets.env"
export NORTHSTAR_DATA_DIR="$HOME/.northstar/data"
export NORTHSTAR_BACKUP_DIR="$HOME/.northstar/backups"
export NORTHSTAR_BUILD_COMMIT="$(git rev-parse HEAD)"
docker compose version --short
docker compose config --quiet
docker compose build --pull=false app
docker compose up --detach --no-build app
.venv/bin/python scripts/production_preflight.py --secrets-file "$NORTHSTAR_SECRET_FILE"
```

The reported Compose version must be at least `2.30.0`, and `docker compose config --quiet` must
exit successfully. Production preflight repeats both checks and fails closed before an update can
create a backup, build, restart, or use the Compose-based rollback path.

The image uses a digest-pinned Python base, a complete version-pinned production dependency
constraint file, the reviewed source commit as an immutable OCI image label, and UID/GID
`10001:10001`. The guarded updater supplies the label automatically. Compose uses an explicit
`127.0.0.1` publication, read-only root filesystem, only the data/backup/secret mounts, a bounded
temporary filesystem, all capabilities dropped, `no-new-privileges`, PID/CPU/memory limits,
rotated 10 MiB × 3 JSON logs, a 30-second graceful stop, `unless-stopped`, and a status-only health
check. Do not change the host binding to a LAN, wildcard, or IPv6 wildcard address.

The full preflight is intentionally post-start because it verifies the running container identity,
health, hardening, and actual port publication. A failed check blocks scheduling and updates.

## Controlled Schwab OAuth verification

Do this only after the callback slice is reviewed and merged, the Schwab app callback is exactly
`https://northstar.example.com/integrations/schwab/callback`, and the existing Cloudflare Access
application protects that path with the same approved identity and MFA policy as the rest of Northstar.
Do not create an Access bypass for the callback.

Before entering credentials or attempting live authorization, sign in to the Schwab Developer
Portal and confirm the app's PUT/POST/DELETE order-request limit is exactly `0` requests per account
per minute. Record only pass/fail. The setup command requires typing `ZERO` for this prerequisite
before it opens either hidden credential prompt; the enabled production configuration is invalid
without the resulting external attestation.

Configure the external owner-only secrets file through hidden prompts; never pass either provider
credential as a command-line argument or place it in shell history:

```bash
.venv/bin/python scripts/configure_schwab_runtime.py \
  --secrets-file "$NORTHSTAR_SECRET_FILE"
.venv/bin/python scripts/update_production.py --secrets-file "$NORTHSTAR_SECRET_FILE"
```

After the update and preflight pass, discard any pending attempt by signing in through Cloudflare
Access, opening **Settings**, and choosing **Connect Schwab** again. On Northstar's no-store
confirmation page, choose **Continue to Schwab**; **Cancel and return to Settings** does not leave
Northstar, and starting again invalidates that unfinished attempt. A successful return to the clean
`schwab_connected` settings state proves that Schwab returned the exact one-time state and that the
encrypted token exchange completed. Record only pass/fail—never copy authorization or callback URLs,
query parameters, codes, state, tokens, app identifiers, or account details into logs, GitHub, or
documentation. Stop after this result; account discovery remains disabled.

Treat any Schwab provider-app configuration change as invalidating the prior throttle check. Disable
the integration and deploy that state before changing the provider app; afterward, confirm the zero
limit again and rerun the setup command before restoring credentials or attempting authorization.

If the callback fails, stop there and leave account discovery disabled. To remove the provider
credentials from the external file and disable the integration before another reviewed deployment:

```bash
.venv/bin/python scripts/configure_schwab_runtime.py \
  --secrets-file "$NORTHSTAR_SECRET_FILE" --disable
.venv/bin/python scripts/update_production.py --secrets-file "$NORTHSTAR_SECRET_FILE"
```

## Daily backups and monthly recovery

Install credential-free launchd schedules after preflight passes:

```bash
.venv/bin/python scripts/install_launchd.py \
  --python "$PWD/.venv/bin/python" \
  --secrets-file "$NORTHSTAR_SECRET_FILE"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.northstar.local.backup.plist"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.northstar.local.recovery.plist"
```

- Daily at 03:15 local time, `scheduled_backup.py` takes an exclusive lock, creates a consistent
  SQLCipher snapshot, AES-GCM-envelopes it, publishes it atomically, runs an isolated restore, then
  applies the configured daily/monthly retention. Failure before validation never invokes retention.
- If `NORTHSTAR_REPLICATION_DIR` is configured, the same locked daily job authenticates and atomically
  replicates every missing finalized `.nsbak`, then verifies size and SHA-256. It never prunes remote
  recovery points.
- On day 1 at 04:15 local time, the monthly job copies the newest finalized backup into an isolated
  `0700` environment, restores through the normal path, authenticates SQLCipher and all AES-GCM
  fields, checks current schema and foreign keys, and removes the isolated environment.

launchd plists contain paths only—no environment values, keys, credentials, or provider setup. Job
output is one privacy-safe status object; macOS manages launchd diagnostics. Inspect schedule state
with `launchctl print gui/$(id -u)/com.northstar.local.backup` without printing the secret file.

## Secondary replication commands

Test a configured destination without writing, copy, then verify independently:

```bash
.venv/bin/python scripts/replicate_backups.py --secrets-file "$NORTHSTAR_SECRET_FILE" --dry-run
.venv/bin/python scripts/replicate_backups.py --secrets-file "$NORTHSTAR_SECRET_FILE"
.venv/bin/python scripts/replicate_backups.py --secrets-file "$NORTHSTAR_SECRET_FILE" --verify-only
```

The destination must already exist and be owner-only. Symlinks, repository/runtime destinations,
unsafe files, unauthenticated source envelopes, partial copies, and mismatched sizes/hashes fail
closed. Only canonical `northstar_TIMESTAMP.nsbak` files are considered. No database, sidecar,
restore file, CSV, preview, secret, log, or arbitrary directory content is copied.

## Privacy-safe operational status

```bash
.venv/bin/python scripts/operational_status.py --secrets-file "$NORTHSTAR_SECRET_FILE"
```

The JSON contains health states, last successful backup/drill/replication times, backup count,
disk warning, preflight result, and one allow-listed failure code. It contains no filenames, paths,
values, account/security/provider details, keys, or exception text.

## Controlled update and rollback

Review and check out the desired pinned commit manually. Do not add an automatic pull step. With the
existing container healthy and repository clean:

```bash
.venv/bin/python scripts/update_production.py --secrets-file "$NORTHSTAR_SECRET_FILE"
```

The command reads the source schema without migrating it, creates and drills an encrypted pre-update
backup against that exact schema (and replicates it when configured), preserves the current image,
builds with `--pull=false`, restarts, waits for health, and repeats preflight. After a successful
startup it detects the target schema and creates and drills a second backup against that schema. The
updater derives all required Compose host paths from the validated runtime and secrets-file argument;
it does not depend on those variables having been exported in the invoking shell. The same derived
environment is used for build, restart, stop, and image rollback commands.

If the database is still at the source schema, a build/start/health failure restores the previous
image. If startup advanced the schema, the updater records
`production_update_migration_requires_recovery`, does not start the incompatible image, and leaves the
database/WAL/SHM set and pre-update backup intact for explicit recovery. It never rolls the database
backward automatically. The application is stopped before either migration-recovery or schema-unknown
failure returns. Updater output and operational state use only fixed, privacy-safe codes; operational
status includes the pending source schema version needed for explicit recovery.

If migration validation fails, keep the app stopped, preserve the database/WAL/SHM set, verify the
pre-update backup in isolation, and follow [RECOVERY.md](RECOVERY.md). Restore the database only by an
explicit operator decision; then start the preserved image and repeat preflight. Never run an older
image against a schema it does not support.
