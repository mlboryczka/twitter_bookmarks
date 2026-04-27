#!/usr/bin/env bash
# Bootstrap script for Ubuntu 24.04 VPS.
# Idempotent: re-running is safe.

set -euo pipefail

APP_USER="app"
APP_DIR="/opt/twitter_bookmarks"
DB_NAME="twitter_bookmarks"
DB_USER="twitter_bookmarks"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root (sudo)." >&2
  exit 1
fi

echo "==> apt update + base packages"
apt-get update
apt-get install -y --no-install-recommends \
  ca-certificates curl gnupg lsb-release \
  build-essential pkg-config \
  python3 python3-venv \
  postgresql-16 postgresql-client-16 \
  caddy

echo "==> Install uv (system-wide)"
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | UV_INSTALL_DIR=/usr/local/bin sh
fi

echo "==> Create system user '${APP_USER}'"
if ! id -u "${APP_USER}" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "/home/${APP_USER}" --shell /usr/sbin/nologin "${APP_USER}"
fi

echo "==> Create ${APP_DIR}"
mkdir -p "${APP_DIR}"
chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}"

echo "==> Configure Postgres role + database"
DB_PASSWORD="$(openssl rand -base64 24)"
sudo -u postgres psql -v ON_ERROR_STOP=1 <<SQL
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '${DB_USER}') THEN
    CREATE ROLE ${DB_USER} WITH LOGIN PASSWORD '${DB_PASSWORD}';
  END IF;
END
\$\$;
SQL
sudo -u postgres psql -v ON_ERROR_STOP=1 -tAc \
  "SELECT 1 FROM pg_database WHERE datname = '${DB_NAME}'" | grep -q 1 \
  || sudo -u postgres createdb -O "${DB_USER}" "${DB_NAME}"

echo
echo "==> Bootstrap complete."
echo "Database created: ${DB_NAME}"
echo "Database user:    ${DB_USER}"
echo "Database password (record this and set DATABASE_URL accordingly):"
echo "  ${DB_PASSWORD}"
echo
echo "DATABASE_URL=postgresql+asyncpg://${DB_USER}:${DB_PASSWORD}@localhost:5432/${DB_NAME}"
echo
echo "Next steps:"
echo "  1. Clone the repo into ${APP_DIR}"
echo "  2. cp .env.example .env && fill in values (including DATABASE_URL above)"
echo "  3. cd ${APP_DIR} && uv sync"
echo "  4. uv run alembic upgrade head"
echo "  5. Install systemd unit: cp deploy/systemd/twitter_bookmarks.service /etc/systemd/system/"
echo "  6. systemctl daemon-reload && systemctl enable --now twitter_bookmarks"
