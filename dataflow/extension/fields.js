// Finding fields and placing their data type with keyword rules, in several languages. Shared by
// content.js and the canary runner (which loads this file into its headless page on its own).
// A field the rules can't place is "other" with its context, and the helper's small classifier
// may place it later (dataflow/fields.py).
(() => {
  "use strict";
  const TYPES = ["email", "name", "phone", "address", "birthday", "payment", "password", "other"];
  const SKIP_TYPES = new Set(["hidden", "checkbox", "radio", "submit", "button", "image", "reset", "file", "range",
    "color"]);
  const AUTOCOMPLETE = [
    [/^(email)$/, "email"],
    [/^(name|given-name|family-name|additional-name|nickname|honorific-prefix|honorific-suffix)$/, "name"],
    [/^tel(-|$)/, "phone"],
    [/^(street-address|address-line\d|address-level\d|postal-code|country|country-name)$/, "address"],
    [/^bday(-|$)/, "birthday"],
    [/^cc-/, "payment"],
    [/^(new-password|current-password)$/, "password"],
  ];
  // Order matters: the first rule that matches wins.
  const RULES = [
    ["payment", /name on (the )?card|card.?holder/i],
    ["other", /(company|business|organi[sz]ation|employer|school|team|project|store|shop)[\s_.-]?name/i],
    ["password", /pass(word|wd|code)?\b|passwort|mot de passe|contrase(ñ|n)a|senha|パスワード|密码|비밀번호|пароль/i],
    ["payment", /card.?(number|no\b|num)|cc.?(num|number|cvv|cvc|exp)|credit.?card|\bcvv\b|\bcvc\b|\bcsc\b|security.?code|expir|exp.?(date|month|year)|karten?nummer|carte bancaire|n(ú|u)mero de tarjeta|\biban\b|routing.?number|account.?number/i],
    ["email", /e.?mail|courriel|correo|e-?post|posta elettronica|メール|邮箱|电子邮件|電子郵件|이메일|почт/i],
    ["birthday", /birth|\bdob\b|bday|geburt|naissance|nacimiento|nascita|anivers(á|a)rio|生日|生年月日|생년월일|рожд/i],
    ["phone", /phone|mobile|\bcell\b|\btel(efon|éfono|efone|ephone)?\b|handy|portable|m(ó|o)vil|celular|電話|手机|电话|전화|телефон/i],
    ["name", /(first|last|full|given|family|middle|sur|nick)[\s_.-]?name|^name$|\bname\b|\bfname\b|\blname\b|vorname|nachname|pr(é|e)nom|\bnom\b|nombre|apellido|cognome|\bnome\b|姓名|名前|氏名|이름|имя|фамилия/i],
    ["address", /address|street|\bcity\b|\bzip\b|postal|post.?code|\bplz\b|adresse|stra(ß|ss)e|\bville\b|code postal|direcci(ó|o)n|\bcalle\b|ciudad|indirizzo|citt(à|a)|\bcep\b|住所|地址|주소|адрес|\bstate\b|province|county/i],
  ];

  function text(node, max) {
    return String((node && (node.innerText || node.textContent)) || "").replace(/\s+/g, " ").trim().slice(0, max || 120);
  }

  function labelOf(el) {
    const parts = [];
    try {
      if (el.labels && el.labels.length) for (const l of el.labels) parts.push(text(l));
      const by = el.getAttribute("aria-labelledby");
      if (by) {
        const root = el.getRootNode();
        for (const id of by.split(/\s+/)) {
          const node = root.getElementById ? root.getElementById(id) : document.getElementById(id);
          if (node) parts.push(text(node));
        }
      }
    } catch (e) { /* ignore */ }
    return parts.filter(Boolean).join(" ");
  }

  function nearbyText(el) {
    // The text just before the field: a preceding sibling, or the parent's own words.
    let node = el;
    for (let i = 0; node && i < 3; i++) {
      let prev = node.previousElementSibling;
      while (prev && !text(prev, 80)) prev = prev.previousElementSibling;
      if (prev) return text(prev, 80);
      node = node.parentElement;
    }
    return "";
  }

  function contextOf(el) {
    const a = (name) => (el.getAttribute(name) || "").slice(0, 80);
    return {
      tag: el.nodeName.toLowerCase(), type: lower(a("type")), autocomplete: lower(a("autocomplete")),
      name: a("name"), id: a("id"), label: labelOf(el), aria: a("aria-label"), placeholder: a("placeholder"),
      title: a("title"), nearby: nearbyText(el),
    };
  }
  function lower(s) { return String(s || "").toLowerCase(); }

  function typeable(el) {
    if (!el || !el.isConnected) return false;
    if (el.nodeName === "INPUT") return !SKIP_TYPES.has(lower(el.getAttribute("type")));
    if (el.nodeName === "TEXTAREA" || el.nodeName === "SELECT") return true;
    return el.isContentEditable && el.getAttribute("contenteditable") !== null;
  }

  // {type, how: "rules"|null, context}. how null means the rules couldn't place it.
  function classify(el) {
    const c = contextOf(el);
    const type = c.type;
    if (type === "email") return { type: "email", how: "rules", context: c };
    if (type === "password") return { type: "password", how: "rules", context: c };
    if (type === "tel") return { type: "phone", how: "rules", context: c };
    for (const token of c.autocomplete.split(/\s+/)) {
      for (const [re, t] of AUTOCOMPLETE) if (re.test(token)) return { type: t, how: "rules", context: c };
    }
    // Strongest words first: what you can read (label, aria, placeholder), then the markup names.
    for (const source of [c.label, c.aria, c.placeholder, c.title, c.name + " " + c.id, c.nearby]) {
      if (!source.trim()) continue;
      for (const [t, re] of RULES) if (re.test(source)) return { type: t, how: "rules", context: c };
    }
    if (type === "date" && /birth|dob|geburt|naissance/i.test(c.name + c.id + c.label)) {
      return { type: "birthday", how: "rules", context: c };
    }
    if (type === "search" || /search|query|\bq\b|suche|recherche|buscar/i.test(c.name + " " + c.placeholder + " " + c.aria)) {
      return { type: "other", how: "rules", context: c, search: true };
    }
    return { type: "other", how: null, context: c };
  }

  // Every typeable field in a root, through open and closed shadow roots.
  function find(root, openRoot) {
    const out = [];
    const walk = (node, depth) => {
      if (!node || depth > 30) return;
      const all = node.querySelectorAll ? node.querySelectorAll("input, textarea, select, [contenteditable]") : [];
      for (const el of all) if (typeable(el)) out.push(el);
      const hosts = node.querySelectorAll ? node.querySelectorAll("*") : [];
      for (const host of hosts) {
        const shadow = openRoot ? openRoot(host) : host.shadowRoot;
        if (shadow) walk(shadow, depth + 1);
      }
    };
    walk(root, 0);
    return out;
  }

  // A CSS path the canary runner can find the field again with.
  function selectorFor(el) {
    if (el.id && /^[A-Za-z][\w-]*$/.test(el.id)) return "#" + el.id;
    const name = el.getAttribute("name");
    if (name) {
      const sel = `${el.nodeName.toLowerCase()}[name="${name.replace(/"/g, '\\"')}"]`;
      try { if (document.querySelectorAll(sel).length === 1) return sel; } catch (e) { /* odd name */ }
    }
    const parts = [];
    let node = el;
    for (let i = 0; node && node.nodeType === 1 && i < 6; i++) {
      let part = node.nodeName.toLowerCase();
      if (node.id && /^[A-Za-z][\w-]*$/.test(node.id)) { parts.unshift("#" + node.id); break; }
      const parent = node.parentElement;
      if (parent) {
        const same = Array.from(parent.children).filter((n) => n.nodeName === node.nodeName);
        if (same.length > 1) part += `:nth-of-type(${same.indexOf(node) + 1})`;
      }
      parts.unshift(part);
      node = parent;
    }
    return parts.join(" > ");
  }

  function formContext(el, widgets) {
    const form = el.form || (el.closest && el.closest("form"));
    const out = { form: !!form };
    if (form) {
      const action = form.getAttribute("action");
      try { out.action = action ? new URL(action, location.href).href : location.href; } catch (e) { out.action = action; }
      out.method = lower(form.getAttribute("method") || "get");
      out.hidden = Array.from(form.querySelectorAll("input[type=hidden]")).map((h) => h.name).filter(Boolean).slice(0, 20);
    }
    for (const [name, w] of Object.entries(widgets || {})) {
      for (const sel of w.selectors || []) {
        try { if (el.closest(sel)) { out.widget = name; break; } } catch (e) { /* bad selector */ }
      }
      if (out.widget) break;
    }
    return out;
  }

  globalThis.WDMEG_FIELDS = { TYPES, classify, contextOf, find, typeable, selectorFor, formContext };
})();
