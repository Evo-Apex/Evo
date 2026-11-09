// 用可执行的脚本逻辑验证 base.html 的两段事件委托（不依赖真实浏览器）。
// 输入：渲染后的设置页 HTML；输出 JSON：每项 true/false。
import fs from "node:fs";

const html = fs.readFileSync(process.argv[2], "utf8");
const results = {};

function extract(marker) {
  const start = html.indexOf(marker);
  if (start < 0) return null;
  const open = html.lastIndexOf("<script>", start);
  const end = html.indexOf("</script>", start);
  return html.slice(open + 8, end);
}

function makeDocument() {
  const handlers = {};
  const document = {
    addEventListener(type, fn) { (handlers[type] ||= []).push(fn); },
    getElementById(id) { return document.__elements[id] || null; },
    dispatch(type, target, extra = {}) {
      const event = { type, target, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, ...extra };
      for (const fn of handlers[type] || []) fn(event);
      return event;
    },
    __elements: {},
  };
  return document;
}

const external = extract("// 明确标了 data-external-link");
const partner = extract("var lastOpener = null");

if (external) {
  const calls = [];
  const document = makeDocument();
  const window = { pywebview: { api: { open_external: (u) => { calls.push(u); return Promise.resolve({ ok: true }); } } } };
  new Function("document", "window", external)(document, window);
  const link = { href: "https://github.com/Evo-Apex/Evo", closest: (sel) => sel === "a[data-external-link]" ? link : null };
  let event = document.dispatch("click", link);
  results.external_calls_api = calls.length === 1 && calls[0] === link.href;
  results.external_keeps_page = event.defaultPrevented;

  const document2 = makeDocument();
  const window2 = {};
  new Function("document", "window2", external.replaceAll("window.", "window2."))(document2, window2);
  const link2 = { href: "https://example.com/", closest: (sel) => sel === "a[data-external-link]" ? link2 : null };
  event = document2.dispatch("click", link2);
  results.external_browser_keeps_default = !event.defaultPrevented;
} else {
  results.external_calls_api = false;
  results.external_keeps_page = false;
  results.external_browser_keeps_default = false;
}

if (partner) {
  const document = makeDocument();
  const dialog = {
    open: false,
    handlers: {},
    showModal() { this.open = true; },
    close() { this.open = false; for (const fn of this.handlers.close || []) fn({ type: "close" }); },
    addEventListener(type, fn) { (this.handlers[type] ||= []).push(fn); },
    dispatch(type, target, extra = {}) { for (const fn of this.handlers[type] || []) fn({ type, target, preventDefault() { this.defaultPrevented = true; }, ...extra }); },
    focus() {},
  };
  document.__elements["partner-dialog"] = dialog;
  new Function("document", "window", partner)(document, {});
  const top = { id: "partner-open", closest: (sel) => sel.includes("#partner-open") ? top : null, focus() {} };
  const card = { id: "card", closest: (sel) => sel.includes("[data-partner-open]") ? card : null, focus() {} };
  const close = { closest: (sel) => sel === "[data-partner-close]" ? close : null };

  document.dispatch("click", top);
  results.partner_top_opens = dialog.open === true;
  document.dispatch("click", close);
  const closed = dialog.open === false;
  document.dispatch("click", card);
  results.partner_card_opens = dialog.open === true;
  // Esc：dialog 自己的 keydown 监听
  dialog.dispatch("keydown", dialog, { key: "Escape" });
  results.partner_esc_closes = dialog.open === false;
  // 再开一次，点 backdrop：target 就是 dialog，由 dialog 自己的 click 监听处理
  document.dispatch("click", card);
  dialog.dispatch("click", dialog);
  results.partner_backdrop_closes = dialog.open === false;
  document.dispatch("click", top);
  dialog.dispatchEvent ? dialog.dispatchEvent({ type: "close" }) : null;
  results.partner_close_button = closed;
} else {
  results.partner_top_opens = false;
  results.partner_card_opens = false;
  results.partner_backdrop_closes = false;
  results.partner_close_button = false;
}

process.stdout.write(JSON.stringify(results));
