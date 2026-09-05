#!/bin/sh
# Container entrypoint. Three jobs, in order: get a database, keep it durable, serve it.
#
# Both of the first two are conditional, and that is the point — the same image runs under
# `docker run` with nothing configured, on a VM with a mounted disk, and on Cloud Run with
# object-storage replication, with no separate Dockerfile per environment.

set -eu

DB="${PRECEDENT_DB_PATH:-/data/precedent.db}"
PORT="${PORT:-8080}"

serve="uvicorn precedent.api.main:app --host 0.0.0.0 --port ${PORT} --workers 1 --proxy-headers --forwarded-allow-ips=*"

# --- durability ------------------------------------------------------------------------
# With LITESTREAM_REPLICA_URL set (e.g. gcs://bucket/precedent, s3://bucket/precedent),
# restore whatever the last instance replicated before serving. `-if-db-not-exists` makes
# this a no-op when a real volume already holds the file, so a disk-backed deployment and a
# replicated one can use the same line.
if [ -n "${LITESTREAM_REPLICA_URL:-}" ]; then
  echo "entrypoint: restoring ${DB} from ${LITESTREAM_REPLICA_URL} if a replica exists"
  litestream restore -if-replica-exists -if-db-not-exists -o "${DB}" "${LITESTREAM_REPLICA_URL}"
fi

# --- first boot ------------------------------------------------------------------------
# Only ever on a genuinely absent database. `seed_demo.py` deletes and rebuilds the file it
# is pointed at, so guarding on existence here is what stops a restart from erasing the
# reviewer decisions that are the whole point of the approval screen.
if [ ! -f "${DB}" ] && [ "${PRECEDENT_SEED_ON_EMPTY:-0}" = "1" ]; then
  echo "entrypoint: no database at ${DB}; seeding the demo corpus"
  python /app/scripts/seed_demo.py
fi

# --- serve ---------------------------------------------------------------------------
# Under litestream the server runs as its child: litestream owns the process, so a crash or
# a SIGTERM from the platform flushes the final WAL frames before anything exits. Replication
# is continuous, so the worst case on an ungraceful kill is the last second of writes.
if [ -n "${LITESTREAM_REPLICA_URL:-}" ]; then
  echo "entrypoint: replicating ${DB} -> ${LITESTREAM_REPLICA_URL}"
  exec litestream replicate -exec "${serve}" "${DB}" "${LITESTREAM_REPLICA_URL}"
fi

echo "entrypoint: serving ${DB} with no replication"
exec ${serve}
