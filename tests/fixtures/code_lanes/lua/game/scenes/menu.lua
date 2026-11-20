local vec = require("lib.vec")
local ui = require "game.ui"

local Menu = {}
Menu.__index = Menu

function Menu.new(items)
  local self = setmetatable({ items = items, cursor = vec.new(0, 0) }, Menu)
  return self
end

function Menu:move(dx)
  self.cursor = vec.add(self.cursor, vec.new(dx, 0))
  if vec.length(self.cursor) > #self.items then
    self.cursor = vec.new(0, 0)
  end
end

local function draw_item(item, selected)
  ui.text(item, selected and "> " or "  ")
end

function Menu:draw()
  for i, item in ipairs(self.items) do
    draw_item(item, i == self.cursor.x + 1)
  end
end

return Menu
