local client = require("./client.luau")
local tr = noctalia.tr
local opened = false
local selected = nil
local messages = {}
local hasOlder = false
local busy = false
local logError = nil
local generation = 0
local lastPoll = 0
local scrollRev = 0
local showRecent = false
local followLive = true
local nextSeq = nil
local daemonActions = {}
local render
local loadLogs

local function label(text, color, size)
  return ui.label({ text = text, color = color or "on_surface", fontSize = size or 13 })
end

local function profileName(name)
  return name == "" and tr("default-profile") or name
end

local function statusText(status)
  local key = "status." .. status
  local translated = tr(key)
  return translated == key and status or translated
end

local function refresh()
  local token = math.max(noctalia.nowMs(), (noctalia.state.get("refresh") or 0) + 1)
  noctalia.state.set("refresh", token)
  return token
end

local function staleSnapshot(data)
  return not data or os.time() - data.updated_at > math.max(45, (noctalia.getConfig("poll_seconds") or 10) * 3)
end

local function controlUnavailable(data, profile)
  local err = noctalia.state.get("error")
  if staleSnapshot(data) or (err and err ~= "") then return tr("daemon.refresh-required") end
  if not profile.can_control then return tr(profile.control_error or "daemon.cli-missing") end
  return nil
end

local function controlDaemon(name, action, allowActive)
  local data = noctalia.state.get("snapshot")
  local profile = nil
  for _, p in ipairs(data and data.profiles or {}) do
    if p.name == name then profile = p; break end
  end
  local previous = daemonActions[name]
  if not profile or controlUnavailable(data, profile) or (previous and previous.pending) then return end
  if previous and previous.confirm and not allowActive then return end
  if (action == "stop") ~= profile.online then render(); return end
  if action == "stop" and not allowActive and (profile.active or 0) > 0 then
    daemonActions[name] = { action = action, confirm = profile.active }
    render()
    return
  end

  local operation = { action = action, pending = true }
  daemonActions[name] = operation
  local args = { "daemon", "--profile", name, "--action", action }
  if allowActive then table.insert(args, "--allow-active") end
  render()
  client.call(args, function(result, err)
    if result and result.confirmation_required and type(result.active) == "number" and result.active > 0 then
      operation.pending = false
      if opened then operation.confirm = result.active else daemonActions[name] = nil end
    else
      if not err and (not result or result.ok ~= true or result.action ~= action or result.profile ~= name) then
        err = tr("error.response")
      end
      operation.error = err
      -- A command (including a timeout) does not establish the daemon's current state.
      operation.refreshToken = math.max(noctalia.nowMs(), (noctalia.state.get("refresh") or 0) + 1)
      noctalia.state.set("refresh", operation.refreshToken)
    end
    render()
  end, 60000)
end

local function openTask(task)
  if task.restricted then return end
  selected = task
  messages = {}
  nextSeq = nil
  followLive = true
  hasOlder = false
  logError = nil
  busy = false
  generation = generation + 1
  scrollRev = scrollRev + 1
  render()
  loadLogs("initial")
end

loadLogs = function(mode)
  if not opened or not selected or busy then return end
  if mode == "older" then followLive = false end
  if nextSeq == nil then mode = "initial" end
  busy = true
  lastPoll = noctalia.nowMs()
  local requestGeneration = generation
  local args = { "logs", "--profile", selected.profile, "--workspace", selected.workspace_id, "--task", selected.id }
  if mode == "older" and #messages > 0 then
    table.insert(args, "--before")
    table.insert(args, messages[1].seq)
  elseif mode == "newer" and nextSeq ~= nil then
    table.insert(args, "--since")
    table.insert(args, nextSeq)
  end
  client.call(args, function(data, err)
    if requestGeneration ~= generation or not opened then return end
    busy = false
    logError = err
    if data then
      local seen = {}
      for _, m in ipairs(messages) do seen[m.seq] = true end
      for _, m in ipairs(data.messages) do
        if not seen[m.seq] then table.insert(messages, m); seen[m.seq] = true end
      end
      table.sort(messages, function(a, b) return a.seq < b.seq end)
      if mode ~= "newer" then hasOlder = data.has_more end
      if mode ~= "older" then nextSeq = data.last_seq or nextSeq or 0 end
      -- Keep native controls bounded; history mode pauses live following until Latest.
      while #messages > 300 do
        if mode == "older" then
          table.remove(messages)
        else
          table.remove(messages, 1)
          hasOlder = true
        end
      end
    end
    render()
    if data and mode == "newer" and data.has_more then loadLogs("newer") end
  end)
