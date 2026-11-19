// StrayWatch 3D scenes (Three.js, toon style). Loaded only on capable devices; the page works the same without it.
import * as THREE from "/static/vendor/three/three.module.min.js";

const INK = 0x2b2340, SUN = 0xffd23f, SUN_SOFT = 0xffe9a8, CORAL = 0xff8a7a, MINT = 0x3bc9a3, SKY = 0x5bc0eb,
  PINK = 0xff8fa3, TAN = 0xf4b678, TAN_D = 0xc98b5b, WHITE = 0xffffff, ROAD = 0x4a4458;

// 3-step gradient = cartoon shading
const grad = (() => {
  const t = new THREE.DataTexture(new Uint8Array([90, 90, 90, 255, 175, 175, 175, 255, 255, 255, 255, 255]), 3, 1, THREE.RGBAFormat);
  t.minFilter = t.magFilter = THREE.NearestFilter; t.needsUpdate = true; return t;
})();
const toon = (c) => new THREE.MeshToonMaterial({ color: c, gradientMap: grad });
const outlineMat = new THREE.MeshBasicMaterial({ color: INK, side: THREE.BackSide });

// Doodle outline: a slightly bigger, inside-out black copy behind each shape.
function part(geo, color, { outline = 0.06, pos = [0, 0, 0], rot = [0, 0, 0], scale = [1, 1, 1] } = {}) {
  const g = new THREE.Group();
  const m = new THREE.Mesh(geo, toon(color));
  g.add(m);
  if (outline) { const o = new THREE.Mesh(geo, outlineMat); o.scale.setScalar(1 + outline); g.add(o); }
  g.position.set(...pos); g.rotation.set(...rot); g.scale.set(...scale);
  return g;
}

function cow() {
  const g = new THREE.Group();
  g.add(part(new THREE.CapsuleGeometry(0.55, 1.2, 6, 16), WHITE, { rot: [0, 0, Math.PI / 2], pos: [0, 1.05, 0] }));
  [[-0.5, 0.45], [0.5, 0.45], [-0.5, -0.45], [0.5, -0.45]].forEach(([x, z]) =>
    g.add(part(new THREE.CylinderGeometry(0.12, 0.1, 0.8, 10), WHITE, { pos: [x, 0.4, z * 0.6] })));
  [[0.1, 1.45, 0.3], [-0.4, 1.2, -0.45], [0.45, 0.95, 0.5]].forEach((p) =>
    g.add(part(new THREE.SphereGeometry(0.2, 12, 8), INK, { outline: 0, pos: p, scale: [1.2, 0.8, 0.5] })));
  const head = new THREE.Group(); head.position.set(1.15, 1.35, 0);
  head.add(part(new THREE.BoxGeometry(0.6, 0.62, 0.62), WHITE));
  head.add(part(new THREE.BoxGeometry(0.4, 0.36, 0.66), 0xffb3c1, { pos: [0.27, -0.2, 0] }));
  head.add(part(new THREE.ConeGeometry(0.07, 0.35, 8), 0xfff3d6, { pos: [-0.05, 0.42, 0.24], rot: [0.3, 0, 0] }));
  head.add(part(new THREE.ConeGeometry(0.07, 0.35, 8), 0xfff3d6, { pos: [-0.05, 0.42, -0.24], rot: [-0.3, 0, 0] }));
  [-0.14, 0.14].forEach((z) => head.add(part(new THREE.SphereGeometry(0.06, 8, 8), INK, { outline: 0, pos: [0.31, 0.1, z] })));
  g.add(head); g.userData.head = head;
  return g;
}

function dogLying() {
  const g = new THREE.Group();
  g.add(part(new THREE.CapsuleGeometry(0.32, 0.8, 6, 14), TAN, { rot: [0, 0, Math.PI / 2], pos: [0, 0.32, 0], scale: [1, 1, 0.85] }));
  const head = part(new THREE.SphereGeometry(0.33, 16, 12), TAN, { pos: [0.75, 0.38, 0] });
  head.add(part(new THREE.SphereGeometry(0.16, 10, 8), TAN_D, { pos: [0.02, 0.05, 0.3], scale: [0.6, 1.4, 0.4] }));
  head.add(part(new THREE.SphereGeometry(0.16, 10, 8), TAN_D, { pos: [0.02, 0.05, -0.3], scale: [0.6, 1.4, 0.4] }));
  head.add(part(new THREE.SphereGeometry(0.06, 8, 8), INK, { outline: 0, pos: [0.32, 0.02, 0] }));
  g.add(head);
  g.add(part(new THREE.CylinderGeometry(0.06, 0.04, 0.5, 8), TAN, { pos: [-0.75, 0.3, 0.1], rot: [0, 0, 1.2] }));
  g.add(part(new THREE.BoxGeometry(0.34, 0.12, 0.3), WHITE, { pos: [0.1, 0.62, 0], outline: 0.08 })); // bandage-like patch
  return g;
}

