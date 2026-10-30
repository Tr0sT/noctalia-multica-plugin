local M = {}

function M.call(args, callback)
  local command = { "python3", noctalia.pluginDir() .. "/bridge.py" }
  for _, arg in ipairs(args) do table.insert(command, tostring(arg)) end
  local started = noctalia.runAsync(command, function(result)
    if result.timedOut or result.exitCode ~= 0 or result.stdoutTruncated then
      callback(nil, noctalia.tr("error.helper"))
      return
    end
    local data = noctalia.json.decode(result.stdout)
    if type(data) ~= "table" then
      callback(nil, noctalia.tr("error.response"))
    elseif data.error then
      callback(nil, data.error)
    else
      callback(data, nil)
    end
  end, 30000)
  if not started then callback(nil, noctalia.tr("error.helper")) end
end

return M
