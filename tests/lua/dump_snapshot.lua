-- Prints one snapshot from the mocked world to stdout (used by scripts/test.sh
-- to feed the bridge tests and to validate the JSON with a real parser).
local here = arg[0]:match("^(.*)/[^/]*$") or "."
package.path = here .. "/?.lua;" .. package.path
local M = require("mock")
local world = require("world")
M.install{settings = {["wiretap-interval-seconds"] = 10, ["wiretap-decimals"] = 3, ["wiretap-spread"] = true, ["wiretap-mode"] = "snapshot",
  ["wiretap-filename"] = "wiretap/stats.json", ["wiretap-journal-filename"] = "wiretap/stats.ndjson",
  ["wiretap-server-only"] = true, ["wiretap-logistics"] = true, ["wiretap-quality-breakdown"] = true,
  ["wiretap-power"] = true, ["wiretap-power-breakdown"] = true, ["wiretap-power-flow"] = true,
  ["wiretap-production"] = true, ["wiretap-production-quality"] = true, ["wiretap-science"] = true,
  ["wiretap-pollution"] = true, ["wiretap-platforms"] = true, ["wiretap-kills"] = true,
  ["wiretap-build-counts"] = false}}
game = world{}
dofile(arg[1] or (here .. "/../../mod/wiretap/control.lua"))
M.handlers.on_init()
io.write(M.remote_interfaces.wiretap.stats_json(), "\n")
