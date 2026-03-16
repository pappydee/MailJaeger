#!/bin/sh
# MailJaeger container entrypoint
#
# Optionally imports custom CA certificates before starting the server.
# This allows local TLS interception (e.g. corporate antivirus / proxy) to
# work without disabling TLS verification or hardcoding a specific certificate.
#
# Usage:
#   Mount a local directory of *.crt / *.pem files to /app/certs inside the
#   container (read-only is fine):
#
#     volumes:
#       - ./certs:/app/certs:ro
#
#   Files in that directory are imported into the system trust store at startup
#   via update-ca-certificates.  Normal TLS verification stays enabled.
#
# Security note:
#   - Trust is only extended to explicitly provided CA certificates.
#   - TLS verification is NOT disabled; only the provided CAs are trusted.
#   - Do not place untrusted certificates in the certs directory.

set -e

CERTS_DIR="${CERTS_DIR:-/app/certs}"
SYSTEM_CA_DIR="/usr/local/share/ca-certificates"

# Import custom CA certificates if the certs directory is present and non-empty
if [ -d "$CERTS_DIR" ]; then
    # Use find to robustly list cert files (avoids literal glob strings on no-match)
    cert_files=$(find "$CERTS_DIR" -maxdepth 1 \( -name "*.crt" -o -name "*.pem" \) -type f 2>/dev/null)
    if [ -n "$cert_files" ]; then
        cert_count=$(echo "$cert_files" | wc -l)
        echo "[entrypoint] Importing $cert_count custom CA certificate(s) from $CERTS_DIR"
        echo "$cert_files" | while IFS= read -r cert_file; do
            cert_name=$(basename "$cert_file")
            # update-ca-certificates expects .crt extension
            case "$cert_name" in
                *.pem) dest="$SYSTEM_CA_DIR/${cert_name%.pem}.crt" ;;
                *)     dest="$SYSTEM_CA_DIR/$cert_name" ;;
            esac
            cp "$cert_file" "$dest"
            echo "[entrypoint]   + $cert_name"
        done
        update-ca-certificates --fresh 2>&1 | grep -v "^$" || true
        echo "[entrypoint] Certificate trust store updated."
    fi
fi

# Hand off to the main application
exec python -m uvicorn src.main:app \
    --host "${SERVER_HOST:-127.0.0.1}" \
    --port "${SERVER_PORT:-8000}"
