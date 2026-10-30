// The service worker: the one connection to the Mac helper (native messaging), the blocking rules
// (declarativeNetRequest) and Global Privacy Control, and the go-between for every frame's
// content.js. Settings and lists come from the helper and are cached here, so the extension
// keeps working with the last ones while the helper isn't running.
"use strict";

const HOST = "io.github.roocxre.macutils.dataflow";      // dataflow.HOST_NAME
const ALL_TYPES = ["main_frame", "sub_frame", "stylesheet", "script", "image", "font", "object", "xmlhttprequest",
  "ping", "csp_report", "media", "websocket", "webtransport", "webbundle", "other"];
const SUB_TYPES = ALL_TYPES.filter((t) => t !== "main_frame");
const GPC_RULE = 1;
const FIRST_BLOCK_RULE = 100;

let port = null;
let helperUp = false;
let hello = null;
let psl = null;
const pending = new Map();
let seq = 1;
const ruleCategory = new Map();

// ------------------------------------------------------------------ the helper
function connect() {
  if (port) return port;
  try {
    port = chrome.runtime.connectNative(HOST);
  } catch (e) {
    port = null;
    return null;
  }
  port.onMessage.addListener(onHost);
  port.onDisconnect.addListener(() => {
    void chrome.runtime.lastError;
    port = null;
    helperUp = false;
    for (const p of pending.values()) p({ error: "the Mac helper disconnected" });
    pending.clear();
  });
  return port;
}

function call(op, args = {}, timeout = 20000) {
  return new Promise((resolve) => {
    const p = connect();
    if (!p) { resolve({ error: "the Mac helper isn't registered (.venv/bin/python manage.py setup dataflow --only native-host)" }); return; }
    const id = seq++;
    const timer = setTimeout(() => { pending.delete(id); resolve({ error: "the Mac helper took too long" }); }, timeout);
    pending.set(id, (answer) => { clearTimeout(timer); resolve(answer); });
    try {
      p.postMessage({ id, op, ...args });
    } catch (e) {
      pending.delete(id);
      clearTimeout(timer);
      resolve({ error: String(e) });
    }
  });
}

function onHost(message) {
  if (message.push) { onPush(message); return; }
  const done = pending.get(message.id);
  if (!done) return;
  pending.delete(message.id);
  helperUp = true;
  done(message.error ? { error: message.error } : { ok: message.ok });
}

function onPush(message) {
  if (message.push === "site") {
    broadcast({ type: "site-updated", site: message.site });
  } else if (message.push === "settings") {
    if (hello) {
      Object.assign(hello, { modes: message.modes, overlay_off: message.overlay_off, version: message.version });
      chrome.storage.local.set({ hello });
      applyRules(hello);
    }
    broadcast({ type: "settings", ...message });
  }
}

async function broadcast(message) {
  const tabs = await chrome.tabs.query({});
  for (const tab of tabs) {
    if (tab.id >= 0) chrome.tabs.sendMessage(tab.id, message).catch(() => {});
  }
}

// ------------------------------------------------------------------ registrable domains
function registrable(host) {
  host = String(host || "").toLowerCase().replace(/\.$/, "");
  if (!host || /^[\d.]+$/.test(host) || host.includes(":")) return host;
  const labels = host.split(".");
  if (!psl) return labels.slice(-2).join(".");
  for (let i = 0; i < labels.length; i++) {
    const candidate = labels.slice(i).join(".");
    if (psl.exception.has(candidate)) return labels.slice(i).join(".");
    const isSuffix = psl.normal.has(candidate) || (i + 1 < labels.length && psl.wildcard.has(labels.slice(i + 1).join(".")));
    if (isSuffix) return i > 0 ? labels.slice(i - 1).join(".") : host;
  }
  return labels.slice(-2).join(".");
}
function hostOf(url) {
  try { return new URL(url).hostname; } catch (e) { return ""; }
}
function usePsl(data) {
  if (data && data.psl) {
    psl = { normal: new Set(data.psl.normal), wildcard: new Set(data.psl.wildcard), exception: new Set(data.psl.exception) };
  }
}

// ------------------------------------------------------------------ blocking rules and GPC
async function applyRules(h) {
  const observe = Object.entries(h.modes || {}).filter(([, m]) => m === "observe").map(([d]) => d);
  const rules = [{
    id: GPC_RULE, priority: 1,
    action: { type: "modifyHeaders", requestHeaders: [{ header: "Sec-GPC", operation: "set", value: "1" }] },
    condition: { resourceTypes: ALL_TYPES },
  }];
  ruleCategory.clear();
  let id = FIRST_BLOCK_RULE;
  for (const [category, domains] of Object.entries(h.blocks || {})) {
    for (let i = 0; i < domains.length; i += 1000) {
      const condition = { requestDomains: domains.slice(i, i + 1000), domainType: "thirdParty", resourceTypes: SUB_TYPES };
      if (observe.length) condition.excludedInitiatorDomains = observe;
      ruleCategory.set(id, category);
      rules.push({ id: id++, priority: 2, action: { type: "block" }, condition });
    }
  }
  try {
    const existing = await chrome.declarativeNetRequest.getDynamicRules();
    await chrome.declarativeNetRequest.updateDynamicRules({ removeRuleIds: existing.map((r) => r.id), addRules: rules });
  } catch (e) {
    console.warn("blocking rules not applied", e);
  }
}

