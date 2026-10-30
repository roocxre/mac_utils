// The isolated half (every frame, document_start): finds fields, gathers what inject.js reports
// about them, asks the helper (through background.js) who owns each script and what the site's
// policy says, works out each field's risk, and draws the overlay under each field.
(() => {
  "use strict";
  if (globalThis.__wdmegContent) return;
  globalThis.__wdmegContent = true;
  const F = globalThis.WDMEG_FIELDS;
  const TOP = window === window.top;
  const PAYMENT = /(^|\.)(js\.stripe\.com|m\.stripe\.network|hooks\.stripe\.com|checkout\.stripe\.com|assets\.braintreegateway\.com|payments\.braintree-api\.com|checkoutshopper-[a-z-]+\.adyen\.com|web\.squarecdn\.com|pci-connect\.squareup\.com|checkout\.shopifycs\.com|api\.recurly\.com)$/;
  if (PAYMENT.test(location.hostname)) return;
  const HIGH = ["identity-resolution", "session-replay", "data-broker"];
  const MEDIUM = ["advertising", "customer-data-platform"];
  const LABEL = {
    "identity-resolution": "identity resolution", "session-replay": "session replay", "data-broker": "data broker",
    "customer-data-platform": "customer-data platform", "email-platform": "email platform",
    "tag-manager": "tag manager", "fraud-prevention": "fraud prevention",
  };

  // ---------------------------------------------------------------- state
  let port = null, secret = null;
  let config = null;                  // from background.js
  let siteInfo = null;
  const recs = new Map();             // cid -> field record
  const byEl = new WeakMap();
  let nextId = 1;
  const pageListen = new Map();       // who -> Set(event types), listeners on window/document
  const scripts = new Map();          // script URL -> who inserted it
  const blocked = [];
  const entities = new Map();         // URL or host -> resolved entity
  const pendingTags = [];
  let reportedSubmit = false;

  const send = (type, payload) => new Promise((resolve) => {
    try {
      chrome.runtime.sendMessage({ type, ...payload }, (answer) => {
        if (chrome.runtime.lastError) resolve({ error: chrome.runtime.lastError.message });
        else resolve(answer || {});
      });
    } catch (e) { resolve({ error: String(e) }); }
  });

  // ---------------------------------------------------------------- the channel from inject.js
  window.addEventListener("message", (event) => {
    const data = event.data;
    if (port || event.source !== window || !data || !data.__wdmeg_hello || !event.ports || !event.ports[0]) return;
    event.stopImmediatePropagation();
    secret = data.__wdmeg_hello;
    port = event.ports[0];
    port.onmessage = (e) => onMain((e.data && e.data.batch) || []);
    if (config) sendConfig();
    while (pendingTags.length) tag(pendingTags.shift());
  }, true);

  function sendConfig() {
    if (!port || !config) return;
    port.postMessage({ config: { mode: config.mode, needles: config.needles, after_submit: config.after_submit,
      login_hosts: config.login_hosts, top_site: config.site } });
  }

  function onMain(batch) {
    for (const item of batch) {
      const rec = item.f ? recs.get(item.f) : null;
      if (item.k === "listen") {
        const map = rec ? rec.listen : pageListen;
        if (!map.has(item.who)) map.set(item.who, new Set());
        map.get(item.who).add(item.type);
      } else if (item.k === "read" && rec) {
        rec.read.add(item.who);
      } else if (item.k === "hidden" && rec) {
        rec.hidden.add(item.who);
      } else if (item.k === "tx") {
        for (const cid of item.fields || []) {
          const r = recs.get(cid);
          if (!r) continue;
          if (!r.tx.has(item.who)) r.tx.set(item.who, new Set());
          r.tx.get(item.who).add(item.dest);
          if (item.third) r.sentBefore.add(item.dest);
        }
        if (item.real && item.third) noteSubmitted();
      } else if (item.k === "script") {
        scripts.set(item.url, item.by);
      } else if (item.k === "blocked") {
        blocked.push({ how: item.how, dest: item.dest, who: item.who, what: item.what, at: Date.now() });
        send("blocked", { domain: item.dest, kind: item.how, detail: item.what || "" });
      }
    }
    changed();
  }

  // ---------------------------------------------------------------- fields
  function shadowOf(host) {
    try { return chrome.dom.openOrClosedShadowRoot(host); } catch (e) { return host.shadowRoot; }
  }
  function scan(root) {
    for (const el of F.find(root || document, shadowOf)) register(el);
    if (config && config.payment_frames) markPaymentFrames(root || document);
  }
  function register(el) {
    if (byEl.has(el)) return;
    const found = F.classify(el);
    const rec = {
      cid: nextId++, el, type: found.type, how: found.how, search: !!found.search, context: found.context,
      form: withWidgetScript(el, F.formContext(el, config && config.widgets)), selector: F.selectorFor(el), listen: new Map(),
      read: new Set(), tx: new Map(), hidden: new Set(), sentBefore: new Set(), risk: "low", badge: null, line: null, panel: null,
      expanded: false, visible: false, payment: null,
    };
    byEl.set(el, rec);
    recs.set(rec.cid, rec);
    if (port) tag(rec); else pendingTags.push(rec);
    observeVisibility(rec);
    if (!found.how) unplaced.push(rec);
    changed();
  }
  // A widget known by its script rather than its markup: the field is in a popup and the page loads it.
  function withWidgetScript(el, form) {
    if (form.widget || !config || !config.widgets) return form;
    if (!el.closest || !el.closest("[role=dialog], [aria-modal], [class*=popup i], [class*=modal i], [id*=popup i], [class*=overlay i]")) return form;
    const hosts = [...document.scripts].map((x) => { try { return new URL(x.src).hostname; } catch (e) { return ""; } });
    for (const [name, w] of Object.entries(config.widgets)) {
      if ((w.scripts || []).some((h) => hosts.some((x) => x === h || x.endsWith("." + h)))) return { ...form, widget: name };
    }
    return form;
  }
  function tag(rec) {
    try { rec.el.dispatchEvent(new CustomEvent(secret, { detail: rec.cid, composed: true })); } catch (e) { /* gone */ }
  }

  // Fields the rules couldn't place: the helper's classifier, a few at a time.
  const unplaced = [];
  let classifying = false;
  async function classifyUnplaced() {
    if (classifying || !unplaced.length || !config || !config.helper) return;
    classifying = true;
    const batch = unplaced.splice(0, 20);
    const answer = await send("classify_fields", { contexts: batch.map((r) => r.context) });
    (answer.ok && answer.ok.types || []).forEach((t, i) => {
      if (t && t.type && t.type !== "other") { batch[i].type = t.type; batch[i].how = "model"; }
    });
    classifying = false;
    changed();
  }

  function markPaymentFrames(root) {
    for (const frame of root.querySelectorAll ? root.querySelectorAll("iframe[src]") : []) {
      if (byEl.has(frame)) continue;
      let host = "";
      try { host = new URL(frame.src, location.href).hostname; } catch (e) { continue; }
      for (const [name, hosts] of Object.entries(config.payment_frames)) {
        if (hosts.includes(host)) {
          const rec = { cid: nextId++, el: frame, type: "payment", payment: name, listen: new Map(), read: new Set(),
            tx: new Map(), hidden: new Set(), sentBefore: new Set(), risk: "low", form: {}, context: {} };
          byEl.set(frame, rec);
          recs.set(rec.cid, rec);
          observeVisibility(rec);
        }
      }
    }
  }

  const visibility = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      const rec = byEl.get(entry.target);
      if (rec) rec.visible = entry.isIntersecting && entry.boundingClientRect.width > 12 &&
        entry.boundingClientRect.height > 8;
    }
    render();
  });
  function observeVisibility(rec) { try { visibility.observe(rec.el); } catch (e) { /* ignore */ } }

  let mutationTimer = null;
  const added = new Set();
  const mutations = new MutationObserver((list) => {
    for (const m of list) for (const node of m.addedNodes) if (node.nodeType === 1) added.add(node);
    if (!mutationTimer) {
      mutationTimer = setTimeout(() => {
        mutationTimer = null;
        for (const node of added) if (node.isConnected) scan(node);
        added.clear();
        for (const rec of recs.values()) if (!rec.el.isConnected) dropField(rec);
      }, 300);
    }
  });
  function dropField(rec) {
    recs.delete(rec.cid);
    hide(rec);
    for (const node of [rec.badge, rec.line, rec.panel]) if (node) node.remove();
    try { visibility.unobserve(rec.el); } catch (e) { /* ignore */ }
  }

  // ---------------------------------------------------------------- who is who
  function hostOf(url) {
    if (!url || url === "inline") return location.hostname;
    if (url.startsWith("wdmeg-inline://")) return url.slice(15).split("/")[0];
    try { return new URL(url.includes("://") ? url : "https://" + url + "/").hostname; } catch (e) { return ""; }
  }
  function entityOf(url) {
    const key = url === "inline" ? location.origin + "/" : url.startsWith("wdmeg-inline://") ?
      "https://" + hostOf(url) + "/" : url.includes("://") ? url : "https://" + url + "/";
    const e = entities.get(key) || { domain: hostOf(url), categories: [], first_party: isFirst(hostOf(url)) };
    // The site's own subdomain that is really a tracker (a cloaked CNAME, or a tracking path) counts as one.
    if (e.first_party && e.cloaked) {
      return { ...e, first_party: false, domain: e.cloaked.to || hostOf(url), owner: e.cloaked.owner ||
        `${hostOf(url)} (tracking through the site's own address)`, categories: e.cloaked.categories || [],
        broker: e.cloaked.broker || null };
    }
    return e;
  }
  function isFirst(host) {
    const site = config ? config.site : location.hostname;
    return host === site || host.endsWith("." + site);
  }
  let resolving = false;
  async function resolveUnknown() {
    if (resolving || !config || !config.helper) return;
    const wanted = new Set();
    const want = (url) => {
      if (!url) return;
      const key = url === "inline" ? location.origin + "/" : url.startsWith("wdmeg-inline://") ?
        "https://" + hostOf(url) + "/" : url.includes("://") ? url : "https://" + url + "/";
      if (!entities.has(key)) wanted.add(key);
    };
    for (const rec of recs.values()) {
      for (const who of rec.listen.keys()) want(who);
      for (const who of rec.read) want(who);
      for (const [who, dests] of rec.tx) { want(who); for (const d of dests) want(d); }
      if (rec.form && rec.form.action) want(rec.form.action);
    }
    for (const who of pageListen.keys()) want(who);
    for (const url of scripts.keys()) want(url);
    for (const b of blocked) want(b.dest);
    if (!wanted.size) return;
    resolving = true;
    const answer = await send("resolve", { urls: [...wanted].slice(0, 300) });
    resolving = false;
    if (answer.ok) for (const [url, info] of Object.entries(answer.ok)) entities.set(url, info);
    else for (const url of wanted) entities.set(url, { domain: hostOf(url), categories: [], first_party: isFirst(hostOf(url)) });
    changed();
  }

  // Each field's recipients: {domain: {owner, categories, broker, first_party, actions, events, via}}.
  function recipientsOf(rec) {
    const out = new Map();
    const add = (who, action, events) => {
      const e = entityOf(who);
      const key = e.first_party ? "(this site)" : e.domain || hostOf(who);
      if (!out.has(key)) out.set(key, { domain: key, owner: e.owner, categories: e.categories || [], broker: e.broker,
        broker_name: e.broker_name, first_party: !!e.first_party, cloaked: e.cloaked, actions: new Set(),
        events: new Set(), via: new Set(), scripts: new Set() });
      const r = out.get(key);
      r.actions.add(action);
      for (const ev of events || []) r.events.add(ev);
      if (who !== "inline" && !who.startsWith("wdmeg-inline://")) r.scripts.add(who);
      const by = scripts.get(who);
      if (by && by !== "inline") {
        const parent = entityOf(by);
        if (parent.domain && parent.domain !== e.domain && !parent.first_party) r.via.add(parent.owner || parent.domain);
      }
    };
    for (const [who, types] of rec.listen) add(who, "listens", types);
    for (const [who, types] of pageListen) add(who, "listens to every field", types);
    for (const who of rec.read) add(who, "reads");
    for (const [who, dests] of rec.tx) {
      add(who, "sends", []);
      for (const d of dests) add("https://" + d + "/", "receives", []);
    }
    if (rec.form && rec.form.action && !isFirst(hostOf(rec.form.action))) add(rec.form.action, "the form submits here");
    return [...out.values()];
  }

  function brokerScripts() {
    const out = new Map();
    for (const url of scripts.keys()) {
      const e = entityOf(url);
      if (e.broker && !e.first_party) out.set(e.broker, e.broker_name || e.owner || e.domain);
    }
    return out;
  }

  function policyFlags() {
    const f = siteInfo && siteInfo.policy && siteInfo.policy.findings || {};
    const yes = (k) => f[k] && f[k].verdict === "yes";
    return { sells: yes("sells"), brokers: yes("shares_brokers"), ads: yes("shares_ads"), optout: yes("opt_out"),
      analyzed: siteInfo && siteInfo.policy && siteInfo.policy.status === "ok" };
  }

  function riskOf(rec, recipients) {
    if (rec.payment) return "low";
    const third = recipients.filter((r) => !r.first_party && [...r.actions].some((a) => a !== "receives" &&
      a !== "the form submits here"));
    const canary = siteInfo && siteInfo.canary;
    const p = policyFlags();
    if (third.some((r) => r.categories.some((c) => HIGH.includes(c)) || r.broker)) return "high";
    if (rec.sentBefore.size) return "high";
    if (canary && canary.leaked_to && canary.leaked_to.length) return "high";
    if (p.sells || p.brokers) return "high";
    if (brokerScripts().size) return "high";
    if (siteInfo && siteInfo.site_broker) return "high";
    if (third.some((r) => r.categories.some((c) => MEDIUM.includes(c)))) return "medium";
    return "low";
  }

  // ---------------------------------------------------------------- the overlay
  let overlayHost = null, layer = null, shadow = null;
  function ensureOverlay() {
    if (overlayHost || !document.documentElement) return !!overlayHost;
    overlayHost = document.createElement("wdmeg-overlay");
    overlayHost.setAttribute("style", "all:initial!important;position:fixed!important;inset:0!important;" +
      "width:100vw!important;height:100vh!important;margin:0!important;padding:0!important;border:0!important;" +
      "background:transparent!important;pointer-events:none!important;overflow:visible!important;" +
      "z-index:2147483647!important;display:block!important;max-width:none!important;max-height:none!important");
    // Closed, so the page can't read it; #wdmeg-debug in the address opens it for the headless tests.
    shadow = overlayHost.attachShadow({ mode: location.hash.includes("wdmeg-debug") ? "open" : "closed" });
    const style = document.createElement("style");
    style.textContent = CSS;
    shadow.appendChild(style);
    layer = document.createElement("div");
    layer.className = "layer";
    shadow.appendChild(layer);
    document.documentElement.appendChild(overlayHost);
    // Fixed + top z-index, and out of the browser's top layer: password managers (Bitwarden) keep their
    // menus there and switch them off when something else keeps raising itself above them. The overlay
    // enters it only while the page has a dialog or popover open over one of its fields.
    document.addEventListener("toggle", onToggle, true);
    return true;
  }
  const pageLayers = new Set();
  function onToggle(e) {
    const el = e.target;
    schedulePosition();
    // Another extension's menu is a custom element; the page's own dialogs and popovers rarely are.
    if (!(el instanceof Element) || el === overlayHost || el.localName.includes("-")) return;
    if (e.newState === "open") {
      if (![...recs.values()].some((r) => el.contains(r.el))) return;
      pageLayers.add(el);
      raise();
    } else if (pageLayers.delete(el) && !pageLayers.size) {
      try {
        if (overlayHost.matches(":popover-open")) overlayHost.hidePopover();
        overlayHost.removeAttribute("popover");
      } catch (err) { /* ignore */ }
    }
  }
  function raise() {
    try {
      if (!overlayHost.hasAttribute("popover")) overlayHost.setAttribute("popover", "manual");
      if (overlayHost.matches(":popover-open")) overlayHost.hidePopover();
      overlayHost.showPopover();
    } catch (e) { /* no popover API: fixed + z-index */ }
  }

  // Password managers' buttons and menus (Bitwarden, 1Password, …): small fixed custom elements at the top
  // of the page. The badge and the line keep clear of them; Bitwarden closes its menu when covered.
  function foreignRects() {
    const out = [];
    const vw = window.innerWidth, vh = window.innerHeight;
    const seen = new Set();
    const candidates = [];
    for (const parent of [document.documentElement, document.body]) {
      if (parent) candidates.push(...parent.children);
    }
    try { candidates.push(...document.querySelectorAll(":popover-open")); } catch (e) { /* ignore */ }
    for (const el of candidates) {
      if (seen.has(el) || el === overlayHost || !el.localName.includes("-")) continue;
      seen.add(el);
      const r = el.getBoundingClientRect();
      if (r.width < 4 || r.height < 4 || r.width * r.height > vw * vh / 4) continue;
      const how = getComputedStyle(el).position;
      if (how === "fixed" || how === "absolute") out.push(r);
    }
    return out;
  }
  const overlaps = (a, b) => a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top;

  function summaryOf(rec, recipients) {
    if (rec.payment) return { icon: "🔒", text: `Payment field hosted by ${rec.payment}: left alone` };
    const third = recipients.filter((r) => !r.first_party);
    const parts = [];
    const sending = third.filter((r) => r.actions.has("sends") || r.actions.has("receives"));
    const listening = third.filter((r) => !r.actions.has("receives") || r.actions.size > 1);
    if (rec.sentBefore.size) parts.push(`sent before you submitted to ${[...rec.sentBefore].slice(0, 2).join(", ")}`);
    else if (sending.length) parts.push(`${sending.length} third ${sending.length === 1 ? "party" : "parties"} sending`);
    if (listening.length) parts.push(`${listening.length} third ${listening.length === 1 ? "party" : "parties"} listening`);
    const brokers = new Set([...third.filter((r) => r.broker).map((r) => r.broker), ...brokerScripts().keys()]);
    if (siteInfo && siteInfo.site_broker) parts.push(`this site is a data broker (${siteInfo.site_broker.name})`);
    if (brokers.size) parts.push(`${brokers.size} broker${brokers.size === 1 ? "" : "s"}`);
    const p = policyFlags();
    if (p.sells) parts.push("policy: sells data");
    else if (p.brokers) parts.push("policy: shares with brokers");
    else if (p.ads) parts.push("policy: shares for advertising");
    else if (siteInfo && siteInfo.policy && siteInfo.policy.status === "queued") parts.push("policy: reading…");
    const canary = siteInfo && siteInfo.canary;
    if (canary && canary.leaked_to && canary.leaked_to.length) parts.push(`canary leaked to ${canary.leaked_to[0].domain}`);
    const fieldBlocks = blocked.filter((b) => b.what).length;
    if (fieldBlocks) parts.push(`${fieldBlocks} blocked`);
    if (!parts.length) parts.push(config && !config.helper ? "no third parties seen (the Mac helper isn't connected)" :
      "no third parties seen");
    const icon = rec.risk === "high" ? "⚠" : rec.risk === "medium" ? "◆" : "✓";
    return { icon, text: parts.join(" · ") };
  }

  // Each field gets a small badge inside its right edge (risk colour, how many third parties); the
  // one-line summary shows under it while the field has focus or the badge is hovered, and lets
  // clicks through to the page; clicking the badge opens the panel.
  function render() {
    if (!config || config.overlay === false) {
      if (layer) layer.textContent = "";
      for (const rec of recs.values()) rec.badge = rec.line = rec.panel = null;
      return;
    }
    for (const rec of recs.values()) {
      const recipients = recipientsOf(rec);
      rec.risk = riskOf(rec, recipients);
      if (!rec.visible) { hide(rec); continue; }
      if (!ensureOverlay()) return;
      const third = recipients.filter((r) => !r.first_party).length;
      const quiet = rec.risk === "low" && !third && !rec.payment;
      if (!rec.badge) {
        rec.badge = el("div", "badge");
        rec.badge.title = "Where does what you type here go? Click for details";
        rec.badge.addEventListener("click", (e) => { e.stopPropagation(); rec.expanded = !rec.expanded; render(); });
        rec.badge.addEventListener("mouseenter", () => { rec.hover = true; render(); });
        rec.badge.addEventListener("mouseleave", () => { rec.hover = false; render(); });
        layer.append(rec.badge);
      }
      rec.badge.className = `badge ${rec.risk}${quiet ? " quiet" : ""}${rec.expanded ? " on" : ""}`;
      rec.badge.textContent = quiet ? "" : rec.payment ? "🔒" :
        (rec.risk === "high" ? "⚠" : rec.risk === "medium" ? "◆" : "✓") + (third ? " " + third : "");
      rec.badge.style.display = "";
      const { icon, text } = summaryOf(rec, recipients);
      if ((rec.focused || rec.hover) && !rec.expanded) {
        if (!rec.line) { rec.line = el("div", "line"); layer.append(rec.line); }
        const key = JSON.stringify([icon, text, rec.risk, rec.type, rec.form.widget, rec.note]);
        if (rec.line.dataset.key !== key) {
          rec.line.dataset.key = key;
          rec.line.textContent = "";
          rec.line.className = `line ${rec.risk}`;
          rec.line.append(el("span", "icon", icon), el("span", "text", rec.note || text));
        }
        rec.line.style.display = "";
      } else if (rec.line) {
        rec.line.style.display = "none";
      }
      if (rec.expanded) {
        if (!rec.panel) {
          rec.panel = el("div", "panel");
          rec.panel.addEventListener("keydown", (e) => { if (e.key === "Escape") { rec.expanded = false; render(); } });
          layer.append(rec.panel);
        }
        const key = JSON.stringify([icon, text, detailKey(rec, recipients)]);
        if (rec.panel.dataset.key !== key) {
          rec.panel.dataset.key = key;
          rec.panel.textContent = "";
          rec.panel.className = `panel ${rec.risk}`;
          const head = el("div", "head");
          head.append(el("span", "icon", icon), el("span", "text", text),
            button("✕", () => { rec.expanded = false; render(); }, "close"));
          rec.panel.append(head, details(rec, recipients));
        }
        rec.panel.style.display = "";
      } else if (rec.panel) {
        rec.panel.style.display = "none";
      }
    }
    position();
  }
  function hide(rec) {
    for (const node of [rec.badge, rec.line, rec.panel]) if (node) node.style.display = "none";
  }
  document.addEventListener("focusin", (e) => {
    setTimeout(schedulePosition, 250);     // a password manager's menu opens a moment after focus
    const rec = byEl.get(e.composedPath()[0]);
    if (rec) { rec.focused = true; render(); }
  }, true);
  document.addEventListener("focusout", (e) => {
    const rec = byEl.get(e.composedPath()[0]);
    if (!rec) return;
    setTimeout(() => {
      rec.focused = false;
      render();
    }, 200);
  }, true);
  function detailKey(rec, recipients) {
    return JSON.stringify([recipients.map((r) => [r.domain, [...r.actions], r.owner]), siteInfo, rec.policy,
      rec.type, blocked.length, config.mode, rec.note]);
  }

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }
  function button(text, onClick, cls) {
    const b = el("button", cls || "", text);
    b.addEventListener("click", (e) => { e.stopPropagation(); onClick(); });
    return b;
  }
  function section(title) {
    const s = el("div", "section");
    s.append(el("div", "title", title));
    return s;
  }

  function details(rec, recipients) {
    const box = el("div", "details");
    const kind = rec.payment ? "payment" : rec.type + (rec.how === "model" ? " (guessed)" : "");
    box.append(el("div", "kind", `${kind} field · ${rec.risk} risk`));
    if (rec.note) box.append(el("div", "note", rec.note));

    // 1. Recipients seen
    const s1 = section("Recipients seen");
    const third = recipients.filter((r) => !r.first_party);
    const own = recipients.find((r) => r.first_party);
    if (!third.length) s1.append(el("div", "row muted", "No third party listens to, reads or sends this field."));
    for (const r of third) {
      const row = el("div", "row");
      const cats = r.categories.filter((c) => c !== "data-broker").map((c) => LABEL[c] || c);
      row.append(el("b", "", r.owner || r.domain));
      if (r.owner && r.owner !== r.domain) row.append(el("span", "muted", ` ${r.domain}`));
      if (cats.length) row.append(el("span", "cats", ` · ${cats.join(", ")}`));
      row.append(el("div", "acts", [...r.actions].join(", ") +
        (r.events.size ? ` (${[...r.events].slice(0, 5).join(", ")})` : "") +
        (r.via.size ? ` · loaded by ${[...r.via].join(", ")}` : "")));
      if (r.cloaked) row.append(el("div", "acts warn", `on the site's own subdomain, really ${r.cloaked.owner || r.cloaked.to || r.cloaked.why}`));
      s1.append(row);
    }
    if (own) s1.append(el("div", "row muted", `This site's own code: ${[...own.actions].join(", ")}`));
    if (rec.form && rec.form.action) {
      s1.append(el("div", "row muted", `The form submits to ${hostOf(rec.form.action)}` +
        (rec.form.widget ? ` · ${rec.form.widget} email-capture widget` : "")));
    } else if (rec.form && rec.form.widget) {
      s1.append(el("div", "row muted", `${rec.form.widget} email-capture widget`));
    }
    if (rec.hidden.size) s1.append(el("div", "row ok", `Strict mode hid this field from ${rec.hidden.size} third-party script(s)`));
    const fieldBlocked = blocked.filter((b) => b.what);
    for (const b of fieldBlocked.slice(-4)) s1.append(el("div", "row ok", `Blocked: your email going to ${b.dest} (${b.how})`));
    box.append(s1);

    // 2. Potential data brokers
    const brokers = new Map(brokerScripts());
    for (const r of third) if (r.broker) brokers.set(r.broker, r.broker_name || r.owner || r.domain);
    if (siteInfo && siteInfo.site_broker) brokers.set(siteInfo.site_broker.id, siteInfo.site_broker.name + " (this site)");
    if (brokers.size) {
      const s2 = section("Potential data brokers");
      for (const [, name] of brokers) s2.append(el("div", "row warn", `${name}: in a data-broker registry`));
      box.append(s2);
    }

    // 3. Policy
    const s3 = section("Policy");
    const policy = siteInfo && siteInfo.policy;
    if (!config.helper) s3.append(el("div", "row muted", "The Mac helper isn't connected."));
    else if (!policy || policy.status === "none") s3.append(el("div", "row muted", "No policy found yet."));
    else if (policy.status === "queued" || policy.status === "running") s3.append(el("div", "row muted", "Reading the policy…"));
    else if (policy.status === "error" || policy.status === "missing") {
      s3.append(el("div", "row muted", policy.error || "Couldn't find or read the policy."));
    } else {
      const f = policy.findings || {};
      const names = { sells: "Sells personal information", shares_ads: "Shares it for advertising",
        shares_brokers: "Shares it with data brokers", opt_out: "Lets you opt out" };
      for (const [k, label] of Object.entries(names)) {
        const v = f[k];
        if (!v) continue;
        const row = el("div", `row ${v.verdict === "yes" && k !== "opt_out" ? "warn" : ""}`);
        row.append(el("b", "", `${label}: ${v.verdict === "yes" ? "yes" : v.verdict === "no" ? "says no" : "not stated"}`));
        if (v.quote) {
          const q = el("blockquote", "", `“${v.quote}”`);
          row.append(q);
        }
        if (v.link) {
          const a = el("a", "", "in the policy");
          a.href = v.link; a.target = "_blank"; a.rel = "noopener";
          row.append(a);
        }
        s3.append(row);
      }
      if (policy.signals && policy.signals.do_not_sell) s3.append(el("div", "row", "The site shows a “Do Not Sell or Share” link: it sells or shares data under CCPA's definitions."));
      if (policy.signals && policy.signals.consent_tool) s3.append(el("div", "row muted", `Consent tool: ${policy.signals.consent_tool}`));
      if (rec.policy && rec.policy.items) {
        for (const p of rec.policy.items.slice(0, 4)) {
          const row = el("div", "row");
          if (p.labels.length) row.append(el("span", "tag", p.labels.join(", ")));
          row.append(el("blockquote", "", `“${p.text.slice(0, 400)}${p.text.length > 400 ? "…" : ""}”`));
          if (p.link) { const a = el("a", "", "in the policy"); a.href = p.link; a.target = "_blank"; a.rel = "noopener"; row.append(a); }
          s3.append(row);
        }
      } else if (policy.status === "ok") {
        loadPolicy(rec);
      }
      if (policy.url) {
        const a = el("a", "muted", "the whole policy");
        a.href = policy.url; a.target = "_blank"; a.rel = "noopener";
        s3.append(a);
      }
    }
    box.append(s3);

    // 4. Canary
    const canary = siteInfo && siteInfo.canary;
    if (canary) {
      const s4 = section("Canary test");
      if (canary.status === "queued" || canary.status === "running") s4.append(el("div", "row muted", "Testing in a hidden browser…"));
      else if (canary.status === "error") s4.append(el("div", "row muted", `The test didn't run: ${canary.error || "error"}`));
      else if (canary.leaked_to && canary.leaked_to.length) {
        s4.append(el("div", "row warn", `Typed data was sent before submission to ${canary.leaked_to.map((l) => l.domain).join(", ")}` +
          (canary.ran ? ` (${canary.ran})` : "")));
      } else if (canary.field_found === false) {
        s4.append(el("div", "row muted", "The test couldn't find this field as a fresh visitor sees the page" +
          (canary.ran ? ` (${canary.ran})` : "")));
      } else {
        s4.append(el("div", "row ok", "No leak seen" + (canary.ran ? ` (${canary.ran})` : "")));
      }
      if (canary.consent_banner) s4.append(el("div", "row muted", "A consent banner was showing; the test didn't touch it."));
      box.append(s4);
    }

    // 5. Actions
    const s5 = section("Actions");
    const actions = el("div", "actions");
    const select = el("select");
    for (const m of ["observe", "block", "strict"]) {
      const o = el("option", "", `${m}${m === "block" ? " (default)" : ""}`);
      o.value = m;
      if (config.mode === m) o.selected = true;
      select.append(o);
    }
    select.addEventListener("change", () => setMode(select.value));
    select.addEventListener("click", (e) => e.stopPropagation());
    const label = el("label", "", "This site: ");
    label.append(select);
    actions.append(label);
    if (!rec.payment && rec.type !== "password") actions.append(button("Re-run canary test", () => runCanary(rec)));
    actions.append(button("Hide on this site", () => setOverlay(false)));
    s5.append(actions);
    box.append(s5);
    return box;
  }

  async function loadPolicy(rec) {
    if (rec.policyLoading) return;
    rec.policyLoading = true;
    const answer = await send("policy", { data_type: rec.type === "other" ? null : rec.type });
    rec.policy = answer.ok || { items: [] };
    render();
  }

  function position() {
    if (!layer) return;
    const vw = window.innerWidth, vh = window.innerHeight;
    const move = (node, x, y) => { node.style.transform = `translate(${Math.round(x)}px, ${Math.round(y)}px)`; };
    const foreign = foreignRects();
    for (const rec of recs.values()) {
      if (!rec.visible || !rec.badge || rec.badge.style.display === "none") continue;
      const r = rec.el.getBoundingClientRect();
      const bw = rec.badge.offsetWidth || 8, bh = rec.badge.offsetHeight || 8;
      const tall = rec.el.nodeName === "TEXTAREA" || r.height > 56;
      // The right-hand slot belongs to password managers and the site's own icons (show password, clear).
      let x = r.right - bw - 4 - (r.width > 140 ? Math.min(Math.max(r.height, 24), 36) : 0);
      const y = tall ? r.top + 4 : r.top + Math.max(0, (r.height - bh) / 2);
      for (const f of foreign) {
        if (overlaps({ left: x - 2, right: x + bw + 2, top: y, bottom: y + bh }, f)) x = f.left - bw - 6;
      }
      move(rec.badge, Math.max(r.left + 2, x), y);
      const left = Math.min(Math.max(4, r.left), Math.max(4, vw - 450));
      if (rec.line && rec.line.style.display !== "none") {
        move(rec.line, left, r.bottom + 2);
        rec.line.style.maxWidth = `${Math.max(260, Math.min(560, vw - left - 8))}px`;
        const box = { left, right: left + rec.line.offsetWidth, top: r.bottom + 2, bottom: r.bottom + 2 + rec.line.offsetHeight };
        rec.line.style.visibility = foreign.some((f) => overlaps(box, f)) ? "hidden" : "";
      }
      if (rec.panel && rec.panel.style.display !== "none") {
        const ph = rec.panel.offsetHeight;
        const up = r.bottom + 4 + ph > vh && r.top - ph - 4 > 0;
        move(rec.panel, left, up ? r.top - ph - 4 : r.bottom + 4);
      }
    }
  }
  let rafPending = false;
  function schedulePosition() {
    if (rafPending) return;
    rafPending = true;
    requestAnimationFrame(() => { rafPending = false; position(); });
  }

  // ---------------------------------------------------------------- actions
  async function setMode(mode) {
    const answer = await send("mode", { mode });
    if (answer.ok) { config.mode = mode; sendConfig(); render(); }
  }
  async function setOverlay(on) {
    await send("overlay", { on });
    config.overlay = on;
    render();
  }
  async function runCanary(rec) {
    rec.note = "Queued a canary test: a hidden browser types a throwaway address here and watches the network.";
    render();
    const answer = await send("canary", { url: location.href, selector: rec.selector, field_type: rec.type });
    if (answer.error) rec.note = `Canary: ${answer.error}`;
    refreshSite();
  }

  // ---------------------------------------------------------------- submits
  function fieldsIn(container) {
    return [...recs.values()].filter((r) => !r.payment && r.el.isConnected && (!container || container.contains(r.el)));
  }
  function noteSubmitted(container) {
    if (!config) return;
    const list = fieldsIn(container);
    const values = list.map((r) => { try { return String(r.el.value || "").trim(); } catch (e) { return ""; } });
    const real = values.some((v) => v && config.needles.ci.includes(v.toLowerCase()));
    if (!real || reportedSubmit) return;
    reportedSubmit = true;
    const domains = new Set();
    for (const url of scripts.keys()) { const e = entityOf(url); if (!e.first_party && e.domain) domains.add(e.domain); }
    for (const who of pageListen.keys()) { const e = entityOf(who); if (!e.first_party && e.domain) domains.add(e.domain); }
    send("submitted", { url: location.href, real_email: real, domains: [...domains] });
  }
  const allowOnce = new WeakSet();
  document.addEventListener("submit", (e) => {
    if (!e.isTrusted || !config) return;
    const form = e.target;
    if (port) port.postMessage({ submit: 1 });
    noteSubmitted(form);
    if (config.mode === "observe" || allowOnce.has(form)) return;
    let action;
    try { action = new URL(form.getAttribute("action") || location.href, location.href); } catch (err) { return; }
    const host = action.hostname;
    const known = [...(config.after_submit || []), ...(config.login_hosts || [])].some((d) => host === d || host.endsWith("." + d));
    if (isFirst(host) || known || !fieldsIn(form).some((r) => r.type !== "other" || r.form.widget)) return;
    e.preventDefault();
    e.stopImmediatePropagation();
    confirmSubmit(form, host);
  }, true);
  document.addEventListener("click", (e) => {
    if (!e.isTrusted) return;
    const b = e.target && e.target.closest && e.target.closest("button, input[type=submit], [role=button]");
    if (!b) return;
    if (port) port.postMessage({ submit: 1 });
    setTimeout(() => noteSubmitted(b.closest("form, [role=dialog], [class*=popup], [class*=modal]") || null), 50);
  }, true);

  function confirmSubmit(form, host) {
    ensureOverlay();
    const box = el("div", "confirm");
    const e = entityOf("https://" + host + "/");
    box.append(el("div", "title", "This form sends what you typed to another company"));
    box.append(el("div", "row", `${e.owner || host} (${host}) isn't this site and isn't a known email service for it.`));
    const actions = el("div", "actions");
    actions.append(button("Send anyway", () => { box.remove(); allowOnce.add(form); form.requestSubmit(); }));
    actions.append(button("Don't send", () => box.remove()));
    box.append(actions);
    layer.append(box);
  }

  // ---------------------------------------------------------------- the page's signals and site info
  function policyLinks() {
    const scored = [];
    for (const a of document.querySelectorAll("a[href]")) {
      const t = (a.innerText || a.textContent || "").trim().toLowerCase().slice(0, 80);
      const href = a.href;
      if (!/^https?:/.test(href)) continue;
      let score = 0;
      if (/privacy (policy|notice|statement)|datenschutz|confidentialit|privacidad|privacy-policy|プライバシー|隐私|개인정보/.test(t)) score = 3;
      else if (/privacy/.test(t) || /privacy/.test(href.toLowerCase())) score = 2;
      else continue;
      if (/choices|do not sell|cookie|opt.?out|request/.test(t)) score -= 2;
      if (a.closest("footer, [role=contentinfo], #footer, .footer, [class*=footer]")) score += 1;
      scored.push([score, href.split("#")[0]]);
    }
    scored.sort((a, b) => b[0] - a[0]);
    return [...new Set(scored.filter((s) => s[0] > 0).map((s) => s[1]))].slice(0, 3);
  }
  function signals() {
    const texts = [...document.querySelectorAll("a, button")].map((a) => (a.innerText || "").trim().toLowerCase().slice(0, 80));
    const out = {
      do_not_sell: texts.some((t) => /do not sell|don't sell|do not share|your privacy choices|opt.?out of (the )?(sale|sharing)/.test(t)),
      cookie_settings: texts.some((t) => /cookie (settings|preferences|choices)|manage cookies|cookie policy/.test(t)),
    };
    const hosts = [...document.scripts].map((s) => { try { return new URL(s.src).hostname + new URL(s.src).pathname; } catch (e) { return ""; } });
    for (const [name, list] of Object.entries((config && config.consent_tools) || {})) {
      if (hosts.some((h) => list.some((l) => h.startsWith(l) || h.includes("." + l.split("/")[0])))) { out.consent_tool = name; break; }
    }
    return out;
  }
  async function refreshSite(withLinks) {
    if (!config || !config.helper) return;
    const payload = { page_url: location.href };
    if (withLinks) { payload.policy_links = policyLinks(); payload.signals = signals(); }
    const answer = await send("site", payload);
    if (answer.ok) { siteInfo = answer.ok; for (const rec of recs.values()) rec.policy = null; changed(); }
  }

  let lastPageReport = 0, pageTimer = null;
  function reportPage() {
    if (!config || !config.helper) return;
    const list = [...recs.values()].filter((r) => !r.payment && !r.search);
    if (!list.length) return;
    const fields = list.slice(0, 60).map((r) => ({ type: r.type, risk: r.risk, selector: r.selector, how: r.how,
      widget: r.form.widget || null, recipients: recipientsOf(r).filter((x) => !x.first_party).map((x) => x.domain) }));
    const merged = new Map();
    for (const r of list) {
      for (const x of recipientsOf(r)) {
        if (x.first_party) continue;
        const m = merged.get(x.domain) || { domain: x.domain, owner: x.owner, categories: x.categories, broker: x.broker,
          actions: new Set(), field_types: new Set() };
        for (const a of x.actions) m.actions.add(a);
        m.field_types.add(r.type);
        merged.set(x.domain, m);
      }
    }
    const recipients = [...merged.values()].map((m) => ({ ...m, actions: [...m.actions], field_types: [...m.field_types] }));
    const order = { low: 0, medium: 1, high: 2 };
    const risk = list.reduce((best, r) => (order[r.risk] > order[best] ? r.risk : best), "low");
    lastPageReport = Date.now();
    send("page", { url: location.href, risk, fields, recipients });
  }

  let changeTimer = null;
  function changed() {
    if (changeTimer) return;
    changeTimer = setTimeout(() => {
      changeTimer = null;
      resolveUnknown();
      classifyUnplaced();
      render();
      clearTimeout(pageTimer);
      pageTimer = setTimeout(reportPage, Math.max(3000, 30000 - (Date.now() - lastPageReport)));
    }, 250);
  }

  // ---------------------------------------------------------------- manual triggers
  function analyzeAll() {
    for (const rec of recs.values()) if (rec.visible) rec.expanded = true;
    if (config && config.overlay === false && TOP) setOverlay(true);       // asking again turns it back on here
    else render();
  }
  function selectionMode() {
    if (!ensureOverlay()) return;
    const pick = el("div", "pick");
    const box = el("div", "box");
    pick.append(box);
    layer.append(pick);
    let start = null;
    const done = () => { pick.remove(); window.removeEventListener("keydown", onKey, true); };
    const onKey = (e) => { if (e.key === "Escape") { e.preventDefault(); done(); } };
    window.addEventListener("keydown", onKey, true);
    pick.addEventListener("mousedown", (e) => { start = [e.clientX, e.clientY]; e.preventDefault(); });
    pick.addEventListener("mousemove", (e) => {
      if (!start) return;
      const [x0, y0] = start;
      Object.assign(box.style, { left: Math.min(x0, e.clientX) + "px", top: Math.min(y0, e.clientY) + "px",
        width: Math.abs(e.clientX - x0) + "px", height: Math.abs(e.clientY - y0) + "px", display: "block" });
    });
    pick.addEventListener("mouseup", (e) => {
      if (!start) return;
      const [x0, y0] = start;
      const sel = { left: Math.min(x0, e.clientX), right: Math.max(x0, e.clientX), top: Math.min(y0, e.clientY),
        bottom: Math.max(y0, e.clientY) };
      for (const rec of recs.values()) {
        const r = rec.el.getBoundingClientRect();
        if (r.right >= sel.left && r.left <= sel.right && r.bottom >= sel.top && r.top <= sel.bottom && r.width) {
          rec.expanded = true;
        }
      }
      done();
      if (config && config.overlay === false) setOverlay(true);
      else render();
    });
  }

  chrome.runtime.onMessage.addListener((message) => {
    if (!message || !config) return;
    if (message.type === "site-updated" && message.site === config.site) refreshSite();
    else if (message.type === "settings") {
      const mode = (message.modes || {})[config.site] || message.default_mode;
      config.overlay = !(message.overlay_off || []).includes(config.site);
      if (mode !== config.mode) { config.mode = mode; sendConfig(); }
      render();
    } else if (message.type === "config") {
      send("config", { url: location.href, top: TOP }).then((answer) => {
        if (answer.ok) { config = answer.ok; sendConfig(); render(); }
      });
    } else if (message.type === "analyze-all") analyzeAll();
    else if (message.type === "select-fields" && TOP) selectionMode();
    else if (message.type === "net-blocked") { blocked.push({ how: "rule", dest: message.domain, at: Date.now() }); changed(); }
  });

  // ---------------------------------------------------------------- start
  async function start() {
    const answer = await send("config", { url: location.href, top: TOP });
    config = answer.ok || { site: location.hostname, mode: "block", needles: { ci: [], cs: [] }, after_submit: [],
      helper: false, overlay: true };
    sendConfig();
    const go = () => {
      scan(document);
      mutations.observe(document.documentElement, { childList: true, subtree: true });
      window.addEventListener("scroll", schedulePosition, { capture: true, passive: true });
      window.addEventListener("resize", schedulePosition, { passive: true });
      setInterval(schedulePosition, 600);
      if (TOP) setTimeout(() => refreshSite(true), 1500);
      else refreshSite(false);
    };
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", go, { once: true });
    else go();
  }
  start();

  const CSS = `
    :host { all: initial; }
    .layer { position: fixed; inset: 0; pointer-events: none; font: 12px/1.35 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      --bg: #fff; --fg: #1d1d1f; --muted: #6e6e73; --line: rgba(0,0,0,.12); --high: #c62828; --medium: #b26a00; --low: #2e7d32;
      --chip: rgba(255,255,255,.96); --accent: #0a63c9; }
    @media (prefers-color-scheme: dark) { .layer { --bg: #1e1e20; --fg: #f2f2f7; --muted: #a1a1a6; --line: rgba(255,255,255,.14);
      --high: #ff6b6b; --medium: #ffb340; --low: #5fd068; --chip: rgba(30,30,32,.96); --accent: #5aa6ff; } }
    .badge { position: absolute; left: 0; top: 0; pointer-events: auto; cursor: pointer; user-select: none;
      font-size: 10px; line-height: 14px; height: 14px; padding: 0 5px; border-radius: 7px; font-weight: 600;
      background: var(--low); color: #fff; opacity: .85; white-space: nowrap; box-shadow: 0 1px 3px rgba(0,0,0,.2); }
    .badge.medium { background: var(--medium); } .badge.high { background: var(--high); }
    .badge.quiet { width: 8px; height: 8px; padding: 0; border-radius: 50%; opacity: .4; box-shadow: none; }
    .badge:hover, .badge.on { opacity: 1; }
    .line { position: absolute; left: 0; top: 0; pointer-events: none; display: flex; gap: 6px; align-items: center;
      white-space: nowrap; overflow: hidden; background: var(--chip); color: var(--fg); border: 1px solid var(--line);
      border-left: 3px solid var(--low); border-radius: 6px; padding: 2px 6px; box-shadow: 0 2px 10px rgba(0,0,0,.15); }
    .line .text { overflow: hidden; text-overflow: ellipsis; }
    .line.medium, .panel.medium { border-left-color: var(--medium); } .line.high, .panel.high { border-left-color: var(--high); }
    .panel { position: absolute; left: 0; top: 0; pointer-events: auto; width: min(440px, calc(100vw - 16px));
      max-height: 60vh; overflow: auto; background: var(--chip); color: var(--fg); border: 1px solid var(--line);
      border-left: 3px solid var(--low); border-radius: 8px; padding: 6px 10px 8px; box-shadow: 0 6px 24px rgba(0,0,0,.22); }
    .head { display: flex; gap: 6px; align-items: flex-start; font-weight: 600; }
    .head .text { flex: 1; }
    button.close { border: 0; background: transparent; padding: 0 2px; color: var(--muted); }
    .icon { font-size: 12px; } .high .icon { color: var(--high); } .medium .icon { color: var(--medium); } .low .icon { color: var(--low); }
    .details { margin-top: 4px; white-space: normal; }
    .kind { color: var(--muted); margin-bottom: 4px; text-transform: none; }
    .note { background: rgba(10,99,201,.1); border-radius: 4px; padding: 4px 6px; margin: 4px 0; }
    .section { border-top: 1px solid var(--line); padding: 5px 0 3px; }
    .title { font-weight: 600; margin-bottom: 3px; }
    .row { margin: 3px 0; } .muted { color: var(--muted); } .warn { color: var(--high); } .ok { color: var(--low); }
    .cats { color: var(--muted); } .acts { color: var(--muted); margin-left: 2px; }
    .tag { display: inline-block; font-size: 10px; color: var(--muted); border: 1px solid var(--line); border-radius: 3px; padding: 0 4px; margin-right: 4px; }
    blockquote { margin: 3px 0 2px 0; padding-left: 8px; border-left: 2px solid var(--line); color: var(--fg); font-style: italic; }
    a { color: var(--accent); text-decoration: none; margin-right: 8px; } a:hover { text-decoration: underline; }
    .actions { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
    button, select { font: inherit; color: var(--fg); background: var(--bg); border: 1px solid var(--line); border-radius: 5px; padding: 2px 8px; cursor: pointer; }
    button:hover { border-color: var(--accent); }
    .confirm { position: fixed; top: 20px; left: 50%; transform: translateX(-50%); pointer-events: auto; background: var(--bg); color: var(--fg);
      border: 1px solid var(--line); border-left: 3px solid var(--high); border-radius: 8px; padding: 10px 14px; box-shadow: 0 6px 24px rgba(0,0,0,.25); max-width: 440px; }
    .pick { position: fixed; inset: 0; pointer-events: auto; cursor: crosshair; background: rgba(10,99,201,.04); }
    .pick .box { position: fixed; display: none; border: 1.5px dashed var(--accent); background: rgba(10,99,201,.08); }
  `;
})();
