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

function percent(part, total) {
  if (!total) return "—";
  const value = part / total * 100;
  if (part > 0 && value < 0.1) return "<0.1%";
  if (part < total && value > 99.9) return ">99.9%";
  return count(value) + "%";
}

function resultLabel(code) {
  return codes[code] || code || "—";
}

function alert(message) {
  write("error", message);
  byId("error").hidden = !message;
}

function table(id, rows, columns, emptyMessage = "该时段暂无请求") {
  const body = byId(id);
  body.replaceChildren();
  if (!rows.length) {
    const cell = body.insertRow().insertCell();
    cell.colSpan = columns;
    cell.textContent = emptyMessage;
    return;
  }
  for (const values of rows) {
    const row = body.insertRow();
    for (const value of values) row.insertCell().textContent = value;
  }
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function renderGroups() {
  if (!current) return;
  const dimension = byId("dimension").value;
  const metric = byId("rank-metric").querySelector("input:checked").value;
  const tokens = metric === "tokens";
  const { totals } = current;
  const groups = (tokens ? current.token_groups : current.groups)[dimension];
  const total = tokens ? totals.token_total : totals.requests;
  const measure = tokens ? "token_total" : "requests";
  const unit = tokens ? " Token" : " 次";
  const headers = tokens
    ? [dimensions[dimension], "总 Token", "输入", "输出", "缓存命中", "已计数 / 请求"]
    : [dimensions[dimension], "请求", "失败", "会话复用", "Token"];
  byId("group-columns").replaceChildren(...headers.map(label => {
    const th = document.createElement("th");
    th.scope = "col";
    th.textContent = label;
    return th;
  }));
  const basis = tokens
    ? `占时段内已记录 Token 的比例 · ${count(totals.token_attempts)} / ${count(totals.requests)} 次请求有计数`
    : "占时段内全部请求的比例";
  write("rank-basis", "前 5 项 · " + basis);
  const empty = tokens && totals.requests ? "该时段的请求没有 Token 计数" : "该时段暂无请求";
  const ranking = byId("ranking");
  ranking.dataset.metric = metric;
  const ranks = document.createDocumentFragment();
  const rows = groups.map((row, index) => {
    let label = dimension === "code" ? resultLabel(row.label) : row.label;
    if (!label) label = dimension === "session" ? "未识别会话" : "未记录 ACP 发送";
    if (index < 5) {
      const value = row[measure];
      const share = total ? value / total * 100 : 0;
      const item = element("li", "rank");
      const name = element("span", "rank-name", label);
      name.title = label;
      const track = element("div", "rank-track");
      const fill = element("div", "rank-fill");
      fill.style.width = share + "%";
      track.append(fill);
      item.append(name, element("span", "rank-count", count(value) + unit),
        track, element("span", "rank-share", percent(value, total)));
      ranks.append(item);
    }
    if (tokens) {
      return [label, tokenCount(row.token_total), tokenCount(row.token_input), tokenCount(row.token_output),
        tokenCount(row.token_cached), count(row.token_attempts) + " / " + count(row.requests)];
    }
    return [label, count(row.requests), count(row.failed), count(row.reused), tokenCount(row.token_total)];
  });
  ranking.replaceChildren(ranks);
  write("groups-empty", empty);
  byId("groups-empty").hidden = groups.length > 0;
  table("groups", rows, headers.length, empty);
}

const trends = {
  requests: {
    series: [["success", "成功"], ["fail", "失败"]],
    values: day => [day.requests - day.failed, day.failed],
    detail: day => `成功 ${count(day.requests - day.failed)} 次 · 失败 ${count(day.failed)} 次`,
  },
  tokens: {
    series: [["tokens-bar", "已记录 Token"]],
    values: day => [day.token_total],
    detail: day => day.token_total == null ? "未记录 Token"
      : `${count(day.token_total)} Token · 已记录 ${count(day.token_attempts)} / ${count(day.requests)} 次请求`,
  },
  duration: {
    series: [["duration-bar", "平均耗时"]],
    values: day => [day.duration_avg_ms],
    detail: day => `平均耗时 ${count(day.duration_avg_ms)} ms · 平均排队 ${count(day.queue_avg_ms)} ms`,
  },
};

function selectDay() {
  const picker = byId("day-picker");
  const day = current.trend[picker.valueAsNumber];
  const text = day.requests ? trends[byId("metric").value].detail(day) : "当日无请求";
  const chart = byId("chart");
  chart.querySelector(".selected")?.classList.remove("selected");
  chart.children[picker.valueAsNumber].classList.add("selected");
  write("selected-date", day.label);
  byId("selected-date").dateTime = day.label;
  write("selected-values", text);
  picker.setAttribute("aria-valuetext", day.label + " · " + text);
}

function renderTrend() {
  if (!current) return;
  const days = current.trend;
  const metric = byId("metric").value;
  const trend = trends[metric];
  const hasData = metric === "tokens" ? current.totals.token_attempts > 0 : current.totals.requests > 0;
  byId("chart-empty").hidden = hasData;
  byId("trend-content").hidden = !hasData;
  if (!hasData) {
    write("chart-empty", current.totals.requests ? "该时段的请求没有 Token 计数" : "该时段暂无请求");
    return;
  }
  const values = days.map(trend.values);
  const peak = Math.max(1, ...values.map(parts => parts.reduce((total, part) => total + (part ?? 0), 0)));
  const magnitude = 10 ** Math.floor(Math.log10(peak));
  const ceiling = Math.ceil(peak / magnitude) * magnitude;
  write("scale-top", count(ceiling));
  const bars = document.createDocumentFragment();
  let hasMissing = false;
  days.forEach((day, index) => {
    const bar = element("div", "bar");
    const parts = values[index];
    if (day.requests && parts[0] == null) {
      hasMissing = true;
      bar.classList.add("missing");
    } else if (day.requests && parts.every(n => n === 0)) {
      bar.classList.add("recorded-zero", trend.series[0][0]);
    } else {
      parts.forEach((value, i) => {
        if (!value) return;
        const segment = element("span", trend.series[i][0]);
        segment.style.height = (value / ceiling * 100) + "%";
        bar.append(segment);
      });
    }
    bars.append(bar);
  });
  byId("chart").replaceChildren(bars);
  const legend = hasMissing ? [...trend.series, ["missing-key", "未记录"]] : trend.series;
  byId("legend").replaceChildren(...legend.map(([style, label]) => {
    const key = element("span", "legend-item");
    key.append(element("i", "swatch " + style), label);
    return key;
  }));
  const picker = byId("day-picker");
  picker.max = days.length - 1;
  picker.style.setProperty("--day-width", 100 / days.length + "%");
  const previous = days.findIndex(day => day.label === byId("selected-date").dateTime);
  picker.value = previous >= 0 ? previous : days.findLastIndex(day => day.requests);
  selectDay();
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
  write("success", percent(stats.success, stats.requests));
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
  table("daily", data.trend.map(day => [
    day.label, count(day.requests), count(day.failed), tokenCount(day.token_total),
    day.duration_avg_ms == null ? "—" : count(day.duration_avg_ms) + " ms",
  ]), 5);
  renderTrend();
  renderGroups();
  byId("dashboard").hidden = false;
  if (data.write_failures) {
    alert(`本次运行有 ${count(data.write_failures)} 次统计写入失败，数据可能不完整。`);
  }
}

async function waitForBridge(promise) {
  let timer;
  try {
    const timeout = new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error("管理面板响应超时，请刷新重试。")), 15000);
    });
    return await Promise.race([promise, timeout]);
  } finally {
    clearTimeout(timer);
  }
}

async function refresh() {
  const button = byId("refresh");
  const range = byId("range");
  const days = Number(range.value);
  button.disabled = range.disabled = true;
  if (current?.trend?.length !== days) byId("dashboard").hidden = true;
  alert("");
  byId("status").hidden = false;
  write("status", "正在读取本地统计…");
  try {
    const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
    if (!bridge || window.parent === window) {
      throw new Error("请从 AstrBot 插件管理页打开此页面。");
    }
    await waitForBridge(bridge.ready());
    const data = await waitForBridge(bridge.apiPost("usage/dashboard", { days }));
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
byId("metric").addEventListener("change", renderTrend);
byId("day-picker").addEventListener("input", selectDay);
byId("rank-metric").addEventListener("change", renderGroups);
byId("dimension").addEventListener("change", renderGroups);
refresh();
