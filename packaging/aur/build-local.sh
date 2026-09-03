# Local / CI helper: build the AUR package from the current tree (no GitHub tag needed).
# Usage: ./packaging/aur/build-local.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

pkgver="$(sed -n 's/^version = "\(.*\)"/\1/p' "$ROOT/pyproject.toml" | head -1)"
pkgname=cursor-spend-tray

mkdir -p "$WORKDIR/$pkgname-$pkgver"
# Copy package sources (exclude venv / git / build artifacts)
tar -C "$ROOT" \
  --exclude='.git' \
  --exclude='.venv' \
  --exclude='dist' \
  --exclude='build' \
  --exclude='*.egg-info' \
  --exclude='__pycache__' \
  -cf - . | tar -C "$WORKDIR/$pkgname-$pkgver" -xf -

cd "$WORKDIR"
tar -czf "$pkgname-$pkgver.tar.gz" "$pkgname-$pkgver"

cp "$ROOT/packaging/aur/PKGBUILD" "$WORKDIR/PKGBUILD"
# Point source at the local tarball and fill checksum
sed -i "s|^pkgver=.*|pkgver=$pkgver|" "$WORKDIR/PKGBUILD"
sed -i "s|^source=.*|source=(\"$pkgname-$pkgver.tar.gz\")|" "$WORKDIR/PKGBUILD"
sum="$(sha256sum "$WORKDIR/$pkgname-$pkgver.tar.gz" | awk '{print $1}')"
sed -i "s|^sha256sums=.*|sha256sums=('$sum')|" "$WORKDIR/PKGBUILD"

cd "$WORKDIR"
# Extra flags: MAKEPKG_OPTS="-d" to skip dep checks (build-only smoke test).
# shellcheck disable=SC2086
makepkg -f ${MAKEPKG_OPTS:-}
echo "Built packages:"
ls -1 "$WORKDIR"/*.pkg.tar.*
mkdir -p "$ROOT/dist"
cp -v "$WORKDIR"/*.pkg.tar.* "$ROOT/dist/"
# Refresh .SRCINFO next to the repo PKGBUILD for AUR submits (uses GitHub source URL).
if command -v makepkg >/dev/null; then
  (
    cd "$ROOT/packaging/aur"
    makepkg --printsrcinfo > .SRCINFO
  )
fi
