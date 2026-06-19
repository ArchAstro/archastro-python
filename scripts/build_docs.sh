#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

rm -rf site
uv run pdoc \
  archastro.platform \
  archastro.phx_channel \
  '!archastro.phx_channel.tests' \
  --output-directory site \
  --docformat google \
  --footer-text "ArchAstro Python SDK"
