# Northstar encrypted recovery runbook

## Mandatory recovery-key checklist

Complete this manually before real financial data exists and after every key change:

- [ ] FileVault is enabled, and its recovery key is stored independently of the Mac mini.
- [ ] Independent password-manager copies exist for `NORTHSTAR_DATABASE_KEY`,
  `NORTHSTAR_DATA_KEY`, `NORTHSTAR_BACKUP_KEY`, and `NORTHSTAR_BLIND_INDEX_KEY`.
- [ ] The session/application key and current key-version metadata are also recoverable.
- [ ] Recovery keys are not stored beside `.nsbak` files, in the source repository, or solely on the
  Mac being protected.
- [ ] A temporary owner-only environment built from the password-manager copies can authenticate the
  newest replicated backup, restore SQLCipher, and authenticate all field-encrypted records.
- [ ] The temporary recovery environment and copied secret file were removed after the test.

Northstar can validate keys supplied to a drill, but it cannot inspect a password manager or prove
that independent copies exist. This checklist and periodic manual recovery using those copies remain
operator responsibilities.

## Required controls

- Keep `secrets.env` owner-readable only (`0600` or stricter), runtime directories at `0700`, and
  database/sidecar/restore/backup files at `0600`.
- Never copy a database, backup, secret, statement, CSV, log, or drill output into the repository.
- Keep the database key and backup-envelope key: a `.nsbak` contains an AES-GCM envelope around a
  still-SQLCipher-encrypted database, so successful recovery requires both keys plus the data and
  blind-index keys for full field verification.

## Verify without modifying production

Stop if any required key is unavailable. Load the external environment without printing it and run:

```bash
.venv/bin/python scripts/monthly_recovery_drill.py \
  --secrets-file "$HOME/.northstar/secrets.env"
```

The drill copies the newest finalized backup into an isolated owner-only directory, decrypts the
outer envelope in memory, writes only its still-SQLCipher-encrypted image, authenticates SQLCipher
and every AES-GCM record, checks schema/foreign-key integrity, and removes the temporary environment.
It neither replaces nor opens the production database for writing.

## Replace a damaged runtime database

### Failed update after a schema migration

When the updater reports `production_update_migration_requires_recovery`, do not start the preserved
old image. The updater stops the application first. Keep it stopped and preserve the database, WAL,
and SHM files as one set. Do not retry with manual container commands. Read
`pending_recovery_schema_version` from privacy-safe operational status, select the pre-update backup,
and verify it explicitly without modifying production:

```bash
.venv/bin/python scripts/verify_backup_restore.py \
  --backup "$HOME/.northstar/backups/northstar_TIMESTAMP.nsbak" \
  --expected-schema-version SOURCE_SCHEMA
```

Diagnose the new release with synthetic data and choose explicitly between repairing the migrated
runtime or restoring the complete pre-update recovery point. If restoring, pass the same
`--expected-schema-version SOURCE_SCHEMA` option to `scripts/restore_backup.py`. Only start an image
after confirming that it supports the selected database schema, then run preflight and create a new
validated backup.

1. Stop Northstar gracefully and confirm no process is using the database.
2. Move the current `northstar.db`, `northstar.db-wal`, and `northstar.db-shm`, when present, together
   into an owner-only quarantine directory outside the repository. Do not delete them yet.
3. Run the isolated drill against the selected encrypted backup.
4. Restore only after the configured destination has no WAL or SHM sidecars:

```bash
.venv/bin/python scripts/restore_backup.py \
  --backup "$HOME/.northstar/backups/northstar_TIMESTAMP.nsbak" \
  --destination "$HOME/.northstar/data/northstar.db" \
  --replace-production
```

5. Start the application on loopback, verify `/healthz`, run production preflight, and perform another
   isolated drill.
6. Retain the quarantined database set until application behavior is independently verified.

Restore validates a new owner-only `.restore` file before atomically replacing the destination.
Unexpected artifact types and existing sidecars stop recovery without touching the valid database.
A stale regular file or symlink with the exact `.restore` name is removed without following it.

## Key exposure and loss response

Never respond by editing only `secrets.env`; current ciphertext must remain decryptable during a
controlled offline migration.

### Exposed session/application key

Stop the app, create and verify a backup, replace the session key with a new independent value,
restart, and repeat preflight. Existing browser sessions become invalid. The session key does not
decrypt stored financial data.

### Exposed database or field-encryption key

Stop the app, prevent further copies, preserve the complete database set and recovery keys, and assess
exposure. SQLCipher rekey and AES-GCM field re-encryption are not automated in Gate 4A. Build and test
an explicit offline migration using synthetic copies before touching production; keep old keys in the
password manager until every database and retained backup has been migrated and drilled. Merely
changing either key makes existing data unreadable.

### Lost database key

The live SQLCipher database and the SQLCipher image inside every backup are unrecoverable without an
independent copy of this key. Do not overwrite the runtime. Search only approved recovery-key stores;
if no copy exists, there is no technical bypass. Rebuild a new empty runtime and retain the unreadable
artifacts in quarantine in case the key is recovered later.

### Lost or exposed backup-envelope key

Without an independent copy, existing `.nsbak` envelopes cannot be opened even if the live database
key is available. If the live database remains accessible, stop changes, replace the exposed/lost
backup key, create new encrypted backups, drill them, and replicate them before retiring old recovery
points. Retain the old key separately for any old backups that must remain recoverable.

### Lost blind-index key

Exact-match indexes and privacy-safe deduplication hashes cannot be regenerated safely by the current
release. Field ciphertext may remain decryptable with the data key, but lookups and import identity
checks are not trustworthy. Restore the independent key copy. An offline decrypt-and-reindex migration
must be built and tested before choosing a new blind-index key.

## Rebuild a synthetic runtime after a failed drill

Do not experiment on production. Create a fresh temporary synthetic runtime outside the repository,
seed only invented records, create a backup, run the monthly drill, and test replication:

```bash
python3 scripts/create_runtime.py --root /private/tmp/northstar-recovery-synthetic
```

Use separate synthetic keys and an owner-only synthetic destination. Diagnose fixed error codes and
tests, never decrypted production values or copied statements. Remove the temporary runtime after the
failure path is understood. A repeated production drill failure remains a release/deployment blocker.
