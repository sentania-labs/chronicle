# 024: scheduled backups to a local path or an S3 bucket

- **Status:** accepted
- **Date:** 2026-09-24

## Context

Backups existed on demand only (ADR 016: `chronicle backup create`,
`/admin/backup`). Issue #68 asks for a schedule, retention, and status on
`/admin`. A bundle written to the data volume does not survive losing that
volume, so it is not a backup; the target has to be somewhere else. Scott
chose to support both an S3 bucket and a local directory, picked on `/admin`
(issue #68, comment of Sept 22).

## Decision

- **Bundle:** unchanged. A scheduled run calls `backup.create_backup`, so the
  format, manifest and the `instance.key` exclusion are exactly ADR 016's.
- **Schedule:** every 6 or 12 hours, daily, or weekly on Sunday, from a time
  of day in America/Chicago wall time, so a daily 03:00 stays 03:00 across
  DST. A background thread in the api (`scheduled_backup.run_loop`, ADR 013's
  pattern) checks once a minute; a run missed while the api was down happens
  once at the next check, not once per missed slot. `/admin/backup` also has
  "Run a backup now" and "Test target", which writes and deletes a probe.
- **Local target:** an absolute directory, refused when it is or sits under
  the data directory. What backs it (a separate volume, NFS, an S3-backed
  mount) is a deployment decision; `examples/k8s/pvc-backups.yaml` and
  `docker-compose.yml` mount one at `/backups`.
- **S3 target:** endpoint, bucket, optional prefix, region, access key and
  secret. `chronicle/api/s3.py` signs three calls (PUT, DELETE, ListObjectsV2)
  with SigV4 over `httpx` instead of adding an AWS SDK; tests hold the signer
  to AWS's published examples. `amazonaws.com` endpoints are addressed
  virtual-host style, anything else path style (what MinIO and NAS S3
  services expect).
- **Credentials:** `state/backup-schedule.json`, mode 0600, with the secret
  encrypted by the instance key like the GitHub App's. The field is
  write-only on `/admin`: never echoed, blank keeps the saved value. The file
  is not in `backup.STATE_MEMBERS`, so no bundle carries it, and a restore
  leaves the running instance's schedule as it was.
- **Retention:** keep the newest N (default 14). Only names matching
  `chronicle-backup-<UTC stamp>.tar.gz` are counted or deleted; anything else
  at the target is never touched.
- **Status:** `state/backup-status.json` records the last attempt, success
  (time, size, where), and failure (time, error). The `/admin` status page
  shows them and warns when the last success is more than two intervals old.

## Consequences

- One more api thread; it holds no store lock and only reads the data
  directory, the same way a manual backup does.
- After restoring onto a new instance, the schedule has to be entered again:
  it is deliberately not in the bundle.
- A bundle is uploaded to S3 in a single PUT, which S3 caps at 5 GB.
- Retention counts every `chronicle-backup-*` bundle at the target, so two
  instances must never share one directory or prefix: each would prune the
  other's bundles.
- The three schedule POSTs are same-origin only (`ui_deps.check_same_origin`)
  on top of the admin session: the target decides where a copy of the whole
  data directory goes. A restore waits for a running backup
  (`scheduled_backup.run_lock`) so no torn bundle is shipped mid-swap.
- "Test target" reaches whatever endpoint an admin enters, internal addresses
  included; it shows only the status and S3 error code, never a body, and the
  page is admin-only.
