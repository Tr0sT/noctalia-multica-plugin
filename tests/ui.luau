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
snapshot.profiles = { { name = "", device = "pc", status = "running", running = 1, online = true, agents = {}, errors = {} } }

local bridgeClient = { call = function(args, callback) table.insert(pending, { args = args, callback = callback }) end }
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

local serviceState = { snapshot = snapshot }
local serviceEnv = setmetatable({
  require = function() return bridgeClient end,
  noctalia = {
    tr = host.tr, getConfig = host.getConfig, setUpdateInterval = host.setUpdateInterval,
    state = {
      set = function(k, v) serviceState[k] = v end,
      watch = function() end,
    },
  },
}, { __index = getfenv() })
local service = assert(loadstring(serviceSource)); setfenv(service, serviceEnv)()
serviceEnv.update()
check(#pending == 1, "service never overlaps snapshot requests")
reply(nil, "offline")
check(serviceState.snapshot == snapshot and serviceState.error == "offline", "service retains last snapshot on failure")
serviceEnv.update()
reply(snapshot, nil)
check(serviceState.error == "", "successful retry clears connection error")
print(tostring(checks) .. " UI checks passed")
