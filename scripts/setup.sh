#!/usr/bin/env bash
set -euo pipefail

# AI Trading Assistant — bootstrap script
# Run from the repo root.

echo "==> Verifying Python (need >=3.11)"
python3 --version

echo "==> Verifying uv"
if ! command -v uv >/dev/null 2>&1; then
    echo "uv not found. Install with: curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
fi
uv --version

echo "==> Creating virtualenv and installing dependencies"
uv sync --extra dev

echo "==> Creating data directory"
mkdir -p data

if [ ! -f .env ]; then
    echo "==> Copying .env.example to .env (FILL THIS IN BEFORE RUNNING)"
    cp .env.example .env
fi

echo
echo "==> Setup complete."
echo "Next steps:"
echo "  1. Edit .env with your API keys"
echo "  2. Activate venv:   source .venv/bin/activate"
echo "  3. (Phase 1+):      python -m traderouter.orchestrator"
