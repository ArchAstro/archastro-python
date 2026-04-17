#!/usr/bin/env bash
# Regenerate the Python SDK from the canonical OpenAPI spec.
#
# Flow:
#   1. Fetch specs/platform-openapi.json from ArchAstro/archastro-openapi
#      on GitHub (the canonical source of truth).
#   2. Copy it into ./specs/platform-openapi.json so the SDK package
#      ships its own copy for contract-test consumers.
#   3. Run @archastro/sdk-generator (from public npm via npx) to emit
#      Pydantic models, resources, and channel classes under
#      src/archastro/platform/, and the contract-test tree under
#      tests/contract/.
#
# Usage:
#   ./scripts/regenerate_sdk.sh                       # pull spec from main
#   ARCHASTRO_OPENAPI_REF=some-branch ./scripts/regenerate_sdk.sh
#   ARCHASTRO_SDK_GENERATOR=@archastro/sdk-generator@0.1.0 ./scripts/regenerate_sdk.sh
#
# Env knobs:
#   ARCHASTRO_OPENAPI_REF     Git ref in archastro-openapi to pull the
#                             spec from (default: main). Useful when a
#                             spec change is on a branch awaiting merge.
#   ARCHASTRO_SDK_GENERATOR   Package spec for the generator passed to
#                             npx (default: @archastro/sdk-generator@latest).
#                             Pin to a specific version for reproducible
#                             regenerations in a release branch.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SPEC_DST="$REPO_ROOT/specs/platform-openapi.json"
CONFIG_FILE="$REPO_ROOT/scripts/sdk-generator-config.json"

REF="${ARCHASTRO_OPENAPI_REF:-main}"
SPEC_URL="https://raw.githubusercontent.com/ArchAstro/archastro-openapi/${REF}/specs/platform-openapi.json"
SDK_GENERATOR_SPEC="${ARCHASTRO_SDK_GENERATOR:-@archastro/sdk-generator@latest}"

log() { printf '==> %s\n' "$*"; }

# ─── 1. Fetch the spec ──────────────────────────────────────────

mkdir -p "$(dirname "$SPEC_DST")"
log "Fetching spec from $SPEC_URL"
curl --fail --silent --show-error --location "$SPEC_URL" -o "$SPEC_DST"

# Sanity-check the spec. Pass the path via env var rather than
# interpolating into the JS string so paths with quotes/spaces are safe.
SPEC="$SPEC_DST" node -e '
  const s = require(process.env.SPEC);
  const paths = Object.keys(s.paths ?? {}).length;
  const schemas = Object.keys(s.components?.schemas ?? {}).length;
  const channels = (s["x-channels"] ?? []).length;
  console.log(`Spec: ${paths} routes, ${schemas} schemas, ${channels} channels`);
'

# ─── 2. Generate SDK + contract tests ───────────────────────────

log "Generating Python SDK into $REPO_ROOT"
npx --yes "$SDK_GENERATOR_SPEC" \
  --spec "$SPEC_DST" \
  --config "$CONFIG_FILE" \
  --lang python \
  --out "$REPO_ROOT"

log "Generating Python contract tests into $REPO_ROOT"
npx --yes "$SDK_GENERATOR_SPEC" \
  --spec "$SPEC_DST" \
  --config "$CONFIG_FILE" \
  --lang contract-tests-py \
  --out "$REPO_ROOT"

# ─── 3. Normalize formatting ────────────────────────────────────

# Run ruff across the generated trees so the committed output matches
# the repo's style config. The generator emits deliberately plain
# Python; this brings it in line with what CI expects.
log "Applying ruff lint fixes + format"
# cd into the repo so `uv run` discovers this repo's pyproject.toml
# even when the script is invoked with a different CWD.
(
  cd "$REPO_ROOT"
  uv run ruff check --fix --fix-only src tests >/dev/null
  uv run ruff format src tests >/dev/null
)

log "Done. Review the diff and commit, or re-run this script after the spec is updated upstream."
