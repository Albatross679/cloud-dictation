#!/usr/bin/env bash
# Create a self-signed code signing identity so rebuilds keep one signature.
#
# Ad hoc signing (`codesign -s -`) mints a new identity every build, and macOS
# ties Accessibility, Input Monitoring, Microphone, and PostEvent grants to the
# signature. That is why permissions die on every rebuild. A stable certificate
# ends it: grant once, then every later build is the same app to TCC.
#
# User scoped and free. It does not make the app distributable, which still
# needs an Apple Developer ID, but it does make it stable on this Mac.
set -euo pipefail

CN="${1:-Cloud Dictation Local Signing}"
KEYCHAIN="$HOME/Library/Keychains/login.keychain-db"

if security find-certificate -c "$CN" >/dev/null 2>&1; then
  echo "Identity already exists: $CN"
  security find-identity -v -p codesigning | grep "$CN" || true
  exit 0
fi

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

# codeSigning EKU is what lets codesign accept the certificate.
cat > "$WORK/openssl.cnf" <<EOF
[ req ]
distinguished_name = dn
x509_extensions = v3
prompt = no

[ dn ]
CN = $CN

[ v3 ]
basicConstraints = critical,CA:false
keyUsage = critical,digitalSignature
extendedKeyUsage = critical,codeSigning
EOF

openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
  -keyout "$WORK/key.pem" -out "$WORK/cert.pem" -config "$WORK/openssl.cnf" 2>/dev/null

# macOS rejects the PKCS12 MAC that OpenSSL 3 writes by default, so the
# bundle is built with the older SHA-1 based algorithms it accepts.
PW="clouddictation"
openssl pkcs12 -export -out "$WORK/identity.p12" \
  -inkey "$WORK/key.pem" -in "$WORK/cert.pem" \
  -keypbe PBE-SHA1-3DES -certpbe PBE-SHA1-3DES -macalg sha1 \
  -passout "pass:$PW" 2>/dev/null

security import "$WORK/identity.p12" -k "$KEYCHAIN" -P "$PW" \
  -T /usr/bin/codesign -T /usr/bin/security >/dev/null

# Trust it for code signing only, in the user's own trust settings, so no
# sudo is needed and TLS trust is untouched.
security add-trusted-cert -r trustRoot -p codeSign -k "$KEYCHAIN" "$WORK/cert.pem" >/dev/null 2>&1 \
  || echo "note: trust settings needed confirmation; approve the dialog if one appeared"

# Let codesign use the key without prompting for the keychain password.
security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "$PW" "$KEYCHAIN" >/dev/null 2>&1 || true

echo "Created: $CN"
security find-identity -v -p codesigning | grep "$CN" || {
  echo "Certificate imported but not yet a valid signing identity." >&2
  echo "Open Keychain Access, find \"$CN\", and set Trust > Code Signing to Always Trust." >&2
  exit 1
}
