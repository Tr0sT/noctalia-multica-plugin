local client = require("./client.luau")
local busy = false
local refreshToken = 0
local refreshQueued = false

function update()
  if busy then return end
  busy = true
  local requestedToken = refreshToken
  client.call({ "snapshot" }, function(data, err)
    busy = false
    if data then
      noctalia.state.set("snapshot", data)
      noctalia.state.set("error", "")
    else
      noctalia.state.set("error", err)
    end
    noctalia.state.set("refresh_done", requestedToken)
    if refreshQueued then
      refreshQueued = false
      update()
    end
  end)
end

function onConfigChanged()
  noctalia.setUpdateInterval((noctalia.getConfig("poll_seconds") or 10) * 1000)
  update()
end

function onIpc(event)
  if event == "refresh" then update() end
end

noctalia.state.watch("refresh", function(token)
  refreshToken = token
  if busy then refreshQueued = true else update() end
end)
noctalia.setUpdateInterval((noctalia.getConfig("poll_seconds") or 10) * 1000)
update()
