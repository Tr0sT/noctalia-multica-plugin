local checks = 0
local function check(value, message)
  assert(value, message)
  checks = checks + 1
end

local now = 0
local tree = nil
local pending = {}
local watches = {}
local snapshot = { profiles = {}, active = {}, queue = {}, recent = {}, running = 1, waiting = 0, updated_at = os.time() }
local state = { snapshot = snapshot }
local widgetText = ""
local function task(id, status)
  return { id = id, key = id, title = "Task " .. id, status = status, identifier = "M-1", agent = "Agent", workspace = "Space",
    profile = "", workspace_id = "workspace", completed_at = "", detail_error = "", url = "https://example.test/task/" .. id }
end
snapshot.active = { task("one", "running"), task("two", "dispatched") }
snapshot.queue = { task("queued", "queued") }
snapshot.profiles = { { name = "", device = "pc", status = "running", running = 1, active = 2,
  online = true, can_control = true, agents = {}, errors = {} } }

local bridgeClient = { call = function(args, callback, timeout)
  table.insert(pending, { args = args, callback = callback, timeout = timeout })
end }
local host = {
  tr = function(key) return key end,
  getConfig = function() return 10 end,
  nowMs = function() return now end,
  setUpdateInterval = function() end,
  runAsync = function() return true end,
  openSettings = function() end,
  state = {
    get = function(key) return state[key] end,
    set = function(key, value) state[key] = value; if watches[key] then watches[key](value) end end,
    watch = function(key, callback) watches[key] = callback end,
  },
}
local ui = setmetatable({}, { __index = function(_, kind)
  return function(props, children) return { kind = kind, props = props, children = children or {} } end
end })
local env = setmetatable({
  noctalia = host, ui = ui, require = function() return bridgeClient end,
  panel = { render = function(value) tree = value end, setWantsSecondTicks = function() end, close = function() end },
  barWidget = { setGlyph = function() end, setText = function(t) widgetText = t end, setTooltip = function() end, setGlyphColor = function() end },
}, { __index = getfenv() })
local function load(source)
  local chunk, err = loadstring(source)
  assert(chunk, err)
  setfenv(chunk, env)()
end
local function find(predicate, node)
  node = node or tree
  if predicate(node) then return node end
  for _, child in ipairs(node.children) do local hit = find(predicate, child); if hit then return hit end end
end
local function text(value)
  return find(function(n) return n.props.text == value end)
end
local function key(value)
  return find(function(n) return n.props.key == value end)
end
local function pressText(value)
  local node = text(value); assert(node, "missing text " .. value); node.props.onClick()
end
local function message(seq, kind, body)
  return { seq = seq, type = kind or "text", text = body or "Message " .. tostring(seq), created_at = "", tool = "", truncated = false }
end
local function reply(data, err)
  local request = table.remove(pending, 1); assert(request, "no request")
  if data and data.messages and data.last_seq == nil and #data.messages > 0 then data.last_seq = data.messages[#data.messages].seq end
  request.callback(data, err)
  return request
end

