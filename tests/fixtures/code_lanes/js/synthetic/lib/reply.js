// synthetic: CommonJS — prototype methods are `Reply::send`,
// `module.exports` names the module's default, `new Reply()` types the receiver
'use strict'
function Reply (res) { this.raw = res }
Reply.prototype.send = function (payload) { return this.raw.end(payload) }
Reply.prototype.code = function (c) { this.statusCode = c; return this }
function build (res) {
  const reply = new Reply(res)
  reply.code(200)
  return reply.send('ok')
}
module.exports = Reply
module.exports.build = build
