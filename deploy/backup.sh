#!/bin/sh
# Database backups for the production stack. Runs inside the "backup"
# container (the postgres:16 image, so pg_dump always matches the server).
#
#   (default)  wait for 00:00 UTC (03:00 Riyadh), back up, repeat nightly
#   once       take one backup right now and exit:
#              docker compose run --rm backup once
#
# Connection details come from the standard libpq variables PGHOST, PGUSER,
# PGPASSWORD and PGDATABASE, set in docker-compose.yml.
#
# Format: pg_dump's custom format (-Fc) — compressed, and pg_restore can
# restore all of it or just one table. Restoring is in deploy/RUNBOOK.md.

set -u

KEEP_DAYS="${KEEP_DAYS:-14}"
DIR=/backups

backup() {
    file="$DIR/storefront_analytics_$(date -u +%Y%m%d_%H%M%S).dump"

    # Write to a temporary name and rename only on success, so a half-written
    # file (disk full, database down) can never pass for a good backup.
    if pg_dump -Fc -f "$file.part"; then
        mv "$file.part" "$file"
        echo "$(date -u '+%F %T') UTC  backup written: $(basename "$file") ($(du -h "$file" | cut -f1))"
    else
        rm -f "$file.part"
        echo "$(date -u '+%F %T') UTC  BACKUP FAILED — the previous backups are untouched"
        return 1
    fi

    # Retention: delete dumps older than KEEP_DAYS. Runs only after a
    # successful backup, so a run of failures never eats the last good copies.
    find "$DIR" -name 'storefront_analytics_*.dump' -mtime +"$KEEP_DAYS" -print -delete
}

mkdir -p "$DIR"

if [ "${1:-}" = "once" ]; then
    backup
    exit $?
fi

echo "Nightly backups at 00:00 UTC (03:00 Riyadh), keeping $KEEP_DAYS days."
while true; do
    # Seconds until the next 00:00 UTC. Recomputed every night, so it never
    # drifts, and a restart simply waits for the next midnight.
    now=$(date -u +%s)
    next=$(( (now / 86400 + 1) * 86400 ))
    sleep $(( next - now ))
    backup || true
done
