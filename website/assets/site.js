/* pbi-mcp website behaviour. Plain JavaScript, no dependencies, no tracking.
   site-data.js and demo-data.js are generated from the real servers by
   scripts/gen_site_assets.py. */
(() => {
  "use strict";

  const REPO = "rajdeepraoextras-dev/PBI-MCP-Server";
  const SITE = window.PBI_SITE || { version: "", stats: {}, families: [] };
  const DEMO = window.PBI_DEMO || { steps: [] };
  const STATS = Object.assign({}, SITE.stats);
  if (STATS.tools != null && STATS.write_tools != null) STATS.read_tools = STATS.tools - STATS.write_tools;

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const sleep = (ms) => new Promise((r) => setTimeout(r, reduced ? 0 : ms));
  const fmt = (n) => Number(n).toLocaleString("en-US");

  function h(tag, attrs, ...kids) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (v === true) node.setAttribute(k, "");
      else if (v !== false && v != null) node.setAttribute(k, v);
    }
    for (const kid of kids) node.append(kid);
    return node;
  }

  /* ---------- theme ---------- */
  const root = document.documentElement;
  $("#theme-toggle").addEventListener("click", () => {
    const next = root.getAttribute("data-theme") === "light" ? "dark" : "light";
    root.setAttribute("data-theme", next);
    try { localStorage.setItem("pbi-theme", next); } catch (e) { /* storage may be blocked */ }
  });

  /* ---------- numbers: fill, then count up when scrolled into view ---------- */
  const counters = $$("[data-stat]").map((node) => {
    const value = STATS[node.getAttribute("data-stat")];
    if (value == null) { node.textContent = "?"; return null; }
    node.setAttribute("data-target", value);
    node.textContent = reduced || !("IntersectionObserver" in window) ? fmt(value) : "0";
    return node;
  }).filter(Boolean);

  if (!reduced && "IntersectionObserver" in window) {
    const io = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        const node = entry.target;
        io.unobserve(node);
        const target = +node.getAttribute("data-target");
        const ms = target > 500 ? 1400 : 900;
        const t0 = performance.now();
        const tick = (t) => {
          const p = Math.min(1, (t - t0) / ms);
          node.textContent = fmt(Math.round(target * (1 - Math.pow(1 - p, 3))));
          if (p < 1) requestAnimationFrame(tick);
        };
        requestAnimationFrame(tick);
      });
    }, { threshold: 0.4 });
    counters.forEach((n) => io.observe(n));
  }

  const per = STATS.per_server || {};
  const perServer = $("#per-server");
  if (perServer && per.model) perServer.textContent = `${per.model} model · ${per.report} report · ${per.service} cloud`;
  const foot = $("#tools-foot");
  if (foot && per.model) foot.textContent = `${per.model} in the model server, ${per.report} in the report server, ${per.service} in the optional cloud server`;
  if (STATS.python_min) { $("#py-min").textContent = STATS.python_min; $("#py-max").textContent = STATS.python_max; }
  const fv = $("#foot-version");
  if (fv && SITE.version) fv.textContent = "v" + SITE.version;

  /* ---------- band of real tool names ---------- */
  const band = $("#band");
  if (band) {
    const names = SITE.families.flatMap((f) => f.tools.map((t) => t.name));
    names.concat(names).forEach((n) => band.append(h("span", { text: n })));
  }

  /* ---------- downloads ---------- */
  const PLATFORMS = {
    win: { label: "Windows", file: "pbi-mcp-standalone-win32-amd64.plugin" },
    mac: { label: "macOS (Apple silicon)", file: "pbi-mcp-standalone-darwin-arm64.plugin" },
    linux: { label: "Linux", file: "pbi-mcp-standalone-linux-x86_64.plugin" },
  };
  const EXTRAS = [
    { label: "Source plugin, needs Python 3.11 or newer", file: "pbi-mcp.plugin" },
    { label: "Claude Desktop extension: report", file: "pbi-mcp-report.mcpb" },
    { label: "Claude Desktop extension: model", file: "pbi-mcp-model.mcpb" },
    { label: "SHA-256 checksums", file: "SHA256SUMS.txt" },
  ];
  const detectOS = () => {
    const p = (navigator.userAgentData && navigator.userAgentData.platform) || navigator.platform || "";
    const ua = navigator.userAgent || "";
    if (/android|iphone|ipad/i.test(ua)) return "other";
    if (/win/i.test(p) || /Windows/.test(ua)) return "win";
    if (/mac/i.test(p) || /Mac OS X/.test(ua)) return "mac";
    if (/linux|x11/i.test(p) || /Linux/.test(ua)) return "linux";
    return "other";
  };
  const fmtSize = (bytes) => !bytes ? "" : bytes < 1048576 ? Math.round(bytes / 1024) + " KB"
    : (bytes / 1048576).toFixed(bytes > 5e7 ? 0 : 1) + " MB";

  async function latestRelease() {
    try {
      const res = await fetch(`https://api.github.com/repos/${REPO}/releases/latest`,
        { headers: { Accept: "application/vnd.github+json" } });
      return res.ok ? await res.json() : null;
    } catch (e) { return null; }
  }

  async function setupDownloads() {
    const os = detectOS();
    const rel = await latestRelease();
    const assets = {};
    if (rel && Array.isArray(rel.assets)) rel.assets.forEach((a) => { assets[a.name] = a; });
    const known = Object.keys(assets).length > 0;
    const version = rel && rel.tag_name ? rel.tag_name : (SITE.version ? "v" + SITE.version : "");
    const urlFor = (file) => (assets[file] && assets[file].browser_download_url) ||
      `https://github.com/${REPO}/releases/latest/download/${file}`;

    const pick = PLATFORMS[os];
    let label, meta, href;
    if (pick && (!known || assets[pick.file])) {
      label = "Download for " + pick.label;
      meta = [version, fmtSize(assets[pick.file] && assets[pick.file].size)].filter(Boolean).join(" · ");
      href = urlFor(pick.file);
    } else {
      label = "Get the latest release";
      meta = version || "all downloads";
      href = `https://github.com/${REPO}/releases/latest`;
    }
    [[$("#dl-primary"), "#dl-label", "#dl-meta"], [$("#hero-dl"), "#hero-dl-label", "#hero-dl-meta"]].forEach(([a, l, m]) => {
      a.href = href; $(l, a).textContent = label; $(m, a).textContent = meta;
    });

    const note = $("#dl-note");
    if (os === "mac") note.textContent = "The macOS build is for Apple silicon. On an Intel Mac, use the source plugin or install from source.";
    else if (os === "other") note.textContent = "Pick the file that matches your computer below.";
    else note.textContent = "A self-contained build. You do not need Python.";

    const list = $("#dl-others");
    list.textContent = "";
    Object.entries(PLATFORMS)
      .filter(([k, v]) => k !== os && (!known || assets[v.file]))
      .map(([, v]) => ({ label: v.label + " build", file: v.file }))
      .concat(EXTRAS.filter((x) => !known || assets[x.file]))
      .forEach((row) => {
        list.append(h("li", {}, h("a", { href: urlFor(row.file) },
          h("b", { text: row.label }), h("span", { text: fmtSize(assets[row.file] && assets[row.file].size) || row.file }))));
      });
  }
  setupDownloads();

  /* ---------- preview, apply, undo ---------- */
  const STEP_TEXT = {
    preview: "See the exact diff. Nothing is written.",
    apply: "The same call without dry_run. Now it writes the file.",
    undo: "One call restores the file, byte for byte.",
  };
  const out = $("#term-out"), stepsBox = $("#steps");
  let termRun = 0;

  const esc = (s) => s.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
  // colour key=value pairs first, then the leading function name, so the two never collide
  const callHtml = (src) => "&gt; " + src.split("\n").map((line) => esc(line)
    .replace(/(\w+)=("[^"]*"|true|false)/g, (_, k, v) => `<span class="t-key">${k}</span>=<span class="t-str">${v}</span>`)
    .replace(/^(pbi_\w+)/, '<span class="t-call">$1</span>')).join("\n  ");
  const flip = (line) => {
    if (line.startsWith("+++") || line.startsWith("---")) return line;
    if (line.startsWith("@@")) {
      const m = line.match(/^@@ -(\d+,\d+) \+(\d+,\d+) @@(.*)$/);
      return m ? `@@ -${m[2]} +${m[1]} @@${m[3]}` : line;
    }
    if (line.startsWith("+")) return "-" + line.slice(1);
    if (line.startsWith("-")) return "+" + line.slice(1);
    return line;
  };
  const diffHtml = (diff, inverted) => diff.split("\n").filter(Boolean).map((raw) => {
    const line = inverted ? flip(raw) : raw;
    let cls = "t-dim";
    if (line.startsWith("@@")) cls = "t-hunk";
    else if (line.startsWith("+++") || line.startsWith("---")) cls = "t-dim";
    else if (line.startsWith("+")) cls = "t-add";
    else if (line.startsWith("-")) cls = "t-del";
    return `<span class="${cls}">${esc(line.replace(/\t/g, "    "))}</span>`;
  }).join("\n");
  const json = (o) => esc(JSON.stringify(o, null, 2))
    .replace(/("[\w ]+")(:)/g, '<span class="t-key">$1</span>$2')
    .replace(/: ("[^"]*")/g, ': <span class="t-str">$1</span>');

  function stepBody(step) {
    const o = step.output || {};
    if (step.id === "preview") return json({ dry_run: o.dry_run, result: o.result, changes: o.changes }) + "\n\n" + diffHtml(o.diff || "", false);
    if (step.id === "undo") {
      const prev = (DEMO.steps[0] && DEMO.steps[0].output && DEMO.steps[0].output.diff) || "";
      return json(o) + '\n\n<span class="t-dim"># what the undo put back:</span>\n' + diffHtml(prev, true);
    }
    return json(o);
  }

  async function showStep(i, autoplay) {
    const step = DEMO.steps[i];
    if (!step) return;
    const me = ++termRun;
    $$(".step", stepsBox).forEach((b, k) => b.setAttribute("aria-selected", String(k === i)));
    if (!reduced && autoplay !== false) {
      let typed = "";
      for (const ch of step.call) {
        if (me !== termRun) return;
        typed += ch;
        out.innerHTML = callHtml(typed) + '<span class="caret"></span>';
        if (ch !== " ") await sleep(9);
      }
      await sleep(260);
    }
    if (me !== termRun) return;
    out.innerHTML = callHtml(step.call) + "\n\n" + stepBody(step);
  }

  if (stepsBox && DEMO.steps.length) {
    DEMO.steps.forEach((step, i) => {
      const b = h("button", { class: "step", role: "tab", type: "button", "aria-selected": "false" },
        h("span", { class: "n", text: String(i + 1) }),
        h("span", {}, h("b", { text: step.title }), h("span", { text: STEP_TEXT[step.id] || "" })));
      b.addEventListener("click", () => showStep(i));
      stepsBox.append(b);
    });
    showStep(0, false);
    $("#play-all").addEventListener("click", async () => {
      for (let i = 0; i < DEMO.steps.length; i++) {
        const before = termRun;
        await showStep(i);
        await sleep(1600);
        if (termRun !== before + 1) return;    // the visitor clicked something else
      }
    });
  }

  /* ---------- tool explorer ---------- */
  const famList = $("#fam-list"), panel = $("#panel"), search = $("#tool-search");
  let famId = SITE.families.length ? SITE.families[0].id : "";
  const KIND = { read: "read-only", write: "writes", destructive: "deletes" };

  function toolItem(t, tag) {
    const li = h("li", { class: "tool" });
    const head = h("div", {}, h("code", { text: t.name }), h("span", { class: "badge b-" + t.kind, text: KIND[t.kind] || t.kind }));
    if (tag) head.append(h("span", { class: "fam-tag", text: "  " + tag }));
    li.append(head, h("p", { text: t.summary }));
    return li;
  }

  function renderPanel() {
    const q = search.value.trim().toLowerCase();
    panel.textContent = "";
    if (q) {
      const hits = [];
      SITE.families.forEach((f) => f.tools.forEach((t) => {
        if ((t.name + " " + t.summary).toLowerCase().includes(q)) hits.push({ t, f });
      }));
      panel.append(h("h3", { text: hits.length + (hits.length === 1 ? " tool matches" : " tools match") }),
        h("p", { class: "blurb", text: "Across every family." }));
      if (!hits.length) { panel.append(h("p", { class: "empty", text: "Nothing matches. Try a shorter word." })); return; }
      const ul = h("ul", { class: "tools" });
      hits.forEach(({ t, f }) => ul.append(toolItem(t, f.title)));
      panel.append(ul);
      return;
    }
    const fam = SITE.families.find((f) => f.id === famId) || SITE.families[0];
    if (!fam) return;
    panel.append(h("h3", { text: fam.title }), h("p", { class: "blurb", text: fam.blurb }));
    const ul = h("ul", { class: "tools" });
    fam.tools.forEach((t) => ul.append(toolItem(t)));
    panel.append(ul);
  }

  function renderFamilies() {
    famList.textContent = "";
    SITE.families.forEach((f) => {
      const b = h("button", { class: "fam", role: "tab", type: "button", "aria-selected": String(f.id === famId) },
        h("span", { text: f.title }), h("span", { class: "count", text: String(f.count) }));
      b.addEventListener("click", () => { famId = f.id; search.value = ""; renderFamilies(); renderPanel(); });
      b.addEventListener("keydown", (e) => {
        const step = { ArrowDown: 1, ArrowRight: 1, ArrowUp: -1, ArrowLeft: -1 }[e.key];
        if (!step) return;
        e.preventDefault();
        const items = $$(".fam", famList), i = items.indexOf(b);
        items[(i + step + items.length) % items.length].click();
        $$(".fam", famList)[(i + step + items.length) % items.length].focus();
      });
      famList.append(b);
    });
  }
  if (famList) { renderFamilies(); renderPanel(); search.addEventListener("input", renderPanel); }

  /* ---------- install tabs, copy buttons, PyPI check ---------- */
  const tabs = $$("#install-tabs .tab");
  const panels = { "tab-download": "p-download", "tab-package": "p-package", "tab-source": "p-source" };
  function selectTab(tab) {
    tabs.forEach((t) => {
      const on = t === tab;
      t.setAttribute("aria-selected", String(on));
      $("#" + panels[t.id]).classList.toggle("hidden", !on);
    });
  }
  tabs.forEach((t) => t.addEventListener("click", () => selectTab(t)));
  $("#install-tabs").addEventListener("keydown", (e) => {
    const visible = tabs.filter((t) => !t.classList.contains("hidden"));
    const i = visible.indexOf(document.activeElement);
    if (i < 0 || !["ArrowRight", "ArrowLeft"].includes(e.key)) return;
    const next = visible[(i + (e.key === "ArrowRight" ? 1 : -1) + visible.length) % visible.length];
    next.focus(); selectTab(next);
  });

  $$("[data-copy]").forEach((btn) => btn.addEventListener("click", async () => {
    const text = $("pre", btn.parentElement).innerText.trim();
    try { await navigator.clipboard.writeText(text); }
    catch (e) {
      const ta = h("textarea", { style: "position:fixed;opacity:0" });
      ta.value = text; document.body.append(ta); ta.select();
      try { document.execCommand("copy"); } catch (err) { /* nothing more to try */ }
      ta.remove();
    }
    const old = btn.textContent;
    btn.textContent = "Copied";
    setTimeout(() => { btn.textContent = old; }, 1400);
  }));

  fetch("https://pypi.org/pypi/pbi-mcp/json").then((r) => {
    if (!r.ok) return;
    $("#tab-package").classList.remove("hidden");
    const note = $("#pkg-note"); if (note) note.classList.add("hidden");
  }).catch(() => { /* offline or blocked: keep the note */ });
})();
