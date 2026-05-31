#!/usr/bin/env bash
set -euo pipefail

mkdir -p /run/dbus /run/avahi-daemon

if [[ ! -S /run/dbus/system_bus_socket ]]; then
  dbus-daemon --system --fork
fi

for _ in {1..50}; do
  if dbus-send --system --dest=org.freedesktop.DBus --type=method_call \
    / org.freedesktop.DBus.ListNames >/dev/null 2>&1; then
    break
  fi
  sleep 0.1
done

if [[ ! -S /run/avahi-daemon/socket ]]; then
  avahi-daemon --daemonize --no-drop-root
fi

if [[ $# -eq 0 || "${1:0:1}" == "-" ]]; then
  set -- python3 -m ai_server.server "$@"
fi

exec "$@"
