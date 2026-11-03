#!/usr/bin/env bash
# Deploy RKDesk to the board over SSH (tar stream avoids Windows path quirks).
set -euo pipefail

HOST="${1:-root@192.168.31.153}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "[deploy] target: $HOST"
echo "[deploy] source: $ROOT"

ssh "$HOST" "mkdir -p /opt/rkdesk /var/log"

tar -C "$ROOT/board" -cf - rkdesk rkdesk_server.py rkdesk_input.py \
    | ssh "$HOST" "tar -xf - -C /opt/rkdesk"

tar -C "$ROOT" -cf - web \
    | ssh "$HOST" "tar -xf - -C /opt/rkdesk && chmod -R a+rX /opt/rkdesk"

tar -C "$ROOT" -cf - deploy/S48rkdesk-input deploy/S49weston deploy/S99rkdesk deploy/weston deploy/99-rkdesk-rotate.ini \
    | ssh "$HOST" "
        tar -xf - -C /tmp &&
        mv /tmp/deploy/S48rkdesk-input /etc/init.d/S48rkdesk-input &&
        mv /tmp/deploy/S99rkdesk /etc/init.d/S99rkdesk &&
        mkdir -p /etc/xdg/weston/weston.ini.d &&
        install -m 644 /tmp/deploy/99-rkdesk-rotate.ini /etc/xdg/weston/weston.ini.d/99-rkdesk-rotate.ini &&
        if ! grep -q wet_main /usr/bin/weston 2>/dev/null; then
            cp -a /usr/bin/weston /usr/bin/weston.orig.\$(date +%Y%m%d-%H%M%S) 2>/dev/null || true
        fi &&
        install -m 755 /tmp/deploy/weston /usr/bin/weston &&
        install -m 755 /tmp/deploy/S49weston /etc/init.d/S49weston &&
        chmod +x /etc/init.d/S48rkdesk-input /etc/init.d/S99rkdesk
      "

ssh "$HOST" "/etc/init.d/S48rkdesk-input restart; sleep 4; /etc/init.d/S99rkdesk restart"

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