function pin(color) {
  const g = new THREE.Group();
  g.add(part(new THREE.SphereGeometry(0.32, 16, 12), color, { pos: [0, 1.05, 0] }));
  g.add(part(new THREE.ConeGeometry(0.22, 0.7, 16), color, { pos: [0, 0.55, 0], rot: [Math.PI, 0, 0] }));
  g.add(part(new THREE.SphereGeometry(0.13, 10, 8), WHITE, { outline: 0, pos: [0, 1.08, 0.24] }));
  return g;
}

function heartShape() {
  const s = new THREE.Shape(); s.moveTo(0, -0.35);
  s.bezierCurveTo(-0.55, 0, -0.45, 0.42, 0, 0.18); s.bezierCurveTo(0.45, 0.42, 0.55, 0, 0, -0.35);
  return new THREE.ExtrudeGeometry(s, { depth: 0.12, bevelEnabled: true, bevelSize: 0.04, bevelThickness: 0.04, bevelSegments: 2 });
}

function capable() {
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) return false;
  const c = navigator.connection; if (c && (c.saveData || /2g/.test(c.effectiveType || ""))) return false;
  if ((navigator.deviceMemory || 4) < 3 || (navigator.hardwareConcurrency || 4) < 4) return false;
  try { const t = document.createElement("canvas"); return !!(t.getContext("webgl2") || t.getContext("webgl")); } catch (e) { return false; }
}

function mount(container, build, fov = 38) {
  const r = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: "low-power" });
  r.setPixelRatio(Math.min(devicePixelRatio, 2)); r.outputColorSpace = THREE.SRGBColorSpace;
  const canvas = r.domElement; canvas.setAttribute("aria-hidden", "true"); canvas.style.display = "block";
  container.appendChild(canvas);
  const scene = new THREE.Scene();
  const cam = new THREE.PerspectiveCamera(fov, 1, 0.1, 100);
  scene.add(new THREE.HemisphereLight(0xffffff, 0xffe9c8, 1.4));
  const sun = new THREE.DirectionalLight(0xffffff, 1.6); sun.position.set(4, 8, 6); scene.add(sun);
  const tick = build(scene, cam);
  const size = () => { const w = container.clientWidth, h = container.clientHeight; r.setSize(w, h, false); cam.aspect = w / h; cam.updateProjectionMatrix(); };
  size(); new ResizeObserver(size).observe(container);
  let visible = true, last = performance.now();
  new IntersectionObserver((es) => { visible = es[0].isIntersecting; }).observe(container);
  const loop = (now) => {
    requestAnimationFrame(loop);
    if (!visible || document.hidden) { last = now; return; }
    const dt = Math.min(0.05, (now - last) / 1000); last = now;
    tick(dt, now / 1000); r.render(scene, cam);
  };
  requestAnimationFrame(loop);
  return { scene, cam, renderer: r };
}

