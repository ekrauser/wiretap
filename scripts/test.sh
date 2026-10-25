#!/usr/bin/env bash
# Run the mod's Lua tests (under Factorio's Lua version, 5.2) and the bridge's
# Python tests. Lua 5.2 is found via `lua5.2`, `lua` or `mise exec lua@5.2.4`.
set -euo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

lua_cmd=""
if command -v lua5.2 >/dev/null; then lua_cmd="lua5.2"
elif command -v lua >/dev/null && lua -v 2>&1 | grep -q 'Lua 5\.2'; then lua_cmd="lua"
elif command -v mise >/dev/null; then lua_cmd="mise exec lua@5.2.4 -- lua"
fi
if [ -z "$lua_cmd" ]; then
  echo "no Lua 5.2 interpreter found (install lua5.2 or mise)" >&2
  exit 1
fi

echo "== Lua (control.lua under mocked Factorio 2.1 runtime)"
$lua_cmd -e 'print(_VERSION)'
(cd "$repo_root" && $lua_cmd tests/lua/test_control.lua)
# The snapshot the Lua test writes must be valid JSON for the bridge.
(cd "$repo_root" && $lua_cmd tests/lua/dump_snapshot.lua > /tmp/wiretap-test-snapshot.json)
python3 -c 'import json,sys; json.load(open("/tmp/wiretap-test-snapshot.json")); print("snapshot is valid JSON")'

echo "== Python (bridge)"
(cd "$repo_root" && python3 -m unittest discover -s tests/python 2>&1 | tee /tmp/wiretap-pytest.log | tail -4)
grep -qE '^Ran [1-9][0-9]* tests?' /tmp/wiretap-pytest.log || { echo "no Python tests ran" >&2; exit 1; }
grep -q '^OK' /tmp/wiretap-pytest.log || { echo "Python tests failed" >&2; exit 1; }
