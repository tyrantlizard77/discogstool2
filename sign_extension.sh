#!/usr/bin/env bash
# sign_extension.sh — Sign the Discogs Label Printer Firefox extension via AMO
#
# Signs the extension as "unlisted" (private, self-distributed).
# The resulting .xpi can be drag-dropped into any Firefox to install permanently.
#
# AMO API credentials are stored in ~/.discogstool/amo_auth and reused on
# subsequent runs.  Get them at:
#   https://addons.mozilla.org/en-US/developers/addon/api/key/
#
# Requires: npm (for web-ext)

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AMO_AUTH_FILE="${HOME}/.discogstool/amo_auth"
XPI_DIR="${SCRIPT_DIR}/web-ext-artifacts"

# ── Ensure web-ext is available ───────────────────────────────────────────────

if ! command -v web-ext &>/dev/null; then
    echo "web-ext not found.  Installing globally via npm..."
    npm install -g web-ext
fi

# ── Load or prompt for AMO credentials ───────────────────────────────────────

load_creds() {
    if [[ -f "$AMO_AUTH_FILE" ]]; then
        # shellcheck disable=SC1090
        source "$AMO_AUTH_FILE"
    fi
}

save_creds() {
    mkdir -p "$(dirname "$AMO_AUTH_FILE")"
    cat > "$AMO_AUTH_FILE" <<EOF
AMO_API_KEY=${AMO_API_KEY}
AMO_API_SECRET=${AMO_API_SECRET}
EOF
    chmod 600 "$AMO_AUTH_FILE"
    echo "  Credentials saved to ${AMO_AUTH_FILE}"
}

load_creds

if [[ -z "${AMO_API_KEY:-}" || -z "${AMO_API_SECRET:-}" ]]; then
    echo "AMO API credentials not found."
    echo "Get them at: https://addons.mozilla.org/en-US/developers/addon/api/key/"
    echo ""
    read -rp "AMO API Key (JWT issuer):  " AMO_API_KEY
    read -rp "AMO API Secret (JWT secret): " AMO_API_SECRET
    echo ""
    save_creds
fi

# ── Sign ──────────────────────────────────────────────────────────────────────

echo "Signing firefox-ext/ as unlisted extension..."
mkdir -p "$XPI_DIR"

web-ext sign \
    --source-dir "${SCRIPT_DIR}/firefox-ext" \
    --artifacts-dir "$XPI_DIR" \
    --api-key    "$AMO_API_KEY" \
    --api-secret "$AMO_API_SECRET" \
    --channel    unlisted

# ── Find and install the .xpi ─────────────────────────────────────────────────

XPI="$(find "$XPI_DIR" -name '*.xpi' -newer "${SCRIPT_DIR}/firefox-ext/manifest.json" \
       | sort | tail -1)"

if [[ -z "$XPI" ]]; then
    # Fallback: any .xpi in the artifacts dir
    XPI="$(find "$XPI_DIR" -name '*.xpi' | sort | tail -1)"
fi

if [[ -z "$XPI" ]]; then
    echo ""
    echo "Warning: could not locate the signed .xpi in ${XPI_DIR}"
    echo "Check web-ext output above for the file path."
    exit 0
fi

echo ""
echo "Signed: $(basename "$XPI")"
echo "Opening in Firefox — click 'Add' when prompted."

# macOS doesn't register .xpi with any app by default; open explicitly with Firefox.
FIREFOX_CANDIDATES=(
    "/Applications/Firefox.app"
    "/Applications/Firefox Developer Edition.app"
    "${HOME}/Applications/Firefox.app"
)
FIREFOX_APP=""
for candidate in "${FIREFOX_CANDIDATES[@]}"; do
    if [[ -d "$candidate" ]]; then
        FIREFOX_APP="$candidate"
        break
    fi
done

if [[ -n "$FIREFOX_APP" ]]; then
    open -a "$FIREFOX_APP" "$XPI"
else
    echo ""
    echo "Could not find Firefox.app.  Install the extension manually:"
    echo "  Drag this file into Firefox, or open it via File → Open File:"
    echo "  $XPI"
fi
