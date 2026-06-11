#!/usr/bin/env bash
# Regenerate the Python SDK from the canonical OpenAPI spec.
#
# Flow:
#   1. Fetch specs/platform-openapi.json from ArchAstro/archastro-openapi
#      on GitHub (the canonical source of truth).
#   2. Copy it into ./specs/platform-openapi.json so the SDK package
#      ships its own copy for contract-test consumers.
#   3. Run the locked local @archastro/sdk-generator to emit
#      Pydantic models, resources, and channel classes under
#      src/archastro/platform/, and the contract-test tree under
#      tests/contract/.
#
# Usage:
#   ./scripts/regenerate_sdk.sh                       # pull spec from main
#   ARCHASTRO_OPENAPI_REF=some-branch ./scripts/regenerate_sdk.sh
#
# Env knobs:
#   ARCHASTRO_OPENAPI_REF     Git ref in archastro-openapi to pull the
#                             spec from (default: main). Useful when a
#                             spec change is on a branch awaiting merge.
#   ARCHASTRO_SDK_GENERATOR_BIN
#                             Path to a locally installed sdk-generator binary.
#                             Defaults to node_modules/.bin/sdk-generator.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SPEC_DST="$REPO_ROOT/specs/platform-openapi.json"
CONFIG_FILE="$REPO_ROOT/scripts/sdk-generator-config.json"

REF="${ARCHASTRO_OPENAPI_REF:-main}"
SPEC_URL="https://raw.githubusercontent.com/ArchAstro/archastro-openapi/${REF}/specs/platform-openapi.json"
SDK_GENERATOR_BIN="${ARCHASTRO_SDK_GENERATOR_BIN:-$REPO_ROOT/node_modules/.bin/sdk-generator}"

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
if [ ! -x "$SDK_GENERATOR_BIN" ]; then
  echo "sdk-generator not found at $SDK_GENERATOR_BIN. Run 'npm ci --ignore-scripts' first." >&2
  exit 1
fi

"$SDK_GENERATOR_BIN" \
  --spec "$SPEC_DST" \
  --config "$CONFIG_FILE" \
  --lang python \
  --out "$REPO_ROOT"

log "Generating Python contract tests into $REPO_ROOT"
"$SDK_GENERATOR_BIN" \
  --spec "$SPEC_DST" \
  --config "$CONFIG_FILE" \
  --lang contract-tests-py \
  --out "$REPO_ROOT"

# ─── 3. Normalize formatting ────────────────────────────────────

# Run ruff across the generated trees so the committed output matches
# the repo's style config. The generator emits deliberately plain
# Python; this brings it in line with what CI expects.
log "Applying ruff lint fixes + format"
uv run ruff check --fix --fix-only src tests >/dev/null
uv run ruff format src tests >/dev/null

log "Done. Review the diff and commit, or re-run this script after the spec is updated upstream."
