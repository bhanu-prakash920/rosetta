#!/usr/bin/env bash
# Demo certificates for MQTT with mutual TLS: a certificate authority, a server
# certificate for EMQX, and client certificates.
#
#   scripts/make_certs.sh                        CA, server, one client: "gateway"
#   scripts/make_certs.sh gateway simulator      CA, server, two clients
#   scripts/make_certs.sh helix.hx-0000012       a device: common name "<source>.<device>"
#
# Output: infra/emqx/certs (override with CERT_DIR). Every file it leaves behind
# ends in .pem or .key, and the project's .gitignore ignores both, so nothing
# here can be committed by accident.
#
#   ca.pem ca.key                  the demo authority. ca.key signs, keep it private.
#   server.pem server.key          EMQX. Names: emqx, emqx.rosetta.internal, localhost, 127.0.0.1
#   client-<name>.pem / .key       one pair per client
#
# The CA and the server certificate are created once and reused, so client
# certificates issued earlier stay valid. Delete the directory to start again.
#
# DEMO MATERIAL. Server and client keys are written world-readable (0644), so
# that containers running under another user id can read them through a bind
# mount. A real deployment takes certificates from its PKI (cert-manager, Vault,
# AWS Private CA) and mounts them as secrets.
#
# Works with OpenSSL 1.1.1 or newer and with LibreSSL (macOS).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${CERT_DIR:-$ROOT/infra/emqx/certs}"
DAYS_CA="${DAYS_CA:-825}"
DAYS_LEAF="${DAYS_LEAF:-90}"
SERVER_NAMES="${SERVER_NAMES:-emqx emqx.rosetta.internal localhost}"
SERVER_IPS="${SERVER_IPS:-127.0.0.1}"

command -v openssl > /dev/null || { echo "openssl is not installed" >&2; exit 1; }

if [ "$#" -eq 0 ]; then set -- gateway; fi
for name in "$@"; do
    case "$name" in
        *[!A-Za-z0-9._-]*|"") echo "invalid client name: '$name' (letters, digits, dot, hyphen, underscore)" >&2; exit 1 ;;
    esac
done

mkdir -p "$OUT"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

new_key() {   # EC P-256: small, fast, accepted by TLS 1.2 and 1.3
    openssl ecparam -name prime256v1 -genkey -noout -out "$1"
}

sign() {      # sign <csr> <extension file> <out>
    openssl x509 -req -in "$1" -CA "$OUT/ca.pem" -CAkey "$OUT/ca.key" -CAcreateserial \
        -CAserial "$WORK/ca.srl" -days "$DAYS_LEAF" -sha256 -extfile "$2" -out "$3" 2> /dev/null
}

# ------------------------------------------------------------------------ CA
if [ ! -f "$OUT/ca.pem" ] || [ ! -f "$OUT/ca.key" ]; then
    new_key "$OUT/ca.key"
    chmod 600 "$OUT/ca.key"
    cat > "$WORK/ca.cnf" <<CNF
[req]
distinguished_name = dn
x509_extensions = v3_ca
prompt = no
[dn]
O = Rosetta Demo
CN = Rosetta Demo CA
[v3_ca]
basicConstraints = critical, CA:TRUE, pathlen:0
keyUsage = critical, keyCertSign, cRLSign
subjectKeyIdentifier = hash
CNF
    openssl req -new -x509 -key "$OUT/ca.key" -sha256 -days "$DAYS_CA" -config "$WORK/ca.cnf" -out "$OUT/ca.pem"
    echo "created   $OUT/ca.pem"
else
    echo "kept      $OUT/ca.pem"
fi

# -------------------------------------------------------------------- server
if [ ! -f "$OUT/server.pem" ] || [ ! -f "$OUT/server.key" ]; then
    new_key "$OUT/server.key"
    chmod 644 "$OUT/server.key"
    san=""
    i=1; for n in $SERVER_NAMES; do san="${san}DNS.$i = $n"$'\n'; i=$((i + 1)); done
    i=1; for n in $SERVER_IPS; do san="${san}IP.$i = $n"$'\n'; i=$((i + 1)); done
    cat > "$WORK/server.ext" <<CNF
basicConstraints = critical, CA:FALSE
keyUsage = critical, digitalSignature
extendedKeyUsage = serverAuth
subjectAltName = @names
[names]
$san
CNF
    openssl req -new -key "$OUT/server.key" -subj "/O=Rosetta Demo/CN=emqx" -out "$WORK/server.csr"
    sign "$WORK/server.csr" "$WORK/server.ext" "$OUT/server.pem"
    echo "created   $OUT/server.pem"
else
    echo "kept      $OUT/server.pem"
fi

# ------------------------------------------------------------------- clients
cat > "$WORK/client.ext" <<CNF
basicConstraints = critical, CA:FALSE
keyUsage = critical, digitalSignature
extendedKeyUsage = clientAuth
CNF
for name in "$@"; do
    crt="$OUT/client-$name.pem"
    key="$OUT/client-$name.key"
    new_key "$key"
    chmod 644 "$key"
    openssl req -new -key "$key" -subj "/O=Rosetta Demo/CN=$name" -out "$WORK/client.csr"
    sign "$WORK/client.csr" "$WORK/client.ext" "$crt"
    echo "created   $crt   (common name: $name)"
done

# ------------------------------------------------------------------- verify
openssl verify -CAfile "$OUT/ca.pem" "$OUT/server.pem" > /dev/null
for name in "$@"; do
    openssl verify -CAfile "$OUT/ca.pem" "$OUT/client-$name.pem" > /dev/null
done
echo "verified  every certificate chains to $OUT/ca.pem"
