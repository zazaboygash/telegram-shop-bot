#!/bin/bash
# Run the gash polling bot against the dev Gash Core (token from gash-ecosystem .env)
set -a
source /root/projects/gash-ecosystem/.env
set +a
export GASH_CORE_URL="http://127.0.0.1:8100"
export PUBLIC_WEB_URL="https://dev-app.gashvpn.space"
exec /root/projects/gash-ecosystem/apps/api/.venv/bin/python -m gashbot.polling
