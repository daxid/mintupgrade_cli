#!/bin/bash
set -e

if [ "$(id -u)" -ne 0 ]; then
    echo "Run with sudo"
    exit 1
fi

DEST="/usr/lib/linuxmint/mintupgrade"
BIN="/usr/bin/mintupgrade"

echo "Installing mintupgrade CLI …"

mkdir -p "$DEST"
cp usr/lib/linuxmint/mintupgrade/mintupgrade_cli.py "$DEST/"
chmod 644 "$DEST/mintupgrade_cli.py"

# Back up old entry point
[ -f "$BIN" ] && cp "$BIN" "${BIN}.gui.bak"

cat > "$BIN" << 'EOF'
#!/bin/bash
exec /usr/bin/python3 /usr/lib/linuxmint/mintupgrade/mintupgrade_cli.py "$@"
EOF
chmod 755 "$BIN"

mkdir -p /var/log/mintupgrade

echo "Done.  Run: sudo mintupgrade --help"
