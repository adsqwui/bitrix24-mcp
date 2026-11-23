#!/usr/bin/env bash
# Bitrix24 MCP installer for a Linux VPS or macOS. Run it from this folder: ./install.sh
set -euo pipefail
cd "$(dirname "$0")"
DIR="$(pwd)"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

# uv installs Python 3.12 into .venv, nothing is installed system-wide
if ! command -v uv >/dev/null 2>&1; then
  export PATH="$HOME/.local/bin:$PATH"
  if ! command -v uv >/dev/null 2>&1; then
    say "Ставлю uv (менеджер Python-пакетов)..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
  fi
fi

say "Ставлю зависимости..."
[ -x .venv/bin/python ] || uv venv --python 3.12 .venv
# A regular (not editable) install: macOS hides .pth files inside ~/Documents and Python skips them
uv pip install --python .venv/bin/python --quiet --reinstall-package bitrix24-mcp .

[ -f .env ] || cp .env.example .env
chmod 600 .env
mkdir -p var

say "Настройка вебхука"
.venv/bin/python scripts/setup_env.py

TOKEN="$(grep '^MCP_BEARER_TOKEN=' .env | tail -n 1 | cut -d= -f2-)"
PORT="$(grep '^MCP_PORT=' .env | tail -n 1 | cut -d= -f2- || true)"
PORT="${PORT:-8000}"

if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
  read -r -p $'\nЗапустить HTTP-режим как службу systemd с автозапуском? [y/N] ' answer
  if [[ "${answer:-}" =~ ^[YyДд] ]]; then
    SUDO=""
    [ "$(id -u)" -eq 0 ] || SUDO="sudo"
    $SUDO tee /etc/systemd/system/bitrix24-mcp.service >/dev/null <<EOF
[Unit]
Description=Bitrix24 MCP (HTTP)
After=network-online.target
Wants=network-online.target

[Service]
User=$(id -un)
WorkingDirectory=$DIR
ExecStart=$DIR/.venv/bin/bitrix-mcp --transport http --env-file $DIR/.env
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
    $SUDO systemctl daemon-reload
    $SUDO systemctl enable --now bitrix24-mcp
    echo "Служба запущена. Логи: journalctl -u bitrix24-mcp -f"
  fi
fi

say "Готово. Как подключить:"
cat <<EOF

stdio (клиент сам запускает процесс на этом сервере):
  команда: $DIR/.venv/bin/bitrix-mcp
  аргументы: --transport stdio --env-file $DIR/.env

HTTP:
  запуск:    $DIR/.venv/bin/bitrix-mcp --transport http --env-file $DIR/.env
  адрес:     http://127.0.0.1:$PORT/mcp
  заголовок: Authorization: Bearer $TOKEN
EOF
