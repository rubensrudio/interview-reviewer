#!/usr/bin/env bash
# Restore a database dump and storage archive created by backup.sh (DATA-06).
#
# Destructive: database objects are dropped and recreated from the dump, and the
# current contents of IR_STORAGE_DIR are replaced by the archive. Stop api and
# worker before running it.
#
# Database connection uses the same variables as backup.sh (PGHOST, PGPORT,
# PGUSER, PGPASSWORD or ~/.pgpass, PGDATABASE; POSTGRES_USER / POSTGRES_PASSWORD
# as fallbacks).
#
# Usage: restore.sh --yes <db-TIMESTAMP.dump> <storage-TIMESTAMP.tar.gz>
set -euo pipefail

STORAGE_DIR="${IR_STORAGE_DIR:-/data/storage}"

export PGUSER="${PGUSER:-${POSTGRES_USER:-}}"
export PGPASSWORD="${PGPASSWORD:-${POSTGRES_PASSWORD:-}}"
export PGDATABASE="${PGDATABASE:-interview_reviewer}"
[ -n "$PGUSER" ] || unset PGUSER
[ -n "$PGPASSWORD" ] || unset PGPASSWORD

log() { printf '%s restore: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }
fail() { log "error: $*"; exit 1; }
usage() {
    printf 'Usage: %s --yes <db-TIMESTAMP.dump> <storage-TIMESTAMP.tar.gz>\n' "$(basename "$0")" >&2
    exit 2
}

[ "$#" -eq 3 ] || usage
[ "$1" = "--yes" ] || usage
dump_file="$2"
tar_file="$3"

[ -f "$dump_file" ] || fail "dump file not found: $dump_file"
[ -f "$tar_file" ] || fail "storage archive not found: $tar_file"

dump_name="$(basename "$dump_file")"
tar_name="$(basename "$tar_file")"
[[ "$dump_name" =~ ^db-([0-9]{8}T[0-9]{6}Z)\.dump$ ]] || fail "unexpected dump file name: $dump_name"
dump_ts="${BASH_REMATCH[1]}"
[[ "$tar_name" =~ ^storage-([0-9]{8}T[0-9]{6}Z)\.tar\.gz$ ]] || fail "unexpected archive name: $tar_name"
tar_ts="${BASH_REMATCH[1]}"
[ "$dump_ts" = "$tar_ts" ] || fail "dump ($dump_ts) and archive ($tar_ts) are not from the same backup"

command -v pg_restore >/dev/null 2>&1 || fail "pg_restore not found"
command -v tar >/dev/null 2>&1 || fail "tar not found"

# Validate both files before touching anything.
pg_restore --list "$dump_file" >/dev/null || fail "dump file is not a valid pg_dump archive"
tar --list --gzip --file="$tar_file" >/dev/null || fail "storage archive is not a valid tar.gz"

log "restoring database $PGDATABASE from $dump_name"
pg_restore --clean --if-exists --no-owner --single-transaction --exit-on-error \
    --dbname="$PGDATABASE" "$dump_file"

log "restoring storage directory from $tar_name"
umask 077
mkdir -p "$STORAGE_DIR"
chmod 700 "$STORAGE_DIR"
find "$STORAGE_DIR" -mindepth 1 -delete
tar --extract --gzip --file="$tar_file" --directory="$STORAGE_DIR" \
    --no-same-owner --no-overwrite-dir

log "done"
