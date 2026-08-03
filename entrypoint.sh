#!/bin/sh
# Startup wrapper: fetch this app's secrets from Infisical, then launch it with
# those secrets injected as environment variables.
#
# Why this is two steps, not just `infisical run -- gunicorn ...`:
#   `infisical run` authenticates ONLY with an access token (INFISICAL_TOKEN).
#   It does not log in from the client id/secret on its own. So we first log in
#   with the machine identity to mint a token, then run the app under that token.
#
# Required env (injected by Dokploy — the 5 connection vars):
#   INFISICAL_API_URL, INFISICAL_PROJECT_ID, INFISICAL_ENV,
#   INFISICAL_CLIENT_ID, INFISICAL_CLIENT_SECRET
set -eu

: "${INFISICAL_API_URL:?INFISICAL_API_URL is required}"
: "${INFISICAL_PROJECT_ID:?INFISICAL_PROJECT_ID is required}"
: "${INFISICAL_ENV:?INFISICAL_ENV is required}"
: "${INFISICAL_CLIENT_ID:?INFISICAL_CLIENT_ID is required}"
: "${INFISICAL_CLIENT_SECRET:?INFISICAL_CLIENT_SECRET is required}"

# 1. Mint a short-lived access token from the machine identity.
INFISICAL_TOKEN="$(infisical login \
  --method=universal-auth \
  --client-id="$INFISICAL_CLIENT_ID" \
  --client-secret="$INFISICAL_CLIENT_SECRET" \
  --domain="$INFISICAL_API_URL" \
  --plain --silent)"
export INFISICAL_TOKEN

# 2. Pull secrets for (project, env) and exec the real app with them injected.
#    exec so gunicorn becomes PID 1 and receives signals cleanly.
#    Single worker so the in-process APScheduler runs exactly once (multiple
#    workers = multiple schedulers = duplicated jobs); threads cover concurrency.
exec infisical run \
  --projectId="$INFISICAL_PROJECT_ID" \
  --env="$INFISICAL_ENV" \
  --domain="$INFISICAL_API_URL" \
  -- gunicorn --bind 0.0.0.0:5000 --workers 1 --threads 8 app:app
