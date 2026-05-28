#!/usr/bin/env bash
set -euo pipefail

mkdir -p /run/dbus /run/avahi-daemon

if [[ ! -S /run/dbus/system_bus_socket ]]; then
  dbus-daemon --system --fork
fi

if [[ ! -S /run/avahi-daemon/socket ]]; then
  avahi-daemon --daemonize --no-drop-root
fi

if [[ $# -eq 0 || "${1:0:1}" == "-" ]]; then
  set -- python3 -m ai_server.server "$@"
fi

exec "$@"