// Unpacked extensions can see which rule matched: tell the page which address was blocked.
const told = new Map();
if (chrome.declarativeNetRequest.onRuleMatchedDebug) {
  chrome.declarativeNetRequest.onRuleMatchedDebug.addListener((info) => {
    const tabId = info.request.tabId;
    if (tabId < 0 || info.rule.ruleId < FIRST_BLOCK_RULE) return;
    const domain = registrable(hostOf(info.request.url));
    const key = tabId + " " + domain;
    if (told.has(key)) return;
    told.set(key, Date.now());
    if (told.size > 500) told.clear();
    chrome.tabs.sendMessage(tabId, { type: "net-blocked", domain, category: ruleCategory.get(info.rule.ruleId) },
      { frameId: 0 }).catch(() => {});
  });
}

// ------------------------------------------------------------------ start
async function init() {
  const cached = await chrome.storage.local.get("hello");
  if (cached.hello) { hello = cached.hello; usePsl(hello); }
  const answer = await call("hello", {}, 60000);
  if (answer.ok) {
    const first = !hello;
    hello = answer.ok;
    usePsl(hello);
    chrome.storage.local.set({ hello });
    await applyRules(hello);
    // Pages that started before the helper answered got a config without your addresses: refresh them.
    if (first) broadcast({ type: "config" });
  } else if (hello) {
    await applyRules(hello);
  } else {
    await applyRules({ modes: {}, blocks: {} });     // GPC at least
  }
}
const ready = init();

// ------------------------------------------------------------------ the frames' requests
function configFor(topUrl) {
  const h = hello || {};
  const site = registrable(hostOf(topUrl));
  return {
    site, mode: (h.modes || {})[site] || h.default_mode || "block", needles: h.needles || { ci: [], cs: [] },
    after_submit: h.after_submit || [], login_hosts: h.login_hosts || [], widgets: h.widgets || {}, payment_frames: h.payment_frames || {},
    consent_tools: h.consent_tools || {}, overlay: !(h.overlay_off || []).includes(site), helper: helperUp,
  };
}

async function setSiteSetting(op, site, args) {
  const answer = await call(op, { site, ...args });
  if (answer.ok && hello) {
    if (op === "mode") hello.modes = { ...(hello.modes || {}), [site]: args.mode };
    if (op === "overlay") {
      const off = new Set(hello.overlay_off || []);
      if (args.on) off.delete(site); else off.add(site);
      hello.overlay_off = [...off];
    }
    chrome.storage.local.set({ hello });
    if (op === "mode") await applyRules(hello);
    broadcast({ type: "settings", modes: hello.modes, overlay_off: hello.overlay_off, default_mode: hello.default_mode });
  }
  return answer;
}

async function handle(message, sender) {
  if (!hello) await ready;
  const topUrl = (sender.tab && sender.tab.url) || message.url || "";
  const site = registrable(hostOf(topUrl));
  switch (message.type) {
    case "config":
      // A cold start: give the helper's hello up to 3 seconds before answering without it.
      if (!helperUp) await Promise.race([ready, new Promise((r) => setTimeout(r, 3000))]);
      return { ok: configFor(topUrl) };
    case "resolve":
      return call("resolve", { site, urls: message.urls || [] });
    case "site":
      return call("site", { site, page_url: topUrl, policy_links: message.policy_links, signals: message.signals });
    case "page":
      return call("page", { site, url: message.url, risk: message.risk, fields: message.fields, recipients: message.recipients });
    case "policy":
      return call("policy", { site, data_type: message.data_type });
    case "mode":
      return setSiteSetting("mode", site, { mode: message.mode });
    case "overlay":
      return setSiteSetting("overlay", site, { on: message.on });
    case "canary":
      return call("canary", { site, url: message.url, selector: message.selector, field_type: message.field_type });
    case "submitted":
      return call("submitted", { site, url: message.url, real_email: message.real_email, domains: message.domains });
    case "blocked":
      return call("blocked", { site, domain: message.domain, kind: message.kind, detail: message.detail });
    case "classify_fields":
      return call("classify_fields", { contexts: message.contexts }, 30000);
    default:
      return { error: `unknown request ${message.type}` };
  }
}

chrome.runtime.onMessage.addListener((message, sender, reply) => {
  handle(message, sender).then(reply, (e) => reply({ error: String(e) }));
  return true;
});

// ------------------------------------------------------------------ the toolbar button and shortcuts
chrome.action.onClicked.addListener((tab) => {
  chrome.tabs.sendMessage(tab.id, { type: "analyze-all" }).catch(() => {});
});
chrome.commands.onCommand.addListener((command, tab) => {
  if (!tab) return;
  if (command === "analyze-all") chrome.tabs.sendMessage(tab.id, { type: "analyze-all" }).catch(() => {});
  if (command === "select-fields") chrome.tabs.sendMessage(tab.id, { type: "select-fields" }, { frameId: 0 }).catch(() => {});
});
