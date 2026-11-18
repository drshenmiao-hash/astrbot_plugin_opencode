const byId = (id) => document.getElementById(id);
const number = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 1 });
const dimensions = { model: "模型", session: "匿名会话", code: "结果" };
const codes = {
  ok: "成功",
  cancelled: "请求取消",
  closed: "插件停止",
  not_logged_in: "认证失败",
  quota_exhausted: "额度不足",
  rate_limited: "触发限流",
  queue_timeout: "排队超时",
  startup_timeout: "启动超时",
  timeout: "响应超时",
  connection_stalled: "连接停滞",
  ping_timeout: "心跳超时",
  upstream_retryable: "上游短暂故障",
  cli_failed: "CLI 请求失败",
  model_unavailable: "模型不可用",
  tool_blocked: "工具已拦截",
  sandbox_unavailable: "隔离环境不可用",
  cleanup_timeout: "运行端清理超时",
  image_unsupported: "模型不支持图片",
  image_capability_unverified: "图片能力待确认",
  image_source: "图片来源不可读",
  image_invalid: "图片格式无效",
  image_unavailable: "模型无法读取图片",
  image_limit: "图片超限",
};
const count = (value) => number.format(value ?? 0);
const tokenCount = (value) => value == null ? "—" : count(value);
let current;

function write(id, value) {
  byId(id).textContent = value;
}

function bytes(value) {
  if (value < 1024) return `${count(value)} B`;
  if (value < 1024 ** 2) return `${count(value / 1024)} KiB`;
  return `${count(value / 1024 ** 2)} MiB`;
}

function resultLabel(code) {
  return codes[code] || code || "—";
}

function alert(message) {
  write("error", message);
  byId("error").hidden = !message;
}

function table(id, rows, columns) {
  const body = byId(id);
  body.replaceChildren();
  if (!rows.length) {
    const cell = body.insertRow().insertCell();
    cell.colSpan = columns;
    cell.textContent = "该时段暂无请求";
    return;
  }
  for (const values of rows) {
    const row = body.insertRow();
    for (const value of values) row.insertCell().textContent = value;
  }
}

function renderGroups() {
  if (!current) return;
  const dimension = byId("dimension").value;
  write("dimension-title", dimensions[dimension]);
  const rows = current.groups[dimension].map((row) => {
    let label = row.label;
    if (dimension === "code") label = resultLabel(label);
    if (!label) label = dimension === "session" ? "未识别会话" : "未记录 ACP 发送";
    return [label, count(row.requests), count(row.failed), count(row.reused), tokenCount(row.token_total)];
  });
  table("groups", rows, 5);
}

function renderTrend(days) {
  const max = Math.max(1, ...days.map((day) => day.requests));
  const bars = document.createDocumentFragment();
  for (const day of days) {
    const bar = document.createElement("div");
    bar.className = "bar";
    bar.title = `${day.label} · ${count(day.requests)} 次 · ${count(day.failed)} 次失败`;
    for (const [quantity, style] of [[day.requests - day.failed, ""], [day.failed, "fail"]]) {
      const segment = document.createElement("span");
      segment.className = style;
      segment.style.height = `${quantity / max * 100}%`;
      bar.append(segment);
    }
    bars.append(bar);
  }
  byId("chart").replaceChildren(bars);
  table("daily", days.map((day) => [day.label, count(day.requests), count(day.failed), tokenCount(day.token_total)]), 4);
}

function render(data) {
  current = data;
  const stats = data.totals;
  const live = data.runtime;
  write("model", data.model || "未配置");
  const runtime = live.process_alive ? "CLI 运行中" : data.validated ? "等待请求" : "模型待校验";
  write("runtime-state", data.startup_error ? "启动失败" : runtime);
  byId("startup-error").hidden = !data.startup_error;
  write("startup-error", data.startup_error?.message || "");
  write("active", `${count(live.active)} / ${count(data.concurrency_limit)}`);
  write("queued", count(live.queue_depth));
  write("sessions", count(live.cached_sessions));
  write("last-failure", resultLabel(live.last_failure));
  write("token-attempts", stats.token_attempts ? `已记录 ${count(stats.token_attempts)} 次` : "该时段暂无 Token 记录");
  write("token-total", tokenCount(stats.token_total));
  write("token-input", tokenCount(stats.token_input));
  write("token-output", tokenCount(stats.token_output));
  write("token-cached", tokenCount(stats.token_cached));
  write("requests", count(stats.requests));
  write("failed", `失败 ${count(stats.failed)} 次`);
  write("success", stats.requests ? `${count(stats.success / stats.requests * 100)}%` : "—");
  write("success-count", `成功 ${count(stats.success)} 次`);
  write("duration", stats.requests ? `${count(stats.duration_ms / stats.requests)} ms` : "—");
  write("queue-time", stats.requests ? `平均排队 ${count(stats.queue_ms / stats.requests)} ms` : "平均排队 —");
  write("bytes", `${bytes(stats.sent_input_bytes)} / ${bytes(stats.output_bytes)}`);
  write("reuse", `会话复用 ${count(stats.reused)} 次`);
  write("input-bytes", bytes(stats.input_bytes));
  write("wire-bytes", bytes(stats.wire_bytes));
  write("process-reused", count(stats.process_reused));
  write("interval", `${data.start} 至 ${data.end}`);
  write("axis-start", data.start.slice(5));
  write("axis-end", data.end.slice(5));
  renderTrend(data.trend);
  renderGroups();
  byId("dashboard").hidden = false;
  if (data.write_failures) {
    alert(`本次运行有 ${count(data.write_failures)} 次统计写入失败，数据可能不完整。`);
  }
}

async function refresh() {
  const button = byId("refresh");
  const range = byId("range");
  button.disabled = range.disabled = true;
  alert("");
  byId("status").hidden = false;
  write("status", "正在读取本地统计…");
  try {
    const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
    if (!bridge || window.parent === window) {
      throw new Error("请从 AstrBot 插件管理页打开此页面。");
    }
    await bridge.ready();
    const data = await bridge.apiPost("usage/dashboard", { days: Number(range.value) });
    if (!data?.totals) throw new Error("无法读取统计，请刷新重试。");
    render(data);
    const total = data.totals.requests;
    write("status", total
      ? `已更新 · ${new Intl.DateTimeFormat("zh-CN", {timeStyle: "medium"}).format(new Date())}`
      : "该时段暂无用量记录。OpenCode 处理请求后可刷新查看。");
  } catch (error) {
    byId("dashboard").hidden = true;
    byId("status").hidden = true;
    alert(error?.message || "读取失败，请刷新重试。");
  } finally {
    button.disabled = range.disabled = false;
  }
}

byId("refresh").addEventListener("click", refresh);
byId("range").addEventListener("change", refresh);
byId("dimension").addEventListener("change", renderGroups);
refresh();
