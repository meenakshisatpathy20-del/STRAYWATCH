// StrayWatch map (Leaflet + OpenStreetMap). Locations are rounded to ~1 km by the server; nobody's home is shown.
window.swMap = async function (elId, opts = {}) {
  const el = document.getElementById(elId);
  if (!el || !window.L) { if (el) el.innerHTML = '<p class="note" style="padding:14px">The map could not load. Check your connection.</p>'; return; }
  let data;
  try { data = await (await fetch("/api/map")).json(); } catch (e) { el.innerHTML = '<p class="note" style="padding:14px">The map could not load.</p>'; return; }
  const map = L.map(el, { scrollWheelZoom: false }).setView(data.center, 12);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 17, attribution: "© OpenStreetMap contributors" }).addTo(map);
  const color = { HIGH: "#FF6B5B", CHECK: "#FFC21A", LOW: "#9AA3AE" };
  const word = { HIGH: "Urgent", CHECK: "Needs care", LOW: "Healthy · no case" };
  const esc = (t) => String(t ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const layers = { cases: L.layerGroup(), hot: L.layerGroup(), cams: L.layerGroup(), cattle: L.layerGroup() };
  // Several cases can share one ~1 km cell: spread them in a small circle so each can be tapped.
  const seen = {};
  data.cases.forEach((c) => {
    const k = c.lat + "," + c.lon, n = (seen[k] = (seen[k] || 0) + 1) - 1;
    const a = n * 2.4, r = n ? 0.0018 + 0.0005 * Math.floor(n / 6) : 0;
    const m = L.circleMarker([c.lat + r * Math.cos(a), c.lon + r * Math.sin(a)], {
      radius: c.level === "HIGH" ? 9 : 7, color: "#2B2340", weight: 2, fillColor: color[c.level] || "#FF6B5B",
      fillOpacity: c.status === "closed" || c.status === "cancelled" ? 0.35 : 0.95 });
    m.bindPopup(`<b>${esc(c.code)} · ${esc(c.species)}</b><br>${esc(word[c.level] || "")} · ${esc(c.status)}<br>${c.source === "camera" ? "🎥 camera" : "👤 citizen"} · ${esc(c.when)}${c.on_road ? "<br>🛣️ on the road" : ""}`);
    if (opts.onCase) m.on("click", () => opts.onCase(c.id));
    m.addTo(layers.cases);
  });
  data.hotspots.forEach((h) => L.circle([h.lat, h.lon], { radius: 450, color: "#C8322B", weight: 2, dashArray: "6 6", fillColor: "#FF8A7A", fillOpacity: 0.18 })
    .bindPopup(`<b>${esc(h.action)}</b><br>${esc(h.why)}`).addTo(layers.hot));
  data.cameras.forEach((c) => L.marker([c.lat, c.lon], { icon: L.divIcon({ className: "sw-cam", html: "🎥", iconSize: [26, 26] }) })
    .bindPopup(`<b>${esc(c.name)}</b><br>Camera AI: alerts only for an animal lying in a lane`).addTo(layers.cams));
  data.cattle.forEach((c) => L.circleMarker([c.lat, c.lon], { radius: 5 + Math.min(10, c.count * 2), color: "#4A90C2", weight: 1, fillColor: "#5BC0EB", fillOpacity: 0.35 })
    .bindPopup(`🐄 ${c.count} sighting${c.count > 1 ? "s" : ""} of cattle on the road (traffic risk, not a rescue)`).addTo(layers.cattle));
  Object.values(layers).forEach((g) => g.addTo(map));
  L.control.layers(null, { "Cases": layers.cases, "Injury hotspots": layers.hot, "Cameras": layers.cams, "Cattle on roads (traffic)": layers.cattle },
    { collapsed: window.innerWidth < 700 }).addTo(map);
  const pts = data.cases.map((c) => [c.lat, c.lon]).concat(data.cameras.map((c) => [c.lat, c.lon]));
  if (pts.length > 1) map.fitBounds(pts, { padding: [30, 30], maxZoom: 14 });
  window.swMapInstance = map;
  return map;
};