// ---------------------------------------------------------------- overview hero
function hero(container) {
  const pointer = { x: 0, y: 0 };
  container.addEventListener("pointermove", (e) => { const b = container.getBoundingClientRect(); pointer.x = (e.clientX - b.left) / b.width - 0.5; pointer.y = (e.clientY - b.top) / b.height - 0.5; });
  mount(container, (scene, cam) => {
    cam.position.set(0, 5.8, 11.0); cam.lookAt(0, 0.6, 0.3);
    const world = new THREE.Group(); scene.add(world);
    world.add(part(new THREE.CylinderGeometry(5.6, 5.6, 0.25, 48), SUN_SOFT, { pos: [0, -0.13, 0], outline: 0.015 }));
    world.add(part(new THREE.BoxGeometry(9.6, 0.06, 1.9), ROAD, { pos: [0, 0.03, 0.6], outline: 0.01 }));
    for (let i = -4; i <= 4; i++) world.add(part(new THREE.BoxGeometry(0.55, 0.02, 0.1), SUN, { outline: 0, pos: [i * 1.05, 0.08, 0.6] }));
    const c = cow(); c.position.set(-2.6, 0, -1.5); c.rotation.y = 0.5; c.scale.setScalar(0.8); world.add(c);
    const d = dogLying(); d.position.set(0.4, 0.05, 0.65); d.rotation.y = -0.3; world.add(d);
    // SOS: a red cross badge and pulse rings above the hurt dog
    const badge = new THREE.Group(); badge.position.set(0.5, 1.55, 0.65);
    badge.add(part(new THREE.CylinderGeometry(0.3, 0.3, 0.1, 24), WHITE, { rot: [Math.PI / 2, 0, 0] }));
    badge.add(part(new THREE.BoxGeometry(0.32, 0.09, 0.12), 0xc8322b, { outline: 0 }));
    badge.add(part(new THREE.BoxGeometry(0.09, 0.32, 0.12), 0xc8322b, { outline: 0 }));
    world.add(badge);
    const rings = [0, 1, 2].map(() => { const m = new THREE.Mesh(new THREE.RingGeometry(0.42, 0.5, 40), new THREE.MeshBasicMaterial({ color: CORAL, transparent: true, side: THREE.DoubleSide })); m.position.copy(badge.position); world.add(m); return m; });
    // Responders: rescuer and vet pins, with alert arcs
    const targets = [[3.4, 0, -1.4, MINT], [-3.6, 0, 1.9, SKY]].map(([x, y, z, col]) => { const p = pin(col); p.position.set(x, y, z); p.scale.setScalar(0.8); world.add(p); return p; });
    const arcs = targets.map((t) => {
      const a = badge.position.clone(), b = t.position.clone().add(new THREE.Vector3(0, 1, 0));
      const mid = a.clone().lerp(b, 0.5).add(new THREE.Vector3(0, 2, 0));
      const curve = new THREE.QuadraticBezierCurve3(a, mid, b);
      const line = new THREE.Line(new THREE.BufferGeometry().setFromPoints(curve.getPoints(40)), new THREE.LineDashedMaterial({ color: INK, dashSize: 0.18, gapSize: 0.12 }));
      line.computeLineDistances(); world.add(line);
      const dot = part(new THREE.SphereGeometry(0.13, 12, 10), SUN, { outline: 0.15 }); world.add(dot);
      return { curve, dot, target: t };
    });
    const cat = new THREE.Group(); cat.position.set(2.7, 0, 1.9);
    cat.add(part(new THREE.SphereGeometry(0.33, 14, 10), 0xb9c4d2, { pos: [0, 0.3, 0], scale: [1, 1.1, 0.9] }));
    cat.add(part(new THREE.SphereGeometry(0.25, 14, 10), 0xb9c4d2, { pos: [0, 0.75, 0.05] }));
    [-0.12, 0.12].forEach((x) => cat.add(part(new THREE.ConeGeometry(0.08, 0.2, 4), 0x9aa7b8, { pos: [x, 1.0, 0.05] })));
    world.add(cat);
    const hearts = [0, 1, 2].map((i) => { const h = part(heartShape(), PINK, { outline: 0.08 }); h.scale.setScalar(0.45); world.add(h); return { h, off: i / 3 }; });
    return (dt, t) => {
      world.rotation.y += ((pointer.x * 0.35) - world.rotation.y) * 0.05;
      world.rotation.x += ((pointer.y * 0.12) - world.rotation.x) * 0.05;
      c.userData.head.rotation.z = Math.sin(t * 1.3) * 0.08;
      badge.position.y = 1.55 + Math.sin(t * 3) * 0.06; badge.rotation.y = Math.sin(t * 1.5) * 0.4;
      rings.forEach((r, i) => { const k = (t * 0.7 + i / 3) % 1; r.position.copy(badge.position); r.scale.setScalar(0.6 + k * 2.4); r.material.opacity = 0.7 * (1 - k); r.lookAt(cam.position); });
      arcs.forEach((a, i) => {
        const k = ((t * 0.45 + i * 0.5) % 1.3);
        const u = Math.min(1, k);
        a.dot.position.copy(a.curve.getPoint(u)); a.dot.visible = k < 1;
        a.target.position.y = k > 1 && k < 1.15 ? Math.sin((k - 1) / 0.15 * Math.PI) * 0.35 : 0;
      });
      hearts.forEach(({ h, off }, i) => { const k = (t * 0.18 + off) % 1; h.position.set(-1.4 + i * 1.6 + Math.sin(t + i) * 0.2, 1.5 + k * 3, -0.8); h.rotation.y = t * 1.2 + i; h.children[0].material.opacity = 1; h.visible = k < 0.92; });
      cat.rotation.y = Math.sin(t * 0.6) * 0.3;
    };
  });
}

