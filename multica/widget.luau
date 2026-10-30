local tr = noctalia.tr

function update()
  local data = noctalia.state.get("snapshot")
  local err = noctalia.state.get("error")
  barWidget.setGlyph("robot")
  if not data then
    barWidget.setText("…")
    barWidget.setTooltip(err or tr("loading"))
    return
  end
  local stale = os.time() - data.updated_at > math.max(45, (noctalia.getConfig("poll_seconds") or 10) * 3)
  local warning = stale or (err ~= nil and err ~= "") or #data.profiles == 0
  local online = 0
  local tooltip = { { "Multica", tostring(data.running) .. " " .. tr("running") .. " · " .. tostring(#data.queue) .. " " .. tr("queued") } }
  for _, p in ipairs(data.profiles) do
    if p.online then online = online + 1 end
    if #p.errors > 0 then warning = true end
    table.insert(tooltip, { p.name == "" and tr("default-profile") or p.name,
      #p.errors > 0 and table.concat(p.errors, "\n") or (p.device .. " · " .. tostring(p.running) .. " " .. tr("running")) })
  end
  local label = tostring(data.running)
  if #data.queue > 0 then label = label .. "/" .. tostring(#data.queue) end
  if data.waiting > 0 then label = label .. "⌛" end
  if online == 0 or stale then label = "?" end
  if warning then label = label .. "!" end
  barWidget.setText(label)
  barWidget.setGlyphColor(warning and "error" or (data.running > 0 and "primary" or "on_surface_variant"))
  if stale then table.insert(tooltip, { tr("connection"), tr("stale") }) end
  if err and err ~= "" then table.insert(tooltip, { tr("error"), err }) end
  barWidget.setTooltip(tooltip)
end

function onClick() noctalia.togglePanel("tr0st/multica:panel") end
function onRightClick() noctalia.openSettings() end

noctalia.state.watch("snapshot", function() update() end)
noctalia.state.watch("error", function() update() end)
noctalia.setUpdateInterval(5000)
update()
