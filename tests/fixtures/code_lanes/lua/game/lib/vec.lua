-- 2D vector helpers
local vec = {}

function vec.new(x, y)
  return { x = x or 0, y = y or 0 }
end

function vec.add(a, b)
  return vec.new(a.x + b.x, a.y + b.y)
end

function vec.length(v)
  return math.sqrt(v.x * v.x + v.y * v.y)
end

return vec
