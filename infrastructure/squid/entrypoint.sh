#!/bin/sh
set -eu
# Immutable per-process input makes status distinguish saved from running policy.
# The host mounts a directory so atomic replacements are visible on recreation.
cp /etc/squid/log-policy/github-log-hosts.txt /run/github-log-hosts.txt
exec "$@"