// ---------------------------------------------------------------- Agent Room network
function agentNet(container) {
  const AG = [["Camera", "🎥", MINT], ["Triage", "🔍", SKY], ["Rules", "⚖️", CORAL], ["Identify", "🐾", MINT], ["Dispatch", "📣", SUN], ["Watch", "⏱️", 0xb9c4d2], ["Hotspot", "📈", PINK]];
  const FLOW = [["Camera", "Triage"], ["Triage", "Rules"], ["Triage", "Identify"], ["Rules", "Dispatch"], ["Identify", "Dispatch"], ["Dispatch", "Watch"], ["Watch", "Dispatch"], ["Dispatch", "Hotspot"]];
  const label = (txt) => {
    const c = document.createElement("canvas"); c.width = 256; c.height = 96; const x = c.getContext("2d");
    x.fillStyle = "#fff"; x.strokeStyle = "#2B2340"; x.lineWidth = 6; x.beginPath(); x.roundRect(6, 14, 244, 68, 30); x.fill(); x.stroke();
    x.fillStyle = "#2B2340"; x.font = "700 36px Nunito, sans-serif"; x.textAlign = "center"; x.textBaseline = "middle"; x.fillText(txt, 128, 50);
    const s = new THREE.Sprite(new THREE.SpriteMaterial({ map: new THREE.CanvasTexture(c), depthTest: false })); s.scale.set(2.1, 0.8, 1); return s;
  };
  mount(container, (scene, cam) => {
    cam.position.set(0, 2.2, 6.2); cam.lookAt(0, 0.1, 0);
    const g = new THREE.Group(); scene.add(g);
    const nodes = {};
    AG.forEach(([name, icon, col], i) => {
      const a = (i / AG.length) * Math.PI * 2;
      const n = part(new THREE.IcosahedronGeometry(0.42, 1), col, { outline: 0.08 });
      n.position.set(Math.cos(a) * 3.3, Math.sin(a * 2) * 0.35, Math.sin(a) * 1.9);
      const l = label(icon + " " + name); l.position.set(0, 0.85, 0); n.add(l);
      g.add(n); nodes[name] = n;
    });
    FLOW.forEach(([a, b]) => g.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints([nodes[a].position, nodes[b].position]), new THREE.LineBasicMaterial({ color: INK, transparent: true, opacity: 0.35 }))));
    const packets = [];
    const send = (a, b, col = SUN) => { const p = part(new THREE.SphereGeometry(0.11, 10, 8), col, { outline: 0.2 }); g.add(p); packets.push({ p, a: nodes[a].position, b: nodes[b].position, k: 0 }); };
    let since = 0;
    window.addEventListener("sw-agents", (e) => {  // real activity from the Agent Room poller
      (e.detail || []).forEach((name) => { const out = FLOW.filter(([a]) => a === name); if (out.length) send(...out[Math.floor(Math.random() * out.length)], CORAL); if (nodes[name]) nodes[name].userData.pulse = 1; });
    });
    return (dt, t) => {
      g.rotation.y = Math.sin(t * 0.15) * 0.25;
      since += dt; if (since > 1.1) { since = 0; send(...FLOW[Math.floor(Math.random() * FLOW.length)]); }
      Object.values(nodes).forEach((n, i) => { n.position.y += Math.sin(t * 1.5 + i) * 0.002; const pz = n.userData.pulse || 0; n.scale.setScalar(1 + pz * 0.35); n.userData.pulse = Math.max(0, pz - dt * 1.5); n.rotation.y += dt * 0.4; });
      for (let i = packets.length - 1; i >= 0; i--) { const q = packets[i]; q.k += dt * 0.9; q.p.position.lerpVectors(q.a, q.b, Math.min(1, q.k)); if (q.k >= 1) { g.remove(q.p); packets.splice(i, 1); } }
    };
  }, 34);
}

window.swScenes = { capable, hero, agentNet };
if (capable()) {
  const h = document.getElementById("hero3d"); if (h) { h.classList.add("on"); hero(h); }
  const n = document.getElementById("agentnet"); if (n) { n.classList.add("on"); agentNet(n); }
}
