// An IIFE at statement level is module scope
(function () {
  function Legacy() {}
  Legacy.prototype.render = function () {
    return draw();
  };
  function draw() {
    return 1;
  }
  window.Legacy = Legacy;
})();
