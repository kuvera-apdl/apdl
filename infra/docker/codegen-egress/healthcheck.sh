#!/bin/sh
set -eu

socket_path=/run/apdl-codegen-egress/proxy.sock
test -S "$socket_path"

printf 'GET http://169.254.169.254/latest/meta-data/ HTTP/1.1\r\nHost: 169.254.169.254\r\nConnection: close\r\n\r\n' \
  | socat -T 2 - "UNIX-CONNECT:${socket_path}" \
  | sed -n '1p' \
  | grep -Eq '^HTTP/1\.[01] 403'
