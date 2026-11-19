// StrayWatch browser chat: same engine as WhatsApp.
(function () {
  const $ = (s) => document.querySelector(s);
  const T = (k, en) => (window.swT ? window.swT(k) : en);
  const msgs = $("#msgs"), helper = $("#helper"), traceEl = $("#trace");
  const APP = document.body.dataset.mode === "app";  // the installable app screen (/app): real use, no demo panels
  const DEMO_SPOT = { lat: 23.3725, lon: 85.3330, label: "Lalpur, Ranchi" };
  const RATU_RD = { lat: 23.3905, lon: 85.3050, label: "Ratu Road, Ranchi" };
  let traceAfter = 0, busy = false;
  const helperLog = {};
  document.querySelectorAll("[data-as]").forEach((b) => { helperLog[b.dataset.as] = []; });
  let helperAs = (document.querySelector('[data-as][aria-pressed="true"]') || document.querySelector("[data-as]") || {}).dataset?.as || "demo:priya";

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
      el.type = "button"; el.textContent = x.title; el.dataset.id = x.id;
      // Choices lock after one tap; ongoing actions (video, end chat) stay usable.
      const persistent = /^v:(video|end|note):/.test(x.id);
      el.onclick = () => { if (!persistent) row.querySelectorAll("button").forEach((y) => (y.disabled = true)); else el.disabled = true; send({ button: x.id, echo: x.title, asUser }); };
      row.appendChild(el);
    });
    if (m.ask_location && !asUser) {
      (APP ? [[T("share_loc", "📍 Share my location"), "real"]] : [["📍 My location", "real"], ["📍 Demo spot: Lalpur", "demo"]]).forEach(([t, k]) => {
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
    if (on && !t) { t = document.createElement("div"); t.className = "typing"; t.textContent = T("typing", "StrayWatch is looking…"); msgs.appendChild(t); msgs.scrollTop = msgs.scrollHeight; }
    if (!on && t) t.remove();
  }

  // ---------- weak networks: shrink photos on the phone, and queue reports while offline
  async function shrink(file) {
    if (!file || !/^image\//.test(file.type) || file.size < 350000) return file;
    try {
      const bmp = await createImageBitmap(file);
      const k = Math.min(1, 1280 / Math.max(bmp.width, bmp.height));
      const c = document.createElement("canvas");
      c.width = Math.round(bmp.width * k); c.height = Math.round(bmp.height * k);
      c.getContext("2d").drawImage(bmp, 0, 0, c.width, c.height);
      const blob = await new Promise((r) => c.toBlob(r, "image/jpeg", 0.8));
      return blob && blob.size < file.size ? new File([blob], "photo.jpg", { type: "image/jpeg" }) : file;
    } catch (e) { return file; }
  }
  const QKEY = "sw_queue";
  const qload = () => { try { return JSON.parse(localStorage.getItem(QKEY) || "[]"); } catch (e) { return []; } };
  const qsave = (q) => { try { localStorage.setItem(QKEY, JSON.stringify(q)); return true; } catch (e) { return false; } };
  const toDataURL = (f) => new Promise((res) => { const r = new FileReader(); r.onload = () => res(r.result); r.onerror = () => res(null); r.readAsDataURL(f); });
  async function post(item) {
    const fd = new FormData();
    fd.append("text", item.text || ""); fd.append("button", item.button || "");
    if (!APP) fd.append("demo", "1");  // the demo page: sample responders only, kept apart from real reports
    if (item.lat != null) { fd.append("lat", item.lat); fd.append("lon", item.lon); }
    if (item.photo) fd.append("photo", item.photo instanceof File ? item.photo : new File([await (await fetch(item.photo)).blob()], "photo.jpg", { type: "image/jpeg" }));
    const r = await fetch("/api/chat", { method: "POST", body: fd });
    if (!r.ok && r.status >= 500) throw new Error("server " + r.status);
    return (await r.json()).messages || [];
  }
  let flushing = false;
  async function flushQueue() {
    if (flushing || busy || !navigator.onLine) return;
    let q = qload(); if (!q.length) return;
    flushing = true; busy = true; typing(true);
    try {
      while (q.length) {
        const msgsBack = await post(q[0]);
        q.shift(); qsave(q);
        msgsBack.forEach((m) => bubble(msgs, m, false));
      }
      bubble(msgs, { text: offlineMsg(1) }, false);
    } catch (e) { /* still offline; try again later */ }
    typing(false); busy = false; flushing = false; pollTrace();
  }
  window.addEventListener("online", flushQueue);
  setInterval(flushQueue, 20000);

  async function send({ text = "", button = "", file = null, lat = null, lon = null, echo = null, asUser = "", previewSrc = null }) {
    if (asUser) return sendAs(asUser, button, echo, file);
    while (busy) await new Promise((r) => setTimeout(r, 150));  // wait our turn; never drop what the person sent
    busy = true;
    if (button.startsWith("lang:")) { try { localStorage.setItem("sw_lang_code", button.slice(5)); } catch (e) {} window.dispatchEvent(new Event("sw-lang")); }
    if (echo || text || file || lat) bubble(msgs, { text: echo || text || (lat ? "📍 Location shared" : ""), previewSrc }, true);
    if (file) file = await shrink(file);
    const item = { text, button, lat, lon, photo: file };
    typing(true);
    try {
      if (qload().length) throw new Error("queue first");  // keep the order: older saved messages go first
      const back = await post(item);
      typing(false);
      back.forEach((m) => bubble(msgs, m, false));
    } catch (e) {
      typing(false);
      if (APP) {
        const q = qload();
        q.push({ ...item, photo: file ? await toDataURL(file) : null });
        if (qsave(q)) bubble(msgs, { text: offlineMsg(0) }, false);
        else bubble(msgs, { text: T("storage_full", "Network problem, and your phone's storage is full. Please try again, or call 1962. 🙏") }, false);
      } else bubble(msgs, { text: T("network_err", "Network problem. Please try again. 🙏") }, false);
    }
    busy = false;
    pollTrace();
    if (navigator.onLine && qload().length) setTimeout(flushQueue, 500);
  }

  async function sendAs(asUser, button, echo, file = null, text = "") {
    const box = helper;
    const mine = file ? { text: echo, previewSrc: URL.createObjectURL(file) } : { text: echo || text };
    helperLog[asUser].push({ mine: true, m: mine });
    if (asUser === helperAs) bubble(box, mine, true);
    const fd = new FormData(); fd.append("button", button || ""); fd.append("as_user", asUser); fd.append("text", text || "");
    if (file) fd.append("photo", file);
    const r = await fetch("/api/chat", { method: "POST", body: fd });
    const j = await r.json();
    (j.messages || []).forEach((m) => { helperLog[asUser].push({ mine: false, m }); if (asUser === helperAs) bubble(box, m, false, asUser); });
    pollInbox(); pollTrace();
  }

  function noLocation() {
    // Never invent a location in the real app: a rescuer would go to the wrong place. The server replies in the user's language.
    send({ button: "loc:denied", echo: "📍 ✕" });
    const t = $("#text"); if (t) t.focus();
  }
  // Messages shown while offline can't come from the server, so they live here in all 6 languages.
  const OFFLINE = {
    en: ["📶 No network right now. I've saved this on your phone and will send it by myself as soon as you're back online. You can keep adding details.", "📶 Back online: your saved message was sent."],
    hi: ["📶 अभी नेटवर्क नहीं है। मैंने इसे आपके फ़ोन में सेव कर लिया है और नेटवर्क आते ही अपने आप भेज दूँगा। आप और जानकारी जोड़ते रह सकते हैं।", "📶 नेटवर्क वापस आ गया: आपका सेव किया हुआ संदेश भेज दिया गया।"],
    bn: ["📶 এখন নেটওয়ার্ক নেই। আমি এটি আপনার ফোনে সেভ করেছি, নেটওয়ার্ক ফিরলেই নিজে থেকে পাঠিয়ে দেব। আপনি আরও তথ্য যোগ করতে পারেন।", "📶 নেটওয়ার্ক ফিরে এসেছে: আপনার সেভ করা বার্তা পাঠানো হয়েছে।"],
    ta: ["📶 இப்போது நெட்வொர்க் இல்லை. இதை உங்கள் போனில் சேமித்துள்ளேன்; நெட்வொர்க் வந்ததும் தானாக அனுப்புவேன். மேலும் விவரங்களைச் சேர்க்கலாம்.", "📶 நெட்வொர்க் திரும்ப வந்தது: சேமித்த செய்தி அனுப்பப்பட்டது."],
    te: ["📶 ఇప్పుడు నెట్‌వర్క్ లేదు. దీన్ని మీ ఫోన్‌లో సేవ్ చేశాను; నెట్‌వర్క్ రాగానే నేనే పంపిస్తాను. మీరు మరిన్ని వివరాలు జోడించవచ్చు.", "📶 నెట్‌వర్క్ తిరిగి వచ్చింది: సేవ్ చేసిన సందేశం పంపబడింది."],
    mr: ["📶 आत्ता नेटवर्क नाही. मी हे तुमच्या फोनमध्ये सेव्ह केले आहे आणि नेटवर्क येताच आपोआप पाठवेन. तुम्ही अजून माहिती जोडू शकता.", "📶 नेटवर्क परत आले: तुमचा सेव्ह केलेला संदेश पाठवला."],
  };
  const myLang = () => { try { return localStorage.getItem("sw_lang_code") || "en"; } catch (e) { return "en"; } };
  const offlineMsg = (i) => (OFFLINE[myLang()] || OFFLINE.en)[i];
  function shareLocation(kind) {
    if (kind === "demo") return send({ lat: DEMO_SPOT.lat, lon: DEMO_SPOT.lon, echo: "📍 " + DEMO_SPOT.label });
    const fallback = () => APP ? noLocation()
      : send({ lat: DEMO_SPOT.lat, lon: DEMO_SPOT.lon, echo: "📍 " + DEMO_SPOT.label + " (demo spot: your location was blocked)" });
    if (!navigator.geolocation) return fallback();
    navigator.geolocation.getCurrentPosition(
      (p) => send({ lat: p.coords.latitude, lon: p.coords.longitude, echo: "📍 My location" }),
      fallback, { timeout: 10000, enableHighAccuracy: true, maximumAge: 60000 });
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
      const stage = document.getElementById("try") || document.getElementById("stage");
      if (stage && window.innerWidth < 900) stage.scrollIntoView({ behavior: "smooth" });
      if (k === "reset") { msgs.innerHTML = ""; return send({ text: "menu", echo: "menu" }); }
      await ensureLang();
      if (k === "sos") {
        const f = await sampleFile("sos_bruno.jpg");
        await run([
          () => send({ button: "m:sos", echo: "Hurt animal: SOS" }),
          () => send({ file: f, text: "kutta ghayal hai, gaadi ne maara, pair se khoon aa raha hai", echo: "📷 kutta ghayal hai, gaadi ne maara, pair se khoon aa raha hai", previewSrc: URL.createObjectURL(f) }),
          () => send({ lat: DEMO_SPOT.lat + 0.0004, lon: DEMO_SPOT.lon + 0.0003, echo: "📍 Near Lalpur Chowk" }),
        ]);
      } else if (k === "lying") {
        const f = await sampleFile("lying_road.jpg");
        await run([
          () => send({ button: "m:sos", echo: "Hurt animal: SOS" }),
          () => send({ file: f, text: "do kutte sadak par pade hain, ek hil nahi raha", echo: "📷 do kutte sadak par pade hain, ek hil nahi raha", previewSrc: URL.createObjectURL(f) }),
          () => send({ lat: RATU_RD.lat + 0.0002, lon: RATU_RD.lon + 0.0002, echo: "📍 " + RATU_RD.label }),
        ]);
      } else if (k === "cow") {
        const f = await sampleFile("cow_road.jpg");
        await run([
          () => send({ button: "m:sos", echo: "Hurt animal: SOS" }),
          () => send({ file: f, echo: "📷 Cows walking on the road", previewSrc: URL.createObjectURL(f) }),
          () => send({ lat: RATU_RD.lat - 0.004, lon: RATU_RD.lon + 0.003, echo: "📍 Near Ratu Road" }),
        ]);
      } else if (k === "vet" || k === "vetcat" || k === "vetcow") {
        const S = {
          vet: ["sick_puppy.jpg", "v:sp:dog", "🐕 Dog", "mera puppy 2 din se ulti kar raha hai aur kuch kha nahi raha"],
          vetcat: ["sick_cat.jpg", "v:sp:cat", "🐈 Cat", "meri billi 3 din se kuch nahi kha rahi, bahut kamzor hai aur ek jagah padi rehti hai"],
          vetcow: ["thin_cow.jpg", "v:sp:cow", "🐄 Cow / buffalo", "हमारी गाय कई दिन से ठीक से नहीं खा रही, दुबली हो रही है, सड़क पर प्लास्टिक खाती है"],
        }[k];
        const f = await sampleFile(S[0]);
        const tap = async (prefix) => {  // the first matching button in the newest message
          await wait();
          const last = [...msgs.querySelectorAll("button[data-id]")].reverse().find((x) => x.dataset.id.startsWith(prefix) && !x.disabled);
          const b = last && [...last.parentElement.querySelectorAll("button[data-id]")].find((x) => x.dataset.id.startsWith(prefix));
          if (b) b.click();
        };
        await run([
          () => send({ button: "m:vet", echo: "🩺 Talk to a vet" }),
          () => send({ button: k === "vet" || k === "vetcat" ? "v:who:pet" : "v:who:stray", echo: k === "vetcow" ? "🐾 A street animal" : "🏠 My pet" }),
          () => send({ button: S[1], echo: S[2] }),
          () => send({ file: f, text: S[3], echo: "📷 " + S[3], previewSrc: URL.createObjectURL(f) }),
          () => tap("v:connect"),
          () => tap("v:pick"),
        ]);
        const vt = document.querySelector('[data-as="demo:vet2"]'); if (vt) setTimeout(() => vt.click(), 2500);
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
      b.textContent = b.textContent.replace(" ●", "");
      if (!helperLog[helperAs].length) helper.innerHTML = '<div class="typing">Alerts for this responder will appear here…</div>';
      helperLog[helperAs].forEach((x) => bubble(helper, x.m, x.mine, helperAs));
    }));

  const hc = document.getElementById("helperComposer");
  if (hc) hc.addEventListener("submit", (e) => {
    e.preventDefault(); const i = document.getElementById("helperText"); const t = i.value.trim(); if (!t) return;
    i.value = ""; sendAs(helperAs, "", "", null, t);
  });
  const hf = document.getElementById("helperFile");
  if (hf) hf.addEventListener("change", (e) => {
    const f = e.target.files[0]; if (!f) return;
    sendAs(helperAs, "", "📷 Proof photo", f); e.target.value = "";
  });

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
          try { const o = JSON.parse(detail.replace(/```json|```/g, "")); detail = o.summary || o.summary_for_vet || o.reason || o.note || o.problem || (o.intent ? "intent: " + o.intent : JSON.stringify(o).slice(0, 160)); }
          catch (e) { const m = detail.match(/"(summary|summary_for_vet|reason|note|problem)"\s*:\s*"([^"]+)/); detail = m ? m[2] : detail.slice(0, 160); }
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
  const GO = { vet: { button: "m:vet", echo: "🩺 Talk to a vet" }, food: { button: "v:food", echo: "🥣 Food & care" },
               sos: { button: "m:sos", echo: "🚨 Hurt animal: SOS" } };
  const go = new URLSearchParams(location.search).get("go");
  if (go && GO[go]) { try { sessionStorage.setItem("sw_go", go); } catch (e) {} }
  async function runGo() {
    let g = null; try { g = sessionStorage.getItem("sw_go"); sessionStorage.removeItem("sw_go"); } catch (e) {}
    if (g && GO[g]) await send(GO[g]);
  }
  window.addEventListener("sw-lang", () => setTimeout(runGo, 300));
  const seen = new URLSearchParams(location.search).get("seen");
  if (seen) { ensureLang().then(() => send({ text: "SEEN " + seen })); }
  else if (!msgs.children.length) {
    if (APP) {  // opens with the language picker (first time) or the main menu, then any deep link (?go=vet)
      let hasLang = false; try { hasLang = !!localStorage.getItem("sw_lang_code"); } catch (e) {}
      if (hasLang && go && GO[go]) runGo(); else send({ button: "menu" });
    }
    else bubble(msgs, { text: "Namaste! 🐾 Pick a sample above, or say *hi* to start." }, false);
  }
})();
