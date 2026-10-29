#!/usr/bin/env bash

set -Eeuo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
readonly BACKUP_DIR="${BACKUP_DIR:-$PROJECT_ROOT/backups}"
readonly POSTGRES_IMAGE="${POSTGRES_IMAGE:-postgres:17-alpine}"
readonly RESTORE_TMPFS_SIZE="${RESTORE_TMPFS_SIZE:-1g}"
readonly RESTORE_USER="restore_smoke"
readonly RESTORE_DB="restore_smoke"
readonly RESTORE_PASSWORD="restore-smoke-only"
readonly EXPECTED_SCHEMA_REVISION="${EXPECTED_SCHEMA_REVISION:-0013_default_subscriptions}"
readonly MIN_PUBLIC_TABLES="${MIN_PUBLIC_TABLES:-26}"

if [[ ! "$MIN_PUBLIC_TABLES" =~ ^[1-9][0-9]*$ ]]; then
    printf 'Restore smoke failed: MIN_PUBLIC_TABLES must be a positive integer.\n' >&2
    exit 1
fi

archive_path="${1:-}"
if [[ -z "$archive_path" ]]; then
    mapfile -d '' -t archives < <(
        find "$BACKUP_DIR" -maxdepth 1 -type f -name 'terricon-*.dump' -print0 | sort -z
    )
    if (( ${#archives[@]} == 0 )); then
        printf 'Restore smoke failed: no backup archive found in %s.\n' "$BACKUP_DIR" >&2
        exit 1
    fi
    archive_path="${archives[-1]}"
fi

archive_path="$(realpath -- "$archive_path")"
if [[ ! -f "$archive_path" || ! -s "$archive_path" ]]; then
    printf 'Restore smoke failed: archive is missing or empty: %s\n' "$archive_path" >&2
    exit 1
fi

container_name="terricon-restore-smoke-$(date -u +%Y%m%d%H%M%S)-$$"
cleanup() {
    docker rm --force "$container_name" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker run --detach \
    --name "$container_name" \
    --tmpfs "/var/lib/postgresql/data:rw,noexec,nosuid,size=$RESTORE_TMPFS_SIZE" \
    --env "POSTGRES_DB=$RESTORE_DB" \
    --env "POSTGRES_USER=$RESTORE_USER" \
    --env "POSTGRES_PASSWORD=$RESTORE_PASSWORD" \
    --volume "$archive_path:/backup.dump:ro" \
    "$POSTGRES_IMAGE" >/dev/null

ready=false
for _attempt in {1..30}; do
    # The entrypoint first runs a socket-only bootstrap server before POSTGRES_DB
    # exists; only the final server listens on TCP.
    if docker exec "$container_name" pg_isready --host 127.0.0.1 \
        --username "$RESTORE_USER" --dbname "$RESTORE_DB" >/dev/null 2>&1; then
        ready=true
        break
    fi
    sleep 1
done
if [[ "$ready" != true ]]; then
    printf 'Restore smoke failed: temporary PostgreSQL did not become ready.\n' >&2
    exit 1
fi

docker exec "$container_name" pg_restore \
    --exit-on-error \
    --no-owner \
    --no-privileges \
    --username "$RESTORE_USER" \
    --dbname "$RESTORE_DB" \
    /backup.dump

revision="$(
    docker exec "$container_name" psql \
        --tuples-only --no-align \
        --username "$RESTORE_USER" \
        --dbname "$RESTORE_DB" \
        --command 'SELECT version_num FROM alembic_version'
)"
table_count="$(
    docker exec "$container_name" psql \
        --tuples-only --no-align \
        --username "$RESTORE_USER" \
        --dbname "$RESTORE_DB" \
        --command "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'"
)"

if [[ "$revision" != "$EXPECTED_SCHEMA_REVISION" ]]; then
    printf 'Restore smoke failed: revision %s, expected %s.\n' \
        "${revision:-missing}" "$EXPECTED_SCHEMA_REVISION" >&2
    exit 1
fi
if [[ ! "$table_count" =~ ^[1-9][0-9]*$ ]] || (( table_count < MIN_PUBLIC_TABLES )); then
    printf 'Restore smoke failed: public tables %s, expected at least %s.\n' \
        "${table_count:-missing}" "$MIN_PUBLIC_TABLES" >&2
    exit 1
fi

printf 'Restore smoke OK: revision %s, public tables %s.\n' "$revision" "$table_count"
