"use strict";
const checks = document.getElementById("checks");
const next = document.getElementById("next");
const time = document.getElementById("time");
const refresh = document.getElementById("refresh");
const names = ["运行环境", "OpenCode CLI", "bubblewrap 沙盒", "服务商认证", "模型与 ACP"];
const labels = {pass: "通过", action: "需要处理", unknown: "未验证", loading: "正在检查", disabled: "未启用"};

function element(tag, text) {
  const node = document.createElement(tag);
  node.textContent = text;
  return node;
}

function render(rows) {
  checks.replaceChildren();
  for (const row of rows) {
    const card = document.createElement("article");
    const heading = document.createElement("div");
    heading.className = "heading";
    const badge = element("span", labels[row.status] || labels.unknown);
    badge.className = "badge " + row.status;
    heading.append(element("h2", row.title), badge);
    card.append(heading);
    if (row.detail) card.append(element("p", row.detail));
    const facts = document.createElement("dl");
    for (const [name, value] of Object.entries(row.facts || {})) {
      facts.append(element("dt", name), element("dd", value));
    }
    if (facts.childElementCount) card.append(facts);
    checks.append(card);
  }
}

async function load(manual) {
  if (refresh.disabled) return;
  refresh.disabled = true;
  render(names.map(title => ({ title, status: "loading" })));
  next.textContent = "正在检查本地环境…";
  time.textContent = "";
  let timer;
  try {
    const bridge = window.AstrBotPluginView;
    if (!bridge || window.parent === window) throw new Error();
    const data = await Promise.race([
      bridge.ready().then(() => bridge.apiPost("environment/check", {refresh: manual})),
      new Promise((_, reject) => { timer = setTimeout(reject, 15000); }),
    ]);
    if (!Array.isArray(data?.items) || !data.next_step) throw new Error();
    render(data.items);
    next.textContent = data.next_step;
    time.textContent = "本地环境检查时间　" + new Date(data.checked_at).toLocaleString();
  } catch {
    render(names.map(title => ({title, status: "unknown"})));
    next.textContent = "未取得检查结果。请使用管理员账号从 AstrBot 插件详情打开此页，确认插件已启用后重试。";
  } finally {
    clearTimeout(timer);
    refresh.disabled = false;
  }
}
refresh.addEventListener("click", () => load(true));
load(false);
