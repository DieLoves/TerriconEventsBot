#!/usr/bin/env bash

set -Eeuo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
readonly COMPOSE_FILE="${COMPOSE_FILE:-$PROJECT_ROOT/compose.yaml}"
readonly BACKUP_DIR="${BACKUP_DIR:-$PROJECT_ROOT/backups}"
readonly DB_SERVICE="${DB_SERVICE:-db}"
readonly POSTGRES_USER="${POSTGRES_USER:-terricon}"
readonly POSTGRES_DB="${POSTGRES_DB:-terricon}"
readonly BACKUP_KEEP=7

mkdir -p -- "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

readonly timestamp="$(date -u +%Y%m%dT%H%M%S%NZ)"
readonly backup_path="$BACKUP_DIR/terricon-$timestamp-$$.dump"
temporary_path="$(mktemp "$BACKUP_DIR/.terricon-backup.XXXXXX.partial")"

cleanup() {
    if [[ -n "${temporary_path:-}" && -f "$temporary_path" ]]; then
        rm -f -- "$temporary_path"
    fi
}
trap cleanup EXIT

docker compose \
    --project-directory "$PROJECT_ROOT" \
    --file "$COMPOSE_FILE" \
    exec -T "$DB_SERVICE" \
    pg_dump \
    --username "$POSTGRES_USER" \
    --dbname "$POSTGRES_DB" \
    --format=custom \
    --no-owner \
    --no-privileges >"$temporary_path"

if [[ ! -s "$temporary_path" ]]; then
    printf 'Backup failed: pg_dump produced an empty archive.\n' >&2
    exit 1
fi

chmod 600 "$temporary_path"
mv -- "$temporary_path" "$backup_path"
temporary_path=""

mapfile -d '' -t backups < <(
    find "$BACKUP_DIR" -maxdepth 1 -type f -name 'terricon-*.dump' -print0 | sort -z
)
if (( ${#backups[@]} > BACKUP_KEEP )); then
    excess=$(( ${#backups[@]} - BACKUP_KEEP ))
    for (( index = 0; index < excess; index++ )); do
        rm -f -- "${backups[index]}"
    done
fi

printf '%s\n' "$backup_path"
