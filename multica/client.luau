local M = {}

function M.call(args, callback, timeout)
  local command = { "python3", noctalia.pluginDir() .. "/bridge.py" }
  for _, arg in ipairs(args) do table.insert(command, tostring(arg)) end
  local control = args[1] == "daemon"
  local started = noctalia.runAsync(command, function(result)
    if result.timedOut or result.exitCode ~= 0 or result.stdoutTruncated then
      callback(nil, noctalia.tr(control and (result.timedOut and "daemon.timeout" or "daemon.command-failed") or "error.helper"))
      return
    end
    local data = noctalia.json.decode(result.stdout)
    if type(data) ~= "table" then
      callback(nil, noctalia.tr("error.response"))
    elseif data.error then
      callback(nil, type(data.error) == "string" and noctalia.tr(data.error) or noctalia.tr("error.response"))
    else
      callback(data, nil)
    end
  end, timeout or 30000)
  if not started then callback(nil, noctalia.tr(control and "daemon.command-failed" or "error.helper")) end
end

return M
