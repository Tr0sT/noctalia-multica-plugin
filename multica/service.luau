local client = require("./client.luau")
local busy = false

function update()
  if busy then return end
  busy = true
  client.call({ "snapshot" }, function(data, err)
    busy = false
    if data then
      noctalia.state.set("snapshot", data)
      noctalia.state.set("error", "")
    else
      noctalia.state.set("error", err)
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

noctalia.state.watch("refresh", function() update() end)
noctalia.setUpdateInterval((noctalia.getConfig("poll_seconds") or 10) * 1000)
update()
