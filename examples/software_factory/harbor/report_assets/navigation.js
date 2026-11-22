(() => {
  async function attach() {
    let destination = "/assets/factory-charts/index.html";
    try {
      const response = await fetch("/assets/factory-charts/index.json", { cache: "no-store" });
      if (response.ok) {
        const registry = await response.json();
        const selected = new URLSearchParams(location.search).getAll("job");
        const run = Object.values(registry.runs).find(
          (r) => selected.length && selected.every((j) => r.jobs.includes(j)),
        );
        if (run) destination = run.url;
      }
    } catch {}
    const link = document.getElementById("factory-bar-charts-link") || document.createElement("a");
    link.id = "factory-bar-charts-link";
    link.textContent = "Bar charts";
    link.href = destination;
    link.style.cssText =
      "position:fixed;bottom:20px;right:20px;z-index:1000;padding:10px 16px;border-radius:8px;background:#eea650;color:#111827;font:600 14px system-ui;text-decoration:none;box-shadow:0 4px 20px #0004";
    link.onpointerenter = attach;
    link.onfocus = attach;
    document.body.append(link);
  }
  if (document.readyState === "complete") setTimeout(attach, 300);
  else window.addEventListener("load", () => setTimeout(attach, 300), { once: true });
  window.addEventListener("popstate", attach);
})();
