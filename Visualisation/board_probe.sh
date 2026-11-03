#!/usr/bin/env bash
set -u

HOST="${1:-192.168.31.153}"
USER_NAME="${2:-}"

if [[ -z "$USER_NAME" ]]; then
  echo "用法: $0 <板端IP> <SSH用户名>"
  echo "示例: $0 192.168.31.153 root"
  exit 2
fi

ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new "$USER_NAME@$HOST" 'bash -s' <<'REMOTE'
set -u
printf '%s\n' '--- identity ---'
hostname || true
id || true
printf '%s\n' '--- os ---'
uname -a || true
uname -m || true
cat /etc/os-release 2>/dev/null || true
printf '%s\n' '--- resources ---'
free -h 2>/dev/null || true
df -h / 2>/dev/null || true
printf '%s\n' '--- graphics ---'
printf 'DISPLAY=%s\n' "${DISPLAY-}"
printf 'WAYLAND_DISPLAY=%s\n' "${WAYLAND_DISPLAY-}"
ps -ef 2>/dev/null | grep -E '[X]org|[X]wayland|[w]eston|[l]abwc|[o]penbox|[s]way' || true
printf '%s\n' '--- remote-control candidates ---'
for command in vncserver x11vnc wayvnc weston-info wayland-info weston-screenshooter scrcpy adb gst-launch-1.0; do
  if command -v "$command" >/dev/null 2>&1; then
    printf '%s: %s\n' "$command" "$(command -v "$command")"
  fi
done
printf '%s\n' '--- listening ports ---'
if command -v ss >/dev/null 2>&1; then ss -lntup 2>/dev/null || true; elif command -v netstat >/dev/null 2>&1; then netstat -lntup 2>/dev/null || true; fi
REMOTE