end

local function taskCard(task)
  local running = task.status == "running"
  local title = task.restricted and tr("restricted-task") or ((task.identifier ~= "" and task.identifier .. " · " or "") .. task.title)
  return ui.column({ key = task.key, gap = 5, padding = 12, radius = 10,
    fill = "surface_container", border = running and "primary" or "outline_variant", borderWidth = 1,
    onClick = not task.restricted and function() openTask(task) end or nil,
  }, {
    ui.row({ gap = 8, align = "center" }, {
      ui.glyph({ name = running and "player-play" or (task.status == "failed" and "alert-circle" or "robot"), size = 17,
        color = running and "primary" or "on_surface_variant" }),
      ui.label({ text = title, flexGrow = 1, maxLines = 2, fontSize = 14, fontWeight = "medium" }),
      label(statusText(task.status), running and "primary" or "on_surface_variant", 11),
    }),
    label(task.agent .. " · " .. task.workspace .. " · " .. profileName(task.profile), "on_surface_variant", 11),
    task.restricted and label(tr("restricted-task-hint"), "on_surface_variant", 11) or ui.row({}, {}),
    (task.completed_at_local or "") ~= "" and label(task.completed_at_local, "on_surface_variant", 11) or ui.row({}, {}),
    task.detail_error ~= "" and label(task.detail_error, "error", 11) or ui.row({}, {}),
  })
end

