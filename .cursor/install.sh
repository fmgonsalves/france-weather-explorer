#!/usr/bin/env bash
set -euo pipefail

# Install uv (the project's Python toolchain and dependency manager) if the
# base image does not already provide it. The installer is idempotent and
# simply overwrites the existing binary when present.
if ! command -v uv >/dev/null 2>&1 && [ ! -x "$HOME/.local/bin/uv" ]; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="$HOME/.local/bin:$PATH"

# Provision the pinned Python interpreter (see .python-version) and install all
# locked dependencies into the project virtual environment. Uses the committed
# uv.lock so the environment is reproducible.
uv sync --frozen
