// StrayWatch browser chat: same engine as WhatsApp.
(function () {
  const $ = (s) => document.querySelector(s);
  const msgs = $("#msgs"), helper = $("#helper"), traceEl = $("#trace");
  const APP = document.body.dataset.mode === "app";  // the installable app screen (/app): real use, no demo panels
  const DEMO_SPOT = { lat: 23.3725, lon: 85.3330, label: "Lalpur, Ranchi" };
  const RATU_RD = { lat: 23.3905, lon: 85.3050, label: "Ratu Road, Ranchi" };
  let traceAfter = 0, busy = false, helperAs = "demo:priya";
  const helperLog = { "demo:priya": [], "demo:arjun": [] };

  function esc(t) { return String(t).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }
  function fmt(t) { return esc(t).replace(/\*([^*\n]+)\*/g, "<b>$1</b>"); }

  function bubble(box, m, mine, asUser) {
    const b = document.createElement("div");
    b.className = "bubble " + (mine ? "me" : "bot");
    let html = "";
    if (m.image_url) html += `<img src="${esc(m.image_url.replace(/^https?:\/\/[^/]+/, ""))}" alt="photo">`;
    if (m.previewSrc) html += `<img src="${m.previewSrc}" alt="your photo">`;
    if (m.text) html += `<div>${fmt(m.text)}</div>`;
    if (m.link) html += `<a class="link" href="${esc(m.link.replace(/^https?:\/\/localhost:\d+/, ""))}" target="_blank" rel="noopener">${esc(m.link.replace(/^https?:\/\//, ""))}</a>`;
    const btns = [...(m.buttons || []), ...(m.options || [])];
    if (btns.length || m.ask_location) {
      html += `<div class="btns"></div>`;
    }
    b.innerHTML = html;
    const row = b.querySelector(".btns");
    btns.forEach((x) => {
      const el = document.createElement("button");
      el.type = "button"; el.textContent = x.title;
      el.onclick = () => { row.querySelectorAll("button").forEach((y) => (y.disabled = true)); send({ button: x.id, echo: x.title, asUser }); };
      row.appendChild(el);
    });
    if (m.ask_location && !asUser) {
      (APP ? [["📍 Share my location", "real"]] : [["📍 My location", "real"], ["📍 Demo spot: Lalpur", "demo"]]).forEach(([t, k]) => {
        const el = document.createElement("button"); el.type = "button"; el.textContent = t;
        el.onclick = () => shareLocation(k);
        row.appendChild(el);
      });
    }
    box.appendChild(b);
    box.scrollTop = box.scrollHeight;
  }

  function typing(on) {
    let t = msgs.querySelector(".typing");
    if (on && !t) { t = document.createElement("div"); t.className = "typing"; t.textContent = "StrayWatch is looking…"; msgs.appendChild(t); msgs.scrollTop = msgs.scrollHeight; }
    if (!on && t) t.remove();
  }

  async function send({ text = "", button = "", file = null, lat = null, lon = null, echo = null, asUser = "", previewSrc = null }) {
    if (asUser) return sendAs(asUser, button, echo);
    if (busy) return;
    busy = true;
    if (echo || text || file || lat) bubble(msgs, { text: echo || text || (lat ? "📍 Location shared" : ""), previewSrc }, true);
    const fd = new FormData();
    fd.append("text", text); fd.append("button", button);
    if (lat != null) { fd.append("lat", lat); fd.append("lon", lon); }
    if (file) fd.append("photo", file);
    typing(true);
    try {
      const r = await fetch("/api/chat", { method: "POST", body: fd });
      const j = await r.json();
      typing(false);
      (j.messages || []).forEach((m) => bubble(msgs, m, false));
    } catch (e) {
      typing(false);
      bubble(msgs, { text: "Network hiccup. Please try again. 🙏" }, false);
    }
    busy = false;
    pollTrace();
  }

  async function sendAs(asUser, button, echo) {
    const box = helper;
    helperLog[asUser].push({ mine: true, m: { text: echo } });
    if (asUser === helperAs) bubble(box, { text: echo }, true);
    const fd = new FormData(); fd.append("button", button); fd.append("as_user", asUser);
    const r = await fetch("/api/chat", { method: "POST", body: fd });
    const j = await r.json();
    (j.messages || []).forEach((m) => { helperLog[asUser].push({ mine: false, m }); if (asUser === helperAs) bubble(box, m, false, asUser); });
    pollInbox(); pollTrace();
  }

  function shareLocation(kind) {
    if (kind === "demo") return send({ lat: DEMO_SPOT.lat, lon: DEMO_SPOT.lon, echo: "📍 " + DEMO_SPOT.label });
    if (!navigator.geolocation) return send({ lat: DEMO_SPOT.lat, lon: DEMO_SPOT.lon, echo: "📍 " + DEMO_SPOT.label });
    navigator.geolocation.getCurrentPosition(
      (p) => send({ lat: p.coords.latitude, lon: p.coords.longitude, echo: "📍 My location" }),
      () => send({ lat: DEMO_SPOT.lat, lon: DEMO_SPOT.lon, echo: "📍 " + DEMO_SPOT.label + " (location blocked, used demo spot)" }),
      { timeout: 8000 });
  }

  $("#composer").addEventListener("submit", (e) => {
    e.preventDefault();
    const t = $("#text").value.trim();
    if (!t) return;
    $("#text").value = "";
    send({ text: t });
  });
  $("#file").addEventListener("change", (e) => {
    const f = e.target.files[0]; if (!f) return;
    send({ file: f, echo: "📷 Photo", previewSrc: URL.createObjectURL(f) });
    e.target.value = "";
  });
  $("#locbtn").addEventListener("click", () => shareLocation(APP || confirm("Share your real location?\n\nCancel = use the demo spot in Lalpur, Ranchi (where the sample animals live).") ? "real" : "demo"));

  // ---------- sample scenarios
  async function sampleFile(name) {
    const r = await fetch("/static/samples/" + name);
    const b = await r.blob();
    return new File([b], name, { type: "image/jpeg" });
  }
  async function ensureLang() {
    if (localStorage.getItem("sw_lang")) return;
    await send({ button: "lang:en", echo: "English" });
    try { localStorage.setItem("sw_lang", "1"); } catch (e) {}
  }
  const wait = () => new Promise((res) => { const i = setInterval(() => { if (!busy) { clearInterval(i); res(); } }, 150); });
  async function run(steps) { for (const s of steps) { await wait(); await s(); } }

  document.querySelectorAll("[data-sample]").forEach((btn) =>
    btn.addEventListener("click", async () => {
      const k = btn.dataset.sample;
      document.getElementById("try").scrollIntoView({ behavior: "smooth" });
      if (k === "reset") { msgs.innerHTML = ""; return send({ text: "menu", echo: "menu" }); }
      await ensureLang();
      if (k === "sos") {
        const f = await sampleFile("sos_bruno.jpg");
        await run([
          () => send({ button: "m:sos", echo: "Hurt animal: SOS" }),
          () => send({ file: f, text: "kutta ghayal hai, gaadi ne maara, pair se khoon aa raha hai", echo: "📷 kutta ghayal hai, gaadi ne maara, pair se khoon aa raha hai", previewSrc: URL.createObjectURL(f) }),
          () => send({ lat: DEMO_SPOT.lat + 0.0004, lon: DEMO_SPOT.lon + 0.0003, echo: "📍 Near Lalpur Chowk" }),
        ]);
      } else if (k === "register") {
        const f = await sampleFile("register_dog.jpg");
        await run([
          () => send({ button: "a:register", echo: "Register an animal" }),
          () => send({ file: f, echo: "📷 Photo", previewSrc: URL.createObjectURL(f) }),
          () => send({ text: "Sheru" }),
          () => send({ lat: DEMO_SPOT.lat - 0.002, lon: DEMO_SPOT.lon + 0.001, echo: "📍 Near my gate" }),
        ]);
      } else if (k === "checkin") {
        const f = await sampleFile("checkin_bruno.jpg");
        await run([
          () => send({ button: "a:checkin", echo: "Daily check-in" }),
          () => send({ file: f, lat: DEMO_SPOT.lat, lon: DEMO_SPOT.lon, echo: "📷 Feeding time, Lalpur", previewSrc: URL.createObjectURL(f) }),
        ]);
      } else if (k === "lost") {
        const f = await sampleFile("lost_cow.jpg");
        await run([
          () => send({ button: "l:lost", echo: "I lost my animal" }),
          () => send({ file: f, echo: "📷 Old photo of my cow", previewSrc: URL.createObjectURL(f) }),
          () => send({ text: "Nandini" }),
          () => send({ lat: RATU_RD.lat, lon: RATU_RD.lon, echo: "📍 " + RATU_RD.label }),
          () => send({ text: "yesterday evening" }),
        ]);
      }
    }));

  // ---------- helper phones
  document.querySelectorAll("[data-as]").forEach((b) =>
    b.addEventListener("click", () => {
      helperAs = b.dataset.as;
      document.querySelectorAll("[data-as]").forEach((x) => x.setAttribute("aria-pressed", x === b));
      helper.innerHTML = "";
      if (!helperLog[helperAs].length) helper.innerHTML = '<div class="typing">Alerts for this demo helper will appear here…</div>';
      helperLog[helperAs].forEach((x) => bubble(helper, x.m, x.mine, helperAs));
    }));

  async function pollInbox() {
    try {
      const mine = await (await fetch("/api/inbox")).json();
      (mine.messages || []).forEach((m) => bubble(msgs, m, false));
      if (!helper) return;
      for (const who of Object.keys(helperLog)) {
        const j = await (await fetch("/api/inbox?as_user=" + encodeURIComponent(who))).json();
        (j.messages || []).forEach((m) => {
          helperLog[who].push({ mine: false, m });
          if (who === helperAs) {
            const t = helper.querySelector(".typing"); if (t) t.remove();
            bubble(helper, m, false, who);
          } else {
            const tab = document.querySelector(`[data-as="${who}"]`); if (tab && !tab.textContent.includes("●")) tab.textContent += " ●";
          }
        });
      }
    } catch (e) {}
  }
  async function pollTrace() {
    if (!traceEl) return;
    try {
      const j = await (await fetch("/api/trace?after=" + traceAfter)).json();
      if (j.steps.length && traceEl.querySelector(".empty")) traceEl.innerHTML = "";
      j.steps.forEach((s) => {
        traceAfter = Math.max(traceAfter, s.id);
        const li = document.createElement("li");
        let detail = s.detail;
        if (s.model && detail.trim().startsWith("{")) {
          try { const o = JSON.parse(detail.replace(/```json|```/g, "")); detail = o.summary || o.reason || o.note || (o.intent ? "intent: " + o.intent : JSON.stringify(o).slice(0, 160)); } catch (e) { detail = detail.slice(0, 160); }
        }
        li.innerHTML = `<b>${esc(s.step)}</b>: ${esc(detail)} ${s.model ? `<span class="ms">· ${esc(s.model)} · ${(s.ms / 1000).toFixed(1)} s</span>` : ""}`;
        traceEl.appendChild(li);
      });
      traceEl.parentElement.scrollTop = traceEl.parentElement.scrollHeight;
    } catch (e) {}
  }
  setInterval(pollInbox, 3500);
  setInterval(pollTrace, 4000);

  // Arrived from a poster's "I've seen her" link?
  const seen = new URLSearchParams(location.search).get("seen");
  if (seen) { ensureLang().then(() => send({ text: "SEEN " + seen })); }
  else if (!msgs.children.length) {
    if (APP) send({ button: "menu" });  // opens with the language picker (first time) or the main menu
    else bubble(msgs, { text: "Namaste! 🐾 Pick a sample above, or say *hi* to start." }, false);
  }
})();