local function overview(data)
  if not data then return label(tr("loading"), "on_surface_variant") end
  local children = {}
  local incomplete = #data.profiles == 0
  if #data.profiles == 0 then table.insert(children, label(tr("no-profiles"), "error")) end
  for _, p in ipairs(data.profiles) do
    local agentNames = {}
    for _, a in ipairs(p.agents) do table.insert(agentNames, a.name .. " · " .. a.workspace) end
    local operation = daemonActions[p.name]
    local pending = operation and operation.pending
    local action = p.online and "stop" or "start"
    local unavailable = controlUnavailable(data, p)
    local state = p.online and (p.status == "starting" and tr("starting") or tr("online")) or tr("offline")
    local parts = {
      ui.row({ gap = 8, align = "center" }, { ui.label({ text = profileName(p.name), fontSize = 13, maxLines = 1, flexGrow = 1 }),
        label(state, p.online and "primary" or "error", 11),
        ui.button({ key = "daemon-control:" .. p.name,
          text = tr(pending and (operation.action == "start" and "daemon.starting" or "daemon.stopping") or "daemon." .. action),
          glyph = action == "stop" and "player-pause" or "player-play", variant = "ghost", controlSize = "sm",
          enabled = unavailable == nil and not pending and not (operation and operation.confirm),
          tooltip = unavailable or tr(pending and "daemon.checking" or "daemon." .. action .. "-tooltip"),
          onClick = function() controlDaemon(p.name, action, false) end,
        }),
      }),
      label(#agentNames > 0 and table.concat(agentNames, "\n") or tr(#p.errors > 0 and "incomplete" or "no-agents"), "on_surface_variant", 11),
    }
    if operation and operation.confirm then
      table.insert(parts, label(tr("daemon.stop-warning") .. " " .. tostring(operation.confirm), "error", 12))
      table.insert(parts, ui.row({ gap = 8 }, {
        ui.spacer({ flexGrow = 1 }),
        ui.button({ key = "daemon-cancel:" .. p.name, text = tr("daemon.cancel"), variant = "ghost", controlSize = "sm",
          onClick = function() daemonActions[p.name] = nil; render() end }),
        ui.button({ key = "daemon-confirm:" .. p.name, text = tr("daemon.confirm-stop"), glyph = "player-pause",
          variant = "ghost", controlSize = "sm", enabled = unavailable == nil and p.online,
          onClick = function() controlDaemon(p.name, "stop", true) end }),
      }))
    end
    if operation and operation.error then table.insert(parts, label(operation.error, "error", 12)) end
    for _, err in ipairs(p.errors) do incomplete = true; table.insert(parts, label(err, "error", 12)) end
    table.insert(children, ui.column({ gap = 4, paddingV = 4 }, parts))
  end
  table.insert(children, label(tr("active") .. " · " .. tostring(data.running), "primary", 16))
  if #data.active == 0 then
    table.insert(children, label((incomplete or data.running > 0) and tr("incomplete") or tr("idle"), "on_surface_variant"))
  else
    local reported = 0
    for _, task in ipairs(data.active) do
      if task.status == "running" then reported = reported + 1 end
      table.insert(children, taskCard(task))
    end
    if reported ~= data.running then table.insert(children, label(tr("syncing"), "on_surface_variant", 11)) end
  end
  table.insert(children, label(tr("queue") .. " · " .. tostring(#data.queue), "on_surface", 16))
  if #data.queue == 0 then table.insert(children, label(incomplete and tr("incomplete") or tr("queue-empty"), "on_surface_variant")) end
  for _, task in ipairs(data.queue) do table.insert(children, taskCard(task)) end
  if #data.recent > 0 then
    table.insert(children, ui.button({ text = tr("recent") .. " · " .. tostring(#data.recent),
      glyph = showRecent and "chevron-up" or "chevron-down", variant = "ghost", contentAlign = "start",
      onClick = function() showRecent = not showRecent; render() end }))
    if showRecent then for _, task in ipairs(data.recent) do table.insert(children, taskCard(task)) end end
  end
  return ui.scroll({ key = "overview", flexGrow = 1, gap = 10, align = "stretch" }, children)
end

local function logView()
  local nodes = {}
  if hasOlder then table.insert(nodes, ui.button({ text = tr("older"), variant = "ghost", enabled = not busy,
    onClick = function() loadLogs("older") end })) end
  local blocks = {}
  for _, m in ipairs(messages) do
    local last = blocks[#blocks]
    if last and last.type == m.type and last.lastSeq + 1 == m.seq and not last.truncated then
      last.text = last.text .. m.text
      last.lastSeq = m.seq
      last.truncated = m.truncated
    else
      table.insert(blocks, { seq = m.seq, lastSeq = m.seq, type = m.type, text = m.text,
        created_at_local = m.created_at_local or "", truncated = m.truncated })
    end
  end
  for _, m in ipairs(blocks) do
    local content = m.text ~= "" and m.text or "…"
    local title = (m.created_at_local ~= "" and m.created_at_local .. " · " or "")
      .. tr("message." .. m.type)
    local parts = {
      label(title, "primary", 11), label(content, "on_surface", 13),
    }
    if m.truncated then table.insert(parts, label(tr("truncated"), "error", 11)) end
    table.insert(nodes, ui.column({ key = "message-" .. tostring(m.seq), gap = 5, padding = 10,
      radius = 8, fill = "surface_container", align = "stretch" }, parts))
  end
  if #messages == 0 then table.insert(nodes, label(busy and tr("loading") or tr("no-messages"), "on_surface_variant")) end
  local heading = (selected.identifier ~= "" and selected.identifier .. " · " or "") .. selected.title
  return ui.column({ flexGrow = 1, gap = 10, align = "stretch" }, {
    ui.row({ gap = 8, align = "center" }, {
      ui.button({ glyph = "arrow-left", tooltip = tr("back"), variant = "ghost", onClick = function()
        generation = generation + 1; selected = nil; busy = false; render()
      end }),
      ui.label({ text = heading, maxLines = 2, flexGrow = 1, fontSize = 15, fontWeight = "medium" }),
      ui.button({ glyph = "external-link", tooltip = tr("open-multica"), variant = "ghost", onClick = function()
        noctalia.runAsync({ "xdg-open", selected.url })
      end }),
    }),
    label(selected.agent .. " · " .. selected.workspace .. " · " .. statusText(selected.status), "on_surface_variant", 12),
    logError and label(logError, "error", 12) or ui.row({}, {}),
    ui.scroll({ key = "log-" .. selected.key, flexGrow = 1, gap = 8, align = "stretch",
      stickToBottom = followLive, scrollToBottomRev = scrollRev }, nodes),
    ui.row({ gap = 8, align = "center" }, {
      label(tr(followLive and "log-updates" or "history-paused"), "on_surface_variant", 11),
      ui.spacer({ flexGrow = 1 }),
      ui.button({ text = tr("latest"), glyph = "arrow-down", controlSize = "sm", variant = "ghost",
        onClick = function() openTask(selected) end }),
    }),
  })
end

render = function()
  if not opened then return end
  local data = noctalia.state.get("snapshot")
  local err = noctalia.state.get("error")
  local stale = data and staleSnapshot(data)
  panel.render(ui.column({ flexGrow = 1, padding = 8, gap = 12, align = "stretch" }, {
    ui.row({ gap = 10, align = "center" }, {
      ui.glyph({ name = "robot", color = "primary", size = 23 }),
      ui.label({ text = "Multica", fontSize = 20, fontWeight = "bold", flexGrow = 1 }),
      ui.button({ glyph = "refresh", tooltip = tr("refresh"), variant = "ghost", onClick = function()
        refresh(); if selected then loadLogs("newer") end
      end }),
      ui.button({ glyph = "settings", tooltip = tr("settings"), variant = "ghost", onClick = function() noctalia.openSettings() end }),
      ui.button({ glyph = "x", tooltip = tr("close"), variant = "ghost", onClick = function() panel.close() end }),
    }),
    (stale or (err and err ~= "")) and label(stale and tr("stale") or err, "error", 12) or ui.row({}, {}),
    selected and logView() or overview(data),
  }))
end

function onOpen(context)
  opened = true
  selected = nil
  busy = false
  generation = generation + 1
  panel.setWantsSecondTicks(true)
  refresh()
  local data = noctalia.state.get("snapshot")
  if data and type(context) == "string" and context ~= "" then
    for _, list in ipairs({ data.active, data.queue, data.recent }) do
      for _, task in ipairs(list) do
        if task.key == context then openTask(task); return end
      end
    end
  end
  render()
end

function onClose()
  opened = false
  generation = generation + 1
  busy = false
  for name, operation in pairs(daemonActions) do
    if operation.confirm then daemonActions[name] = nil end
  end
end

function update()
  if selected and followLive and noctalia.nowMs() - lastPoll >= 3000 then loadLogs("newer") end
  if opened and not selected then render() end
end

noctalia.state.watch("snapshot", function(data)
  if selected then
    for _, list in ipairs({ data.active, data.queue, data.recent }) do
      for _, task in ipairs(list) do if task.key == selected.key then selected = task end end
    end
  end
  render()
end)
noctalia.state.watch("error", function() render() end)
noctalia.state.watch("refresh_done", function(token)
  for _, operation in pairs(daemonActions) do
    if operation.refreshToken and token >= operation.refreshToken then
      operation.pending = false
      operation.refreshToken = nil
    end
  end
  render()
end)
