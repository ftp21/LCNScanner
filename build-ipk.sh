#!/bin/bash
# Costruisce l'ipk di LCNScanner a partire dai sorgenti del plugin (layout
# "piatto": plugin.py, *.xml, *.png, locale/ direttamente in questa cartella,
# senza dover mantenere un albero usr/ duplicato).
# Uso: ./build-ipk.sh [versione]
#   - se la versione non e' passata come argomento, viene derivata dal tag
#     git corrente (es. tag "v1.2.0" -> versione "1.2.0"); fallisce se non
#     si e' esattamente su un tag e la versione non e' stata passata.
# Output: dist/enigma2-plugin-systemplugins-lcnscanner_<versione>_all.ipk
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PKG_NAME="enigma2-plugin-systemplugins-lcnscanner"
INSTALL_PATH="usr/lib/enigma2/python/Plugins/SystemPlugins/LCNScanner"

VERSION="${1:-}"
if [ -z "$VERSION" ]; then
	TAG="$(git describe --tags --exact-match 2>/dev/null || true)"
	if [ -z "$TAG" ]; then
		echo "Errore: nessuna versione passata e HEAD non e' su un tag." >&2
		echo "Uso: $0 <versione>  (es. $0 1.2.0)" >&2
		exit 1
	fi
	VERSION="${TAG#v}"
fi

for required in plugin.py __init__.py setup.xml rules.xml LCNScanner.png; do
	if [ ! -f "$required" ]; then
		echo "Errore: file richiesto '$required' non trovato in $SCRIPT_DIR." >&2
		exit 1
	fi
done

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

# --- data.tar.gz: l'albero da installare (usr/lib/enigma2/...) -----------
DATA_DIR="$WORKDIR/data"
DEST="$DATA_DIR/$INSTALL_PATH"
mkdir -p "$DEST"
cp -a plugin.py __init__.py setup.xml rules.xml LCNScanner.png "$DEST/"
if [ -d "locale" ]; then
	cp -a locale "$DEST/"
fi
find "$DATA_DIR" -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find "$DATA_DIR" -name "*.pyc" -delete

INSTALLED_SIZE="$(du -sk "$DATA_DIR" | cut -f1)"

tar --numeric-owner --owner=0 --group=0 --sort=name --mtime="@0" \
	-czf "$WORKDIR/data.tar.gz" -C "$DATA_DIR" .

# --- control.tar.gz: metadati del pacchetto -------------------------------
CONTROL_DIR="$WORKDIR/control"
mkdir -p "$CONTROL_DIR"
sed \
	-e "s/^Version:.*/Version: ${VERSION}/" \
	-e "/^Description:/i Installed-Size: ${INSTALLED_SIZE}" \
	opkg-control/control > "$CONTROL_DIR/control"

tar --numeric-owner --owner=0 --group=0 --sort=name --mtime="@0" \
	-czf "$WORKDIR/control.tar.gz" -C "$CONTROL_DIR" .

# --- debian-binary ---------------------------------------------------------
echo "2.0" > "$WORKDIR/debian-binary"

# --- assemblaggio ipk (= ar di debian-binary + control.tar.gz + data.tar.gz)
mkdir -p dist
OUT="dist/${PKG_NAME}_${VERSION}_all.ipk"
rm -f "$OUT"
ar -rcD "$OUT" \
	"$WORKDIR/debian-binary" "$WORKDIR/control.tar.gz" "$WORKDIR/data.tar.gz"

echo "Creato $OUT (versione $VERSION, $INSTALLED_SIZE KB installati)"
