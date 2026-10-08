// `exports.f()` is this module's f
exports.parse = function parse(s) {
  return s;
};
exports.run = () => exports.parse('x');
