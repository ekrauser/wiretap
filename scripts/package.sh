#!/usr/bin/env bash
# Package the mod into a Factorio-compatible zip: wiretap_<version>.zip
# containing a wiretap_<version>/ folder, as the mod portal expects.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mod_dir="$repo_root/mod/wiretap"
version="$(python3 -c "import json; print(json.load(open('$mod_dir/info.json'))['version'])")"
out_dir="$repo_root/dist"
stage="$out_dir/wiretap_$version"

rm -rf "$stage" "$out_dir/wiretap_$version.zip"
mkdir -p "$stage"
cp -r "$mod_dir/." "$stage/"

(cd "$out_dir" && zip -qr "wiretap_$version.zip" "wiretap_$version")
rm -rf "$stage"
echo "Built $out_dir/wiretap_$version.zip"
echo "Copy it into your Factorio mods folder:"
echo "  Windows: %APPDATA%\\Factorio\\mods\\"
echo "  Linux:   ~/.factorio/mods/"