load(panelSource)
env.onOpen()
check(key("one") ~= nil and key("queued") ~= nil, "running and queued cards are wired")
key("one").props.onClick()
check(pending[1].args[1] == "logs" and pending[1].args[7] == "one", "card requests its exact task log")
reply({ messages = { message(10, "reasoning", "Visible "), message(11, "reasoning", "reasoning") }, has_more = true })
check(text("Visible reasoning") ~= nil, "adjacent reasoning chunks are combined")
pressText("older")
reply({ messages = { message(9) }, has_more = false })
now = 4000
env.update()
check(#pending == 0, "reading older history pauses following the live tail")
pressText("latest")
reply({ messages = { message(12) }, has_more = true })
now = 8000
env.update()
check(#pending == 1 and pending[1].args[#pending[1].args] == 12, "live polling resumes from the newest sequence")
reply(nil, "Server unavailable")
check(text("Message 12") ~= nil and text("Server unavailable") ~= nil, "failed refresh preserves the visible transcript")

-- An old task request must never overwrite a newly selected task.
now = 12000
env.update()
local old = table.remove(pending, 1)
find(function(n) return n.props.tooltip == "back" end).props.onClick()
key("two").props.onClick()
old.callback({ messages = { message(99, "text", "WRONG TASK") }, has_more = false }, nil)
reply({ messages = { message(1, "text", "RIGHT TASK") }, has_more = false })
check(text("WRONG TASK") == nil and text("RIGHT TASK") ~= nil, "late callback stays isolated to its task")
now = 16000
env.update()
env.onClose()
local closedTree = tree
reply({ messages = { message(2) }, has_more = false })
check(tree == closedTree, "closing the panel discards a late response")
now = 20000
env.update()
check(#pending == 0, "closed panel never polls logs")

-- History and logs use the local labels supplied by the Python bridge.
local recent = task("recent", "completed")
recent.completed_at = "2026-11-02T11:00:00Z"
recent.completed_at_local = "2026-11-02 14:00"
snapshot.recent = { recent }
env.onOpen()
pressText("recent · 1")
check(text("2026-11-02 14:00") ~= nil, "history card displays the machine-local time")
check(text("2026-11-02 11:00 UTC") == nil, "history must not label raw UTC as local")
key("recent").props.onClick()
local first = message(40, "thinking", "Local ")
first.created_at = "2026-11-02T11:00:00Z"
first.created_at_local = "14:00:00"
local second = message(41, "thinking", "time")
second.created_at = "2026-11-02T11:00:01Z"
second.created_at_local = "14:00:01"
reply({ messages = { first, second, message(43) }, has_more = false })
check(text("14:00:00 · message.thinking") ~= nil, "merged log block keeps its first local time")
check(text("11:00:00 · message.thinking") == nil, "logs must not slice the raw UTC string")
check(text("Local time") ~= nil, "timestamp changes preserve chunk merging")
check(text("message.text") ~= nil, "missing timestamps omit the time without crashing")
env.onClose()
snapshot.recent = {}

-- Lifecycle controls act on exactly one profile and retain the last known state until refreshed.
local primary = snapshot.profiles[1]
local nord = { name = "nord", device = "pc", status = "stopped", running = 0, active = 0,
  online = false, can_control = true, agents = {}, errors = {} }
table.insert(snapshot.profiles, nord)
env.onOpen()
local stop = key("daemon-control:")
check(stop.props.text == "daemon.stop" and stop.props.glyph == "player-pause", "online daemon has a pause-shaped Stop control")
check(key("daemon-control:nord").props.glyph == "player-play", "offline profile has a Start control")
stop.props.onClick()
check(#pending == 0 and text("daemon.stop-warning 2") ~= nil, "active runs require confirmation before a command")
key("daemon-cancel:").props.onClick()
check(key("daemon-confirm:") == nil and #pending == 0, "cancelling leaves the daemon untouched")

local start = key("daemon-control:nord")
start.props.onClick()
start.props.onClick()
check(#pending == 1, "a second click cannot duplicate a pending lifecycle command")
check(table.concat(pending[1].args, "|") == "daemon|--profile|nord|--action|start", "start targets the named profile explicitly")
check(pending[1].timeout == 60000, "cold starts use Noctalia's maximum helper timeout")
check(key("daemon-control:nord").props.enabled == false, "pending command disables its control")
check(key("daemon-control:").props.enabled == true, "a pending command does not block another profile")
local earlierRefresh = state.refresh
reply({ ok = true, action = "start", profile = "nord" })
local startRefresh = state.refresh
check(startRefresh > earlierRefresh and nord.online == false, "accepted start requests a new snapshot without inventing online state")
host.state.set("refresh_done", startRefresh - 1)
check(key("daemon-control:nord").props.enabled == false, "an older snapshot completion cannot unlock the control")
nord.online = true
nord.status = "starting"
host.state.set("snapshot", snapshot)
check(key("daemon-control:nord").props.enabled == false, "snapshot arrival alone does not complete the matching refresh")
host.state.set("refresh_done", startRefresh)
check(key("daemon-control:nord").props.text == "daemon.stop" and text("starting") ~= nil,
  "fresh starting status offers Stop rather than a duplicate Start")
nord.status = "running"

key("daemon-control:").props.onClick()
key("daemon-confirm:").props.onClick()
check(table.concat(pending[1].args, "|") == "daemon|--profile||--action|stop|--allow-active",
  "confirmed Stop explicitly targets the default profile and authorizes active-run interruption")
reply(nil, "daemon.command-failed")
check(primary.online and text("daemon.command-failed") ~= nil, "command failure preserves the previous daemon state and shows its error")
check(key("daemon-control:").props.enabled == false, "failure also waits for status because command outcome may be uncertain")
host.state.set("refresh_done", state.refresh)
check(key("daemon-control:").props.enabled == true, "fresh status after failure permits a deliberate retry")

primary.active = 0
key("daemon-control:").props.onClick()
check(#pending[1].args == 5, "an idle Stop does not pre-authorize interruption of future runs")
reply({ confirmation_required = true, active = 3 })
check(text("daemon.stop-warning 3") ~= nil and key("daemon-confirm:") ~= nil,
  "newly active runs detected by the helper produce a confirmation")
key("daemon-cancel:").props.onClick()
key("daemon-control:").props.onClick()
env.onClose()
reply({ confirmation_required = true, active = 1 })
env.onOpen()
check(key("daemon-confirm:") == nil and key("daemon-control:").props.enabled == true,
  "a late confirmation after close requires a new explicit Stop click on reopen")
key("daemon-control:").props.onClick()
reply({ ok = true, action = "stop", profile = "" })
primary.online = false
primary.status = "stopped"
host.state.set("snapshot", snapshot)
host.state.set("refresh_done", state.refresh)
check(key("daemon-control:").props.text == "daemon.start", "observed stopped daemon switches to Start")

snapshot.updated_at = os.time() - 120
env.update()
local staleControl = key("daemon-control:")
check(staleControl.props.enabled == false and staleControl.props.tooltip == "daemon.refresh-required",
  "controls become disabled when the snapshot ages even without a state event")
staleControl.props.onClick()
check(#pending == 0, "a stale callback cannot issue a daemon command")
snapshot.updated_at = os.time()
host.state.set("error", "Snapshot unavailable")
check(key("daemon-control:").props.enabled == false, "a failed refresh disables controls even on a recent cached snapshot")
host.state.set("error", "")
primary.can_control = false
primary.control_error = "daemon.managed"
nord.can_control = false
host.state.set("snapshot", snapshot)
check(key("daemon-control:").props.tooltip == "daemon.managed" and key("daemon-control:").props.enabled == false,
  "desktop-owned daemons explain why their controls are disabled")
check(key("daemon-control:nord").props.tooltip == "daemon.cli-missing" and key("daemon-control:nord").props.enabled == false,
  "missing CLI disables the button with an actionable explanation")
primary.can_control = true
primary.control_error = nil
nord.can_control = true
host.state.set("snapshot", snapshot)
key("daemon-control:").props.onClick()
env.onClose()
local beforeControlReply = tree
reply({ ok = true, action = "start", profile = "" })
check(tree == beforeControlReply, "a lifecycle result after close refreshes state without rendering a closed panel")
env.onOpen()
check(key("daemon-control:").props.enabled == false, "reopening preserves pending state and cannot duplicate the command")
host.state.set("error", "Snapshot unavailable")
host.state.set("refresh_done", state.refresh)
check(key("daemon-control:").props.enabled == false and key("daemon-control:").props.text == "daemon.start",
  "a failed post-command refresh releases pending state but retains disabled controls and the observed status")
host.state.set("error", "")
primary.online = true
primary.status = "running"
primary.active = 2
snapshot.profiles = { primary }
env.onClose()

load(widgetSource)
env.update()
check(widgetText == "1/1", "bar shows running count and queue count")
snapshot.updated_at = os.time() - 120
env.update()
check(widgetText == "?!", "stale counts are marked unknown")

-- A failed subprocess never passes partial JSON to the UI.
local callbackResult = nil
host.pluginDir = function() return "/plugin path" end
host.json = { decode = function() return { ok = true } end }
host.runAsync = function(args, callback)
  check(args[2] == "/plugin path/bridge.py", "bridge paths are passed as argv")
  callback({ exitCode = 0, stdoutTruncated = true, stdout = "partial" })
  return true
end
local module = assert(loadstring(clientSource)); setfenv(module, env)
module().call({ "snapshot" }, function(data, err) callbackResult = { data = data, err = err } end)
check(callbackResult.data == nil and callbackResult.err ~= nil, "truncated helper output is an explicit error")
host.runAsync = function(args, callback, timeout)
  check(timeout == 60000 and args[3] == "daemon" and args[5] == "nord team", "control timeout and profile are passed directly to runAsync")
  callback({ timedOut = true, exitCode = -1, stdout = "" })
  return true
end
module().call({ "daemon", "--profile", "nord team", "--action", "start" },
  function(data, err) callbackResult = { data = data, err = err } end, 60000)
check(callbackResult.data == nil and callbackResult.err == "daemon.timeout", "a timed-out control returns a lifecycle-specific error")
host.tr = function(value) return value == "daemon.cli-missing" and "CLI unavailable" or value end
host.json.decode = function() return { error = "daemon.cli-missing" } end
host.runAsync = function(_, callback)
  callback({ exitCode = 0, stdout = "{}" })
  return true
end
module().call({ "daemon", "--profile", "", "--action", "start" },
  function(data, err) callbackResult = { data = data, err = err } end, 60000)
check(callbackResult.err == "CLI unavailable", "helper error codes are translated before display")

local serviceState = { snapshot = snapshot }
local serviceWatches = {}
local serviceWrites = {}
local serviceEnv = setmetatable({
  require = function() return bridgeClient end,
  noctalia = {
    tr = host.tr, getConfig = host.getConfig, setUpdateInterval = host.setUpdateInterval,
    state = {
      set = function(k, v) serviceState[k] = v; table.insert(serviceWrites, k) end,
      watch = function(k, callback) serviceWatches[k] = callback end,
    },
  },
}, { __index = getfenv() })
local service = assert(loadstring(serviceSource)); setfenv(service, serviceEnv)()
serviceEnv.update()
check(#pending == 1, "service never overlaps snapshot requests")
serviceWatches.refresh(100)
serviceWatches.refresh(101)
check(#pending == 1, "refreshes during an active snapshot are queued without overlap")
reply(nil, "offline")
check(serviceState.snapshot == snapshot and serviceState.error == "offline", "service retains last snapshot on failure")
check(serviceState.refresh_done == 0 and #pending == 1, "old snapshot completes its own token and starts the queued refresh")
reply(snapshot, nil)
check(serviceState.error == "", "successful retry clears connection error")
check(serviceState.refresh_done == 101 and #pending == 0, "queued refreshes coalesce to the latest token and complete once")
check(serviceWrites[#serviceWrites - 2] == "snapshot" and serviceWrites[#serviceWrites - 1] == "error"
  and serviceWrites[#serviceWrites] == "refresh_done", "refresh completion is published after its snapshot and error state")
print(tostring(checks) .. " UI checks passed")
