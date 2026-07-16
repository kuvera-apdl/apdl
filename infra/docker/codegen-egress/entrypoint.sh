#!/bin/sh
set -eu

socket_path=/run/apdl-codegen-egress/proxy.sock
rm -f "$socket_path"

/usr/sbin/squid -N -f /etc/squid/squid.conf &
squid_pid=$!
socat_pid=

cleanup() {
  if [ -n "$socat_pid" ]; then
    kill "$socat_pid" 2>/dev/null || true
  fi
  kill "$squid_pid" 2>/dev/null || true
  wait "$socat_pid" 2>/dev/null || true
  wait "$squid_pid" 2>/dev/null || true
  rm -f "$socket_path"
}
trap cleanup EXIT INT TERM

attempt=0
while [ "$(
  curl --silent --output /dev/null --write-out '%{http_code}' --max-time 1 \
    --proxy http://127.0.0.1:3128 \
    http://169.254.169.254/latest/meta-data/ 2>/dev/null || true
)" != "403" ]; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 50 ]; then
    echo "Squid did not become ready" >&2
    exit 1
  fi
  sleep 0.1
done

socat \
  "UNIX-LISTEN:${socket_path},fork,mode=0666" \
  TCP:127.0.0.1:3128 &
socat_pid=$!

wait "$squid_pid"
