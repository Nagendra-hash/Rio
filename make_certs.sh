#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# make_certs.sh — local HTTPS for the JARVIS dashboard
#
# Creates config/certs/ with:
#   ca.crt      — local mini-CA (install this on phones/PCs to kill warnings)
#   jarvis.crt  — dashboard leaf certificate (SAN: LAN IP + localhost)
#   jarvis.key  — dashboard private key (chmod 600)
#
# dashboard/server.py picks these up automatically: port 8000 + alias 8001
# both serve HTTPS the moment the files exist. Rerun this script whenever the
# PC's LAN IP changes (the SAN is baked at generation time).
#
# Phone setup after running (one time): open http://<LAN-IP>:8000/ca.crt
# in Chrome → install the CA → mic works on https with zero warnings.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

CERT_DIR="config/certs"
LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
[ -n "${LAN_IP}" ] || { echo "✗ could not detect LAN IP"; exit 1; }

echo "[certs] LAN IP: ${LAN_IP}"
mkdir -p "${CERT_DIR}"

if [ -f "${CERT_DIR}/ca.crt" ]; then
    echo "[certs] CA already exists — reusing. Delete ${CERT_DIR} to regenerate."
else
    echo "[certs] generating mini-CA (10 years)…"
    openssl req -x509 -newkey rsa:3072 -sha256 -nodes -days 3650 \
        -keyout "${CERT_DIR}/ca.key" -out "${CERT_DIR}/ca.crt" \
        -subj "/CN=JARVIS Local CA/O=JARVIS" \
        -addext "basicConstraints=critical,CA:TRUE" \
        -addext "keyUsage=critical,keyCertSign,cRLSign" \
        -addext "subjectKeyIdentifier=hash" 2>/dev/null
fi

echo "[certs] generating leaf certificate (SAN: ${LAN_IP}, localhost)…"
openssl req -newkey rsa:2048 -sha256 -nodes \
    -keyout "${CERT_DIR}/jarvis.key" -out "${CERT_DIR}/jarvis.csr" \
    -subj "/CN=JARVIS Dashboard/O=JARVIS" 2>/dev/null

cat > "${CERT_DIR}/leaf.ext" <<EOF
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid,issuer
subjectAltName=IP:${LAN_IP},IP:127.0.0.1,DNS:localhost,DNS:jarvis.local
EOF

openssl x509 -req -sha256 -days 825 \
    -in "${CERT_DIR}/jarvis.csr" \
    -CA "${CERT_DIR}/ca.crt" -CAkey "${CERT_DIR}/ca.key" -CAcreateserial \
    -out "${CERT_DIR}/jarvis.crt" \
    -extfile "${CERT_DIR}/leaf.ext" 2>/dev/null

chmod 600 "${CERT_DIR}/jarvis.key" "${CERT_DIR}/ca.key"
rm -f "${CERT_DIR}/jarvis.csr"

echo "[certs] ✅ done:"
ls -la "${CERT_DIR}"
echo "[certs] verify:  openssl x509 -in ${CERT_DIR}/jarvis.crt -noout -text | grep -A1 'Alternative'"
echo "[certs] restart JARVIS to activate HTTPS on ports 8000 + 8001."
