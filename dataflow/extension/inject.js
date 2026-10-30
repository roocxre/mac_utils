// The page-world half (world: MAIN, document_start, every frame). It wraps the functions scripts use
// to watch fields and send data, before the page's own scripts run, and works out which script made
// each call. It reports to content.js over a private MessageChannel. Its config (mode, your
// addresses' encoded forms) comes back the same way.
//
// Known limit: a determined script can still find unwrapped copies (a fresh cross-origin frame, a
// worker). Same-origin frames are wrapped as soon as anything touches their window.
(() => {
  "use strict";
  const W = window;
  try {
    if (Object.getOwnPropertyDescriptor(W, "__wdmeg")) return;
    Object.defineProperty(W, "__wdmeg", { value: true });
  } catch (e) { return; }

  // Hosted payment fields: leave the frame entirely alone.
  const PAYMENT = /(^|\.)(js\.stripe\.com|m\.stripe\.network|hooks\.stripe\.com|checkout\.stripe\.com|assets\.braintreegateway\.com|payments\.braintree-api\.com|checkoutshopper-[a-z-]+\.adyen\.com|web\.squarecdn\.com|pci-connect\.squareup\.com|checkout\.shopifycs\.com|api\.recurly\.com)$/;
  if (PAYMENT.test(location.hostname)) return;

  const apply = Reflect.apply, construct = Reflect.construct, defineProperty = Object.defineProperty;
  const getDesc = Object.getOwnPropertyDescriptor;
  const O = {
    addEventListener: EventTarget.prototype.addEventListener,
    dispatchEvent: EventTarget.prototype.dispatchEvent,
    fetch: W.fetch,
    xhrOpen: XMLHttpRequest.prototype.open,
    xhrSend: XMLHttpRequest.prototype.send,
    beacon: Navigator.prototype.sendBeacon,
    wsSend: WebSocket.prototype.send,
    portPost: MessagePort.prototype.postMessage,
    winPost: W.postMessage,
    appendChild: Node.prototype.appendChild,
    insertBefore: Node.prototype.insertBefore,
    replaceChild: Node.prototype.replaceChild,
    append: Element.prototype.append,
    prepend: Element.prototype.prepend,
    after: Element.prototype.after,
    before: Element.prototype.before,
    setAttribute: Element.prototype.setAttribute,
    attachShadow: Element.prototype.attachShadow,
    inputValue: getDesc(HTMLInputElement.prototype, "value"),
    textValue: getDesc(HTMLTextAreaElement.prototype, "value"),
    imgSrc: getDesc(HTMLImageElement.prototype, "src"),
    scriptSrc: getDesc(HTMLScriptElement.prototype, "src"),
    scriptText: getDesc(Node.prototype, "textContent"),
    frameWindow: getDesc(HTMLIFrameElement.prototype, "contentWindow"),
    frameDocument: getDesc(HTMLIFrameElement.prototype, "contentDocument"),
    FormData: W.FormData,
    MessageChannel: W.MessageChannel,
    setTimeout: W.setTimeout,
    URL: W.URL,
    Blob: W.Blob,
    TextDecoder: W.TextDecoder,
    Error: W.Error,
    now: Date.now,
    random: Math.random,
    toLower: String.prototype.toLowerCase,
    includes: String.prototype.includes,
  };
  const lower = (s) => apply(O.toLower, s, []);
  const has = (s, n) => apply(O.includes, s, [n]);

  // ---------------------------------------------------------------- state
  const PAGE_URL = location.href.split("#")[0];
  let topSite = siteOf(location.hostname);
  let mode = "block";
  let needles = { ci: [], cs: [] };
  let afterSubmit = [];
  let loginHosts = [];                // sign-in providers: what you send them is how you sign in
  // In a frame, its own site counts as the place you're signing in to right after you click or type in it
  // (Google's sign-in button and one-tap frames on another site).
  const FRAME_SITE = W !== W.top ? siteOf(location.hostname) : null;
  let lastUserAction = 0;
  let configured = false;
  const fields = new Map();           // cid -> element
  const fieldIds = new WeakMap();     // element -> cid
  const listeners = new WeakMap();    // target -> [{type, who}]
  const globalListeners = [];         // on window/document: [{type, who}]
  const scriptBy = new Map();         // script URL -> the script that inserted it
  const inlineVendor = new Map();     // wdmeg-inline://… -> the one third-party script it loaded
  const seenRead = new Map();         // cid -> {who: last time}
  let inlineCount = 0;
  const SECRET = "wdmeg-" + Math.floor(O.random() * 1e12).toString(36) + Math.floor(O.random() * 1e12).toString(36);

  function siteOf(host) {
    // Until content.js sends the site the Public Suffix List gives, the last two labels (three for
    // a two-part suffix like co.uk) are a close enough guess.
    host = lower(String(host || "")).replace(/\.$/, "");
    const labels = host.split(".");
    if (labels.length <= 2 || /^[\d.]+$|:/.test(host)) return host;     // an IP address is its own site
    const two = labels.slice(-2).join(".");
    if (/^(co|com|org|net|ac|gov|edu)\.[a-z]{2}$/.test(two)) return labels.slice(-3).join(".");
    return two;
  }
  function under(host, domain) {
    host = lower(String(host || "")).replace(/\.$/, "");
    return host === domain || (host.length > domain.length && host.endsWith("." + domain));
  }
  const isFirst = (host) => under(host, topSite);
  function hostOf(url) {
    try { return new O.URL(url, PAGE_URL).hostname; } catch (e) { return ""; }
  }

  // ---------------------------------------------------------------- the channel to content.js
  const channel = new O.MessageChannel();
  const port = channel.port1;
  let queue = [];
  let flushing = false;
  function report(item) {
    queue.push(item);
    if (!flushing) {
      flushing = true;
      apply(O.setTimeout, W, [() => {
        flushing = false;
        const batch = queue;
        queue = [];
        try { apply(O.portPost, port, [{ batch }]); } catch (e) { /* closed */ }
      }, 150]);
    }
  }
  port.onmessage = (event) => {
    const data = event.data || {};
    if (data.config) {
      const c = data.config;
      mode = c.mode || mode;
      needles = c.needles || needles;
      afterSubmit = c.after_submit || afterSubmit;
      loginHosts = c.login_hosts || loginHosts;
      if (c.top_site) topSite = c.top_site;
      configured = true;
    }
    if (data.submit) lastUserAction = O.now();
  };
  apply(O.winPost, W, [{ __wdmeg_hello: SECRET }, "*", [channel.port2]]);

  // ---------------------------------------------------------------- which script is calling
  const FRAME = /(?:blob:)?((?:https?|wdmeg-inline):\/\/[^\s()]+?):\d+:\d+/;
  function caller() {
    // A page's own Error.prepareStackTrace (source-map tools set one) would change the format: off for this one.
    const limit = O.Error.stackTraceLimit, prepare = O.Error.prepareStackTrace;
    O.Error.stackTraceLimit = 60;
    if (prepare) O.Error.prepareStackTrace = undefined;
    let stack = "";
    try { stack = String(new O.Error().stack || ""); } finally {
      O.Error.stackTraceLimit = limit;
      if (prepare) O.Error.prepareStackTrace = prepare;
    }
    let outermost = null;
    const lines = stack.split("\n");
    for (let i = lines.length - 1; i > 0; i--) {
      const m = FRAME.exec(lines[i]);
      if (m) { outermost = m[1]; break; }
    }
    if (!outermost || outermost.split("#")[0] === PAGE_URL) return "inline";
    return inlineVendor.get(outermost) || outermost;
  }
  function thirdParty(who) {
    if (who === "inline") return false;
    const host = has(who, "wdmeg-inline://") ? who.slice(15).split("/")[0] : hostOf(who);
    return !!host && !isFirst(host);
  }

  // ---------------------------------------------------------------- script insertion chain
  function noteInsert(node) {
    if (!node || node.nodeName !== "SCRIPT") return;
    const who = caller();
    const src = apply(O.scriptSrc.get, node, []);
    if (src) {
      if (!scriptBy.has(src)) {
        scriptBy.set(src, who);
        report({ k: "script", url: src, by: who });
        // An inline tag inserted by a tag manager that loads one vendor's script is that vendor's.
        if (has(who, "wdmeg-inline://") && thirdParty(src) && !inlineVendor.has(who)) inlineVendor.set(who, src);
      }
      if (needsBlock(src, "")) {
        apply(O.setAttribute, node, ["src", "data:text/javascript,"]);
        report({ k: "blocked", how: "script", dest: hostOf(src), who });
      }
    } else if (who !== "inline" && thirdParty(who) && isJavaScript(node)) {
      // Name it, so calls from inside it are traced back to whoever inserted it.
      const name = "wdmeg-inline://" + hostOf(who) + "/" + (++inlineCount);
      try {
        apply(O.scriptText.set, node, [apply(O.scriptText.get, node, []) + "\n//# sourceURL=" + name]);
        scriptBy.set(name, who);
        report({ k: "script", url: name, by: who });
      } catch (e) { /* frozen */ }
    }
  }
  // Only JavaScript can carry a sourceURL comment: not JSON-LD, speculation rules, import maps, templates.
  const JS_TYPES = /^((text|application)\/(x-)?(java|ecma)script|module)$/;
  function isJavaScript(node) {
    const type = lower(String(node.getAttribute("type") || "").trim());
    return !type || JS_TYPES.test(type);
  }
  function wrapInsert(proto, name, nodesFrom) {
    const original = proto[name];
    if (!original) return;
    proto[name] = new Proxy(original, {
      apply(target, self, args) {
        try { for (const n of nodesFrom(args)) noteInsert(n); } catch (e) { /* never break the page */ }
        return apply(target, self, args);
      },
    });
  }
  wrapInsert(Node.prototype, "appendChild", (a) => [a[0]]);
  wrapInsert(Node.prototype, "insertBefore", (a) => [a[0]]);
  wrapInsert(Node.prototype, "replaceChild", (a) => [a[0]]);
  for (const name of ["append", "prepend", "after", "before"]) {
    wrapInsert(Element.prototype, name, (a) => a);
  }

  // ---------------------------------------------------------------- listeners
  const WATCH = new Set(["input", "change", "keydown", "keyup", "keypress", "beforeinput", "blur", "focusout",
    "paste", "submit"]);
  function addListener(target, type, who) {
    const record = { type, who };
    if (target === W || target === document) {
      globalListeners.push(record);
      report({ k: "listen", f: 0, who, type });
      return;
    }
    let list = listeners.get(target);
    if (!list) { list = []; listeners.set(target, list); }
    list.push(record);
    for (const [cid, el] of fields) {
      if (contains(target, el)) report({ k: "listen", f: cid, who, type });
    }
  }
  function contains(ancestor, el) {
    let node = el;
    for (let i = 0; node && i < 400; i++) {
      if (node === ancestor) return true;
      node = node.parentNode || node.host || null;
    }
    return false;
  }
  EventTarget.prototype.addEventListener = new Proxy(O.addEventListener, {
    apply(target, self, args) {
      try {
        if (args[1] && WATCH.has(args[0]) && (self === W || self === document || self instanceof Element)) {
          addListener(self, args[0], caller());
        }
      } catch (e) { /* never break the page */ }
      return apply(target, self, args);
    },
  });
  // on<event> properties
  for (const [proto, props] of [[HTMLElement.prototype, ["oninput", "onchange", "onkeydown", "onkeyup", "onkeypress",
    "onblur", "onpaste", "onsubmit"]], [Document.prototype, ["oninput", "onkeydown", "onkeyup", "onkeypress",
    "onchange"]]]) {
    for (const prop of props) {
      const d = getDesc(proto, prop) || getDesc(HTMLElement.prototype, prop);
      if (!d || !d.set) continue;
      defineProperty(proto, prop, {
        configurable: true, enumerable: d.enumerable, get: d.get,
        set: new Proxy(d.set, {
          apply(target, self, args) {
            try { if (args[0]) addListener(self, prop.slice(2), caller()); } catch (e) { /* ignore */ }
            return apply(target, self, args);
          },
        }),
      });
    }
  }

  // ---------------------------------------------------------------- shadow roots (closed ones too)
  Element.prototype.attachShadow = new Proxy(O.attachShadow, {
    apply(target, self, args) {
      const root = apply(target, self, args);
      try { apply(O.addEventListener, root, [SECRET, onTag, true]); } catch (e) { /* ignore */ }
      return root;
    },
  });

  // content.js tags each field it found with a CustomEvent named SECRET, carrying its id.
  function onTag(event) {
    const el = event.composedPath()[0];
    const cid = event.detail;
    if (!el || typeof cid !== "number" || fieldIds.has(el)) return;
    if (el.nodeName !== "INPUT" && el.nodeName !== "TEXTAREA" && el.nodeName !== "SELECT" && !el.isContentEditable) return;
    fieldIds.set(el, cid);
    fields.set(cid, el);
    for (const target of ancestors(el)) {
      for (const r of listeners.get(target) || []) report({ k: "listen", f: cid, who: r.who, type: r.type });
    }
  }
  function ancestors(el) {
    const out = [];
    let node = el;
    for (let i = 0; node && i < 400; i++) {
      out.push(node);
      node = node.parentNode || node.host || null;
    }
    return out;
  }
  apply(O.addEventListener, document, [SECRET, onTag, true]);

  // ---------------------------------------------------------------- value reads
  function onRead(el) {
    const cid = fieldIds.get(el);
    if (!cid) return null;
    const now = O.now();
    let seen = seenRead.get(cid);
    if (!seen) { seen = { at: 0, who: new Set() }; seenRead.set(cid, seen); }
    if (now - seen.at < 40 && mode !== "strict") return null;
    seen.at = now;
    const who = caller();
    if (!seen.who.has(who)) {
      seen.who.add(who);
      report({ k: "read", f: cid, who });
    }
    return who;
  }
  function wrapValue(proto, desc) {
    defineProperty(proto, "value", {
      configurable: true, enumerable: desc.enumerable, set: desc.set,
      get: new Proxy(desc.get, {
        apply(target, self, args) {
          const value = apply(target, self, args);
          try {
            const who = onRead(self);
            if (mode === "strict" && who && thirdParty(who) && value) {
              report({ k: "hidden", f: fieldIds.get(self), who });
              return "";
            }
          } catch (e) { /* ignore */ }
          return value;
        },
      }),
    });
  }
  wrapValue(HTMLInputElement.prototype, O.inputValue);
  wrapValue(HTMLTextAreaElement.prototype, O.textValue);

  W.FormData = new Proxy(O.FormData, {
    construct(target, args, newTarget) {
      const data = construct(target, args, newTarget);
      try {
        const form = args[0];
        if (form && form.nodeName === "FORM") {
          const who = caller();
          const hide = mode === "strict" && thirdParty(who);
          for (const [cid, el] of fields) {
            if (el.form !== form) continue;
            report({ k: "read", f: cid, who, via: "FormData" });
            if (hide && el.name && data.has(el.name)) data.set(el.name, "");
          }
        }
      } catch (e) { /* ignore */ }
      return data;
    },
  });

  // ---------------------------------------------------------------- what leaves the page
  function toText(body) {
    if (body == null) return "";
    if (typeof body === "string") return body;
    try {
      if (body instanceof URLSearchParams) return body.toString();
      if (body instanceof O.FormData) {
        let out = "";
        for (const [k, v] of body.entries()) out += k + "=" + (typeof v === "string" ? v : "") + "&";
        return out;
      }
      if (body instanceof ArrayBuffer || ArrayBuffer.isView(body)) {
        return new O.TextDecoder().decode(body instanceof ArrayBuffer ? new Uint8Array(body) : body).slice(0, 200000);
      }
      if (typeof body === "object" && !(body instanceof O.Blob) && !(body instanceof ReadableStream)) return String(body);
    } catch (e) { /* unreadable */ }
    return "";
  }
  function hasEmail(hay) {
    if (!hay) return false;
    const low = lower(hay);
    for (const n of needles.ci) if (has(low, n)) return true;
    for (const n of needles.cs) if (has(hay, n)) return true;
    return false;
  }
  function fieldsIn(hay) {
    const out = [];
    if (!hay) return out;
    const low = lower(hay);
    for (const [cid, el] of fields) {
      let value = "";
      try {
        value = el.nodeName === "TEXTAREA" ? apply(O.textValue.get, el, []) : el.nodeName === "INPUT" ?
          apply(O.inputValue.get, el, []) : "";
      } catch (e) { /* ignore */ }
      if (value && value.length >= 4) {
        const v = lower(value);
        if (has(low, v) || has(low, lower(encodeURIComponent(value)))) out.push(cid);
      }
    }
    return out;
  }
  // Report what a request carries; true when it must be stopped (your real email to a third party).
  function inspect(url, body, who, how) {
    let dest = "";
    try { dest = new O.URL(url, PAGE_URL).hostname; } catch (e) { return false; }
    const third = !isFirst(dest);
    const hay = String(url) + "\n" + body;
    const carried = fieldsIn(hay);
    const real = configured && hasEmail(hay);
    if (carried.length || real) report({ k: "tx", who, dest, how, fields: carried, real, third });
    if (real && third && mode !== "observe") {
      const recent = O.now() - lastUserAction;
      const exempt = loginHosts.some((d) => under(dest, d)) ||
        (recent < 5000 && afterSubmit.some((d) => under(dest, d))) ||
        (recent < 10000 && FRAME_SITE && under(dest, FRAME_SITE));
      if (!exempt) {
        report({ k: "blocked", how, dest, who, what: "your email" });
        return true;
      }
    }
    return false;
  }
  function needsBlock(url, body) {
    try { return inspect(url, body, caller(), "load"); } catch (e) { return false; }
  }
  async function blobText(blob) {
    try { return blob.size < 1e6 ? await blob.text() : ""; } catch (e) { return ""; }
  }

  W.fetch = new Proxy(O.fetch, {
    apply(target, self, args) {
      let who, url, init, request;
      try {
        who = caller();
        request = args[0] instanceof Request ? args[0] : null;
        url = request ? request.url : String(args[0]);
        init = args[1] || {};
      } catch (e) {
        return apply(target, self, args);
      }
      return (async () => {
        let body = "";
        try {
          if (init.body != null) body = init.body instanceof O.Blob ? await blobText(init.body) : toText(init.body);
          else if (request && request.body) body = await request.clone().text();
        } catch (e) { /* unreadable */ }
        let stop = false;
        try { stop = inspect(url, body, who, "fetch"); } catch (e) { /* ignore */ }
        if (stop) throw new TypeError("Failed to fetch");
        return apply(target, self, args);
      })();
    },
  });

  const xhrInfo = new WeakMap();
  XMLHttpRequest.prototype.open = new Proxy(O.xhrOpen, {
    apply(target, self, args) {
      try { xhrInfo.set(self, { url: String(args[1]), who: caller() }); } catch (e) { /* ignore */ }
      return apply(target, self, args);
    },
  });
  XMLHttpRequest.prototype.send = new Proxy(O.xhrSend, {
    apply(target, self, args) {
      const info = xhrInfo.get(self);
      if (!info) return apply(target, self, args);
      const fail = () => {
        for (const type of ["error", "loadend"]) {
          try { apply(O.dispatchEvent, self, [new ProgressEvent(type)]); } catch (e) { /* ignore */ }
        }
      };
      if (args[0] instanceof O.Blob) {
        blobText(args[0]).then((text) => {
          if (inspect(info.url, text, info.who, "xhr")) fail();
          else apply(target, self, args);
        });
        return undefined;
      }
      let stop = false;
      try { stop = inspect(info.url, toText(args[0]), info.who, "xhr"); } catch (e) { /* ignore */ }
      if (stop) { O.setTimeout(fail, 0); return undefined; }
      return apply(target, self, args);
    },
  });

  if (O.beacon) {
    Navigator.prototype.sendBeacon = new Proxy(O.beacon, {
      apply(target, self, args) {
        let who;
        try { who = caller(); } catch (e) { return apply(target, self, args); }
        if (args[1] instanceof O.Blob) {
          blobText(args[1]).then((text) => {
            if (!inspect(String(args[0]), text, who, "beacon")) apply(target, self, args);
          });
          return true;
        }
        let stop = false;
        try { stop = inspect(String(args[0]), toText(args[1]), who, "beacon"); } catch (e) { /* ignore */ }
        return stop ? true : apply(target, self, args);
      },
    });
  }

  WebSocket.prototype.send = new Proxy(O.wsSend, {
    apply(target, self, args) {
      let stop = false;
      try { stop = inspect(self.url, toText(args[0]), caller(), "websocket"); } catch (e) { /* ignore */ }
      return stop ? undefined : apply(target, self, args);
    },
  });

  MessagePort.prototype.postMessage = new Proxy(O.portPost, {
    apply(target, self, args) {
      try {
        if (self !== port && configured && typeof args[0] === "string" && hasEmail(args[0])) {
          report({ k: "tx", who: caller(), dest: "", how: "postMessage", fields: [], real: true, third: true });
        }
      } catch (e) { /* ignore */ }
      return apply(target, self, args);
    },
  });

  // Pixels: an image whose address carries your email.
  defineProperty(HTMLImageElement.prototype, "src", {
    configurable: true, enumerable: O.imgSrc.enumerable, get: O.imgSrc.get,
    set: new Proxy(O.imgSrc.set, {
      apply(target, self, args) {
        let stop = false;
        try { stop = inspect(String(args[0]), "", caller(), "pixel"); } catch (e) { /* ignore */ }
        return stop ? undefined : apply(target, self, args);
      },
    }),
  });
  Element.prototype.setAttribute = new Proxy(O.setAttribute, {
    apply(target, self, args) {
      try {
        if (self.nodeName === "IMG" && lower(String(args[0])) === "src" && inspect(String(args[1]), "", caller(), "pixel")) {
          return undefined;
        }
      } catch (e) { /* ignore */ }
      return apply(target, self, args);
    },
  });

  // Same-origin frames get the same wrappers the moment anything reaches their window.
  function guardFrame(desc, prop) {
    defineProperty(HTMLIFrameElement.prototype, prop, {
      configurable: true, enumerable: desc.enumerable,
      get: new Proxy(desc.get, {
        apply(target, self, args) {
          const value = apply(target, self, args);
          try {
            const win = prop === "contentWindow" ? value : value && value.defaultView;
            if (win && !getDesc(win, "__wdmeg")) wrapFrame(win);
          } catch (e) { /* cross-origin */ }
          return value;
        },
      }),
    });
  }
  function wrapFrame(win) {
    // A same-origin child shares our reporting: route its network functions through ours.
    defineProperty(win, "__wdmeg", { value: true });
    win.fetch = W.fetch;
    win.XMLHttpRequest.prototype.open = XMLHttpRequest.prototype.open;
    win.XMLHttpRequest.prototype.send = XMLHttpRequest.prototype.send;
    win.Navigator.prototype.sendBeacon = Navigator.prototype.sendBeacon;
    win.WebSocket.prototype.send = WebSocket.prototype.send;
    win.EventTarget.prototype.addEventListener = EventTarget.prototype.addEventListener;
    win.FormData = W.FormData;
  }
  guardFrame(O.frameWindow, "contentWindow");
  guardFrame(O.frameDocument, "contentDocument");

  // ---------------------------------------------------------------- you acting, and GPC
  apply(O.addEventListener, document, ["submit", (e) => { if (e.isTrusted) lastUserAction = O.now(); }, true]);
  apply(O.addEventListener, document, ["click", (e) => {
    if (!e.isTrusted) return;
    const t = e.target && e.target.closest ? e.target.closest("button, input[type=submit], [role=button], a") : null;
    if (t) lastUserAction = O.now();
  }, true]);
  apply(O.addEventListener, document, ["keydown", (e) => {
    if (e.isTrusted && (e.key === "Enter" || FRAME_SITE)) lastUserAction = O.now();
  }, true]);
  try {
    defineProperty(Navigator.prototype, "globalPrivacyControl", { configurable: true, enumerable: true, get: () => true });
  } catch (e) { /* ignore */ }
})();
