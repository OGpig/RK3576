#!/usr/bin/env bash
# Verify that the board-side scripts match this repository.
set -u

HOST="${1:-root@192.168.31.153}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

declare -A REMOTE_HASH
declare -A LOCAL_PATH

check_pair() {
    LOCAL_PATH["$2"]="$1"
}

check_pair "$ROOT/deploy/weston" "/usr/bin/weston"
check_pair "$ROOT/deploy/S48rkdesk-input" "/etc/init.d/S48rkdesk-input"
check_pair "$ROOT/deploy/S49weston" "/etc/init.d/S49weston"
check_pair "$ROOT/deploy/S99rkdesk" "/etc/init.d/S99rkdesk"
check_pair "$ROOT/deploy/99-rkdesk-rotate.ini" "/etc/xdg/weston/weston.ini.d/99-rkdesk-rotate.ini"
check_pair "$ROOT/board/rkdesk_server.py" "/opt/rkdesk/rkdesk_server.py"
check_pair "$ROOT/board/rkdesk_input.py" "/opt/rkdesk/rkdesk_input.py"

for file in "$ROOT"/board/rkdesk/*.py; do
    check_pair "$file" "/opt/rkdesk/rkdesk/$(basename "$file")"
done
for file in "$ROOT"/web/*; do
    check_pair "$file" "/opt/rkdesk/web/$(basename "$file")"
done

remote_paths=("${!LOCAL_PATH[@]}")
echo "[verify] target: $HOST"

# One SSH round-trip for all hashes.
while read -r hash path; do
    [ -n "${hash:-}" ] && [ -n "${path:-}" ] && REMOTE_HASH["$path"]="$hash"
done < <(ssh "$HOST" "md5sum ${remote_paths[*]}" 2>/dev/null)

bad=0
total=0
for remote in "${remote_paths[@]}"; do
    total=$((total + 1))
    local_file="${LOCAL_PATH[$remote]}"
    local_hash="$(md5sum "$local_file" | awk '{print $1}')"
    remote_hash="${REMOTE_HASH[$remote]:-missing}"
    if [ "$local_hash" = "$remote_hash" ]; then
        printf 'OK   %s\n' "$remote"
    else
        printf 'DIFF %s  (local %s, remote %s)\n' "$remote" "${local_hash:0:8}" "${remote_hash:0:8}"
        bad=$((bad + 1))
    fi
done

echo "[verify] $((total - bad))/$total match"
[ "$bad" -eq 0 ]
