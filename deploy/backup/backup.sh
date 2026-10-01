#!/usr/bin/env bash
# Local backup of the database and the resume storage (DATA-06).
#
# Writes a pair of files to IR_BACKUP_DIR, sharing the same UTC timestamp:
#   db-<timestamp>.dump         pg_dump custom format
#   storage-<timestamp>.tar.gz  contents of IR_STORAGE_DIR
# and removes pairs older than 30 days, matching the account deletion notice
# ("backup copies expire within 30 days"). Nothing is sent to an external service.
#
# Database connection uses the standard libpq variables (PGHOST, PGPORT, PGUSER,
# PGPASSWORD or ~/.pgpass, PGDATABASE). POSTGRES_USER / POSTGRES_PASSWORD from
# deploy/db.env are accepted as fallbacks. Credentials never go on the command line.
#
# Usage: backup.sh
set -euo pipefail

BACKUP_DIR="${IR_BACKUP_DIR:-/backups}"
STORAGE_DIR="${IR_STORAGE_DIR:-/data/storage}"
RETENTION_DAYS=30

export PGUSER="${PGUSER:-${POSTGRES_USER:-}}"
export PGPASSWORD="${PGPASSWORD:-${POSTGRES_PASSWORD:-}}"
export PGDATABASE="${PGDATABASE:-interview_reviewer}"
[ -n "$PGUSER" ] || unset PGUSER
[ -n "$PGPASSWORD" ] || unset PGPASSWORD

log() { printf '%s backup: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }
fail() { log "error: $*"; exit 1; }

command -v pg_dump >/dev/null 2>&1 || fail "pg_dump not found"
command -v tar >/dev/null 2>&1 || fail "tar not found"
[ -d "$STORAGE_DIR" ] || fail "storage directory not found: $STORAGE_DIR"

# Backups hold personal data: keep them readable by the owner only.
umask 077
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
dump_file="$BACKUP_DIR/db-$timestamp.dump"
tar_file="$BACKUP_DIR/storage-$timestamp.tar.gz"

cleanup() {
    rm -f "$dump_file.partial" "$tar_file.partial"
}
trap cleanup EXIT

log "dumping database $PGDATABASE"
pg_dump --format=custom --no-owner --file="$dump_file.partial"
mv "$dump_file.partial" "$dump_file"

log "archiving storage directory"
tar --create --gzip --file="$tar_file.partial" --directory="$STORAGE_DIR" .
mv "$tar_file.partial" "$tar_file"

log "created $(basename "$dump_file") and $(basename "$tar_file")"

# Retention: delete copies older than 30 days.
find "$BACKUP_DIR" -maxdepth 1 -type f \
    \( -name 'db-*.dump' -o -name 'storage-*.tar.gz' \) \
    -mtime +"$RETENTION_DAYS" -print -delete |
    while IFS= read -r removed; do
        log "removed expired $(basename "$removed")"
    done

log "done"
