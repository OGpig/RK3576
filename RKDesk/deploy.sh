#!/usr/bin/env bash
# Deploy RKDesk to the board over SSH (tar stream avoids Windows path quirks).
set -euo pipefail

HOST="${1:-root@192.168.31.153}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "[deploy] target: $HOST"
echo "[deploy] source: $ROOT"

ssh "$HOST" "mkdir -p /opt/rkdesk /var/log"

tar -C "$ROOT/board" -cf - rkdesk rkdesk_server.py \
    | ssh "$HOST" "tar -xf - -C /opt/rkdesk"

tar -C "$ROOT" -cf - web \
    | ssh "$HOST" "tar -xf - -C /opt/rkdesk && chmod -R a+rX /opt/rkdesk"

tar -C "$ROOT" -cf - deploy/S99rkdesk \
    | ssh "$HOST" "tar -xf - -C /tmp && mv /tmp/deploy/S99rkdesk /etc/init.d/S99rkdesk && chmod +x /etc/init.d/S99rkdesk"

ssh "$HOST" "/etc/init.d/S99rkdesk restart"

echo -n "[deploy] waiting for health"
for _ in $(seq 1 20); do
    if ssh "$HOST" "wget -q -T 2 -O /dev/null http://127.0.0.1:8088/healthz" 2>/dev/null; then
        echo " OK"
        break
    fi
    echo -n "."
    sleep 0.5
done

ssh "$HOST" "wget -q -T 3 -O - http://127.0.0.1:8088/healthz; echo"
