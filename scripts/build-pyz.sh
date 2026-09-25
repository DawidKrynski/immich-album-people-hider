#!/bin/sh
# Build a single-file executable (needs only python3 >= 3.10 on the target).
set -eu
cd "$(dirname "$0")/.."
stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
cp -r src/immich_album_people_hider "$stage/"
find "$stage" -name __pycache__ -prune -exec rm -rf {} +
printf 'import sys\nfrom immich_album_people_hider.cli import main\nsys.exit(main())\n' > "$stage/__main__.py"
mkdir -p dist
python3 -m zipapp "$stage" -p '/usr/bin/env python3' -o dist/immich-album-people-hider.pyz -c
echo "built dist/immich-album-people-hider.pyz"
