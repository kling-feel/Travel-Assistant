"use strict";

const $ = (id) => document.getElementById(id);
const state = { token: null, generation: 0, controllers: new Set(), sessionId: null,
  sessionRevision: 0, sending: false, pendingTurn: null, busyAction: false, dialogAction: null };
const actionNames = { flight_book: "预订机票", flight_change: "改签机票", flight_cancel: "取消机票",
  hotel_book: "预订酒店", hotel_change: "修改酒店", hotel_cancel: "取消酒店",
  rental_book: "预订租车", rental_change: "修改租车日期", rental_cancel: "取消租车" };
const statusNames = { pending: "待确认", confirmed: "已确认", rejected: "已拒绝", expired: "已过期" };
const fieldNames = { order_id: "订单 ID", flight_id: "航班 ID", flight_no: "航班号",
  departure_airport: "出发机场", arrival_airport: "到达机场", departure_local: "当地出发",
  arrival_local: "当地到达", departure_at: "出发 UTC", arrival_at: "到达 UTC",
  status: "状态", amount: "模拟票价", product_id: "产品 ID", product_name: "车辆",
  hotel_name: "酒店", hotel_city: "城市", room_type: "房型", check_in: "入住日期",
  check_out: "退房日期", total_price: "模拟总价", airport_code: "机场",
  airport_timezone: "机场时区", pickup_local: "当地取车", dropoff_local: "当地还车",
  pickup_at: "取车 UTC", dropoff_at: "还车 UTC", daily_price: "模拟日价",
  billable_days: "计费日数", expected_version: "订单版本", new_flight_id: "新航班 ID" };

function el(tag, className, value) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (value !== undefined && value !== null) node.textContent = String(value);
  return node;
}
function appendText(parent, tag, className, value) { const child = el(tag, className, value); parent.append(child); return child; }
function empty(node, message) { node.replaceChildren(el("p", "subtle", message)); }
function notice(message, error = false) {
  const box = $("notice"); box.textContent = message; box.classList.toggle("error", error); box.hidden = !message;
}
function errorText(error) {
  if (error.message === "stale") return "";
  if (error.message === "timeout") return "请求等待超时。若是对话，请使用“重试本轮”核对原请求。";
  if (error.status === 401) return "登录已失效，请重新登录。";
  if (error.status === 403 || error.status === 404) return "当前账号无权查看此内容，或该记录已不存在。";
  if (error.status === 409) return "状态已变化或请求仍在处理中，请刷新后核对。";
  if (error.status === 422) return "输入不符合要求，请检查日期、机场和必填内容。";
  if (error.status === 503) return "模型或数据服务暂时不可用，请稍后重试。";
  return "请求未完成，请检查连接并重试。";
}
async function request(path, options = {}) {
  const generation = state.generation;
  const controller = new AbortController();
  state.controllers.add(controller);
  const timeout = setTimeout(() => controller.abort(), options.turn ? 70000 : 30000);
  try {
    const headers = { Accept: "application/json" };
    if (options.body !== undefined) headers["Content-Type"] = "application/json";
    if (state.token && !options.public) headers.Authorization = `Bearer ${state.token}`;
    const response = await fetch(path, { method: options.method || "GET", headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      signal: controller.signal, cache: "no-store", credentials: "omit" });
    if (generation !== state.generation) throw new Error("stale");
    let data = null;
    try { data = await response.json(); } catch { /* Error body may be empty. */ }
    if (!response.ok) {
      const error = new Error(`HTTP ${response.status}`); error.status = response.status;
      if (response.status === 401 && !options.public) logout("登录已失效，请重新登录。");
      throw error;
    }
    return data;
  } catch (error) {
    if (generation !== state.generation && error.status !== 401) throw new Error("stale");
    if (error.name === "AbortError") throw new Error("timeout");
    throw error;
  } finally { clearTimeout(timeout); state.controllers.delete(controller); }
}
function logout(message = "") {
  state.generation++;
  for (const controller of state.controllers) controller.abort();
  state.controllers.clear(); state.token = null; state.sessionId = null;
  state.sessionRevision++; state.pendingTurn = null; state.sending = false;
  state.dialogAction = null; state.busyAction = false;
  if ($("action-dialog").open) $("action-dialog").close();
  $("workspace").hidden = true; $("login-view").hidden = false; $("logout").hidden = true;
  $("identity").textContent = ""; $("password").value = ""; $("chat-input").value = "";
  $("retry-turn").hidden = true; $("session-list").replaceChildren();
  $("messages").replaceChildren(); $("action-list").replaceChildren();
  $("itinerary-list").replaceChildren(); $("history-list").replaceChildren();
  $("search-results").replaceChildren(); $("login-message").textContent = message;
  $("username").focus();
}
function report(error) { const message = errorText(error); if (message && error.status !== 401) notice(message, true); }
function local(value) {
  if (value === null || value === undefined || value === "") return "—";
  const text = String(value);
  const stamp = text.match(/^(\d{4}-\d\d-\d\d)T(\d\d:\d\d)(?::\d\d(?:\.\d+)?)?(Z|[+-]\d\d:\d\d)?$/);
  if (!stamp) return text;
  const zone = stamp[3] === "Z" || stamp[3] === "+00:00" ? " UTC" : stamp[3] ? ` ${stamp[3]}` : "";
  return `${stamp[1]} ${stamp[2]}${zone}`;
}
function displayStamp(value) {
  if (!value) return "—";
  const normalized = /^\d{4}-\d\d-\d\dT/.test(value) && !/(Z|[+-]\d\d:\d\d)$/.test(value)
    ? `${value}Z` : value;
  const stamp = new Date(normalized);
  if (Number.isNaN(stamp.getTime())) return local(value);
  return new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hour12: false, timeZoneName: "short" }).format(stamp);
}
function money(value) { return value === null || value === undefined ? "—" : `${value} 演示计价单位`; }
function status(value) { return statusNames[value] || value || "—"; }
function formatObject(value) {
  if (!value) return "无（新建订单）";
  if (typeof value !== "object") return String(value);
  return Object.entries(value).map(([key, item]) => `${fieldNames[key] || key}：${item === null ? "—" : typeof item === "object" ? JSON.stringify(item) : local(item)}`).join("\n");
}
function compactSnapshot(value, kind) {
  if (!value) return "新建订单";
  if (kind.startsWith("flight_"))
    return `${value.flight_no || "航班"} · ${value.departure_airport || ""} → ${value.arrival_airport || ""} · ${local(value.departure_local || value.departure_at)}`;
  if (kind.startsWith("hotel_"))
    return `${value.hotel_name || "酒店"} · ${value.room_type || "房型"} · ${value.check_in || "—"} 至 ${value.check_out || "—"}`;
  return `${value.product_name || "车辆"} · ${value.airport_code || ""} · ${local(value.pickup_local || value.pickup_at)} 至 ${local(value.dropoff_local || value.dropoff_at)}`;
}
function cardSummary(card) {
  const price = card.price || {};
  return [
    ["动作", actionNames[card.action_type] || card.action_type],
    ["状态", status(card.status)], ["动作 ID", card.id],
    ["原订单", card.target_order_id || "新订单"],
    ["调整前", compactSnapshot(card.before, card.action_type)],
    ["调整后", compactSnapshot(card.after, card.action_type)],
    ["原金额", money(price.old_amount)], ["新金额", money(price.new_amount)],
    ["需补", money(price.payable)], ["模拟应退", money(price.refundable)],
    ["取消费", money(price.cancellation_fee)],
    ["规则", card.policy ? `${card.policy.id} / v${card.policy.version}` : "—"],
    ["报价版本", card.offer_version || "—"], ["有效期（本机时间）", displayStamp(card.expires_at)],
    ["说明", card.simulation_notice || "模拟交易"],
  ];
}
function detailList(rows) {
  const dl = el("dl");
  for (const [name, value] of rows) { appendText(dl, "dt", "", name); appendText(dl, "dd", "", value); }
  return dl;
}
function showReferences(references) {
  if (!references?.length) return;
  const box = el("div", "references");
  appendText(box, "strong", "", "本轮依据与候选");
  const labels = { flight: "航班", hotel_product: "酒店候选", rental_product: "租车候选",
    order: "本人订单", policy: "虚构规则", place_source: "地点来源", action: "待确认卡" };
  for (const ref of references) {
    const id = ref.type === "action" && ref.id ? String(ref.id).slice(0, 8) : ref.id;
    const parts = [labels[ref.type] || ref.type, ref.place, ref.city, id, ref.version ? `v${ref.version}` : null]
      .filter((part) => part !== null && part !== undefined && part !== "");
    appendText(box, "span", "", parts.join(" · "));
  }
  $("messages").append(box); $("messages").scrollTop = $("messages").scrollHeight;
}
function setPanel(id) {
  for (const button of document.querySelectorAll(".tab")) button.classList.toggle("active", button.dataset.panel === id);
  for (const panel of ["conversation", "search", "itinerary", "history"]) $(panel).hidden = panel !== id;
}
function initDates(data) {
  $("demo-window").textContent = `演示基准日 ${data.as_of} · 可搜索 ${data.bookable_from} 至 ${data.bookable_until}`;
  for (const id of ["flight-date", "check-in", "pickup-date"]) {
    $(id).min = data.bookable_from; $(id).max = data.bookable_until; $(id).value = data.bookable_from;
  }
  for (const id of ["check-out", "dropoff-date"]) {
    $(id).min = data.bookable_from; $(id).max = data.hotel_checkout_until;
    const next = new Date(`${data.bookable_from}T12:00:00Z`);
    next.setUTCDate(next.getUTCDate() + 1); $(id).value = next.toISOString().slice(0, 10);
  }
}
async function loadSessions(preferred = null) {
  const sessions = await request("/api/v1/sessions");
  const box = $("session-list"); box.replaceChildren();
  if (!sessions.length) { empty(box, "还没有会话"); selectSession(null); return; }
  for (const item of sessions) {
    const button = el("button", "session-item"); button.type = "button";
    button.append(el("span", "", `会话 ${String(item.id).slice(0, 8)}`), el("small", "", displayStamp(item.updated_at)));
    button.addEventListener("click", () => selectSession(item.id)); box.append(button);
  }
  await selectSession(preferred && sessions.some((item) => item.id === preferred) ? preferred : sessions[0].id);
}
async function selectSession(id) {
  state.sessionId = id; state.sessionRevision++;
  const revision = state.sessionRevision;
  state.pendingTurn = null; $("retry-turn").hidden = true;
  for (const button of $("session-list").querySelectorAll(".session-item"))
    button.classList.toggle("active", button.textContent.includes(String(id || "").slice(0, 8)) && !!id);
  $("session-label").textContent = id ? `会话 ${id.slice(0, 8)}` : "选择或新建会话";
  if (!id) { empty($("messages"), "新建会话后开始对话。"); empty($("action-list"), "暂无动作"); return; }
  try { await Promise.all([refreshMessages(id, revision), refreshActions(id, revision)]); }
  catch (error) { report(error); }
}
async function refreshMessages(id = state.sessionId, revision = state.sessionRevision) {
  if (!id) return;
  const history = await request(`/api/v1/sessions/${encodeURIComponent(id)}`);
  if (revision !== state.sessionRevision) return;
  const box = $("messages"); box.replaceChildren();
  if (!history.messages.length) { empty(box, "可以询问政策、推荐或提出预订需求。"); return; }
  for (const message of history.messages) {
    const bubble = el("div", `bubble ${message.role === "user" ? "user" : "assistant"}`);
    appendText(bubble, "small", "", message.role === "user" ? "我" : "旅行助手");
    appendText(bubble, "span", "", message.content); box.append(bubble);
  }
  box.scrollTop = box.scrollHeight;
}
async function refreshActions(id = state.sessionId, revision = state.sessionRevision) {
  if (!id) return;
  const cards = await request(`/api/v1/sessions/${encodeURIComponent(id)}/actions`);
  if (revision !== state.sessionRevision) return;
  const box = $("action-list"); box.replaceChildren();
  if (!cards.length) { empty(box, "对话产生的待确认卡会显示在这里。"); return; }
  for (const card of cards) {
    const item = el("article", "action-card");
    appendText(item, "h3", "", actionNames[card.action_type] || card.action_type);
    appendText(item, "span", `status ${card.status}`, status(card.status));
    appendText(item, "p", "card-target", `动作 ${String(card.id).slice(0, 8)} · ${card.target_order_id || "新订单"}`);
    const change = el("div", "change-preview");
    appendText(change, "small", "", "变更前"); appendText(change, "div", "", compactSnapshot(card.before, card.action_type));
    appendText(change, "small", "", "变更后"); appendText(change, "div", "", compactSnapshot(card.after, card.action_type));
    item.append(change);
    const amount = el("div", "amount-preview");
    appendText(amount, "span", "", `需补 ${money(card.price?.payable)}`);
    appendText(amount, "span", "", `模拟应退 ${money(card.price?.refundable)}`);
    item.append(amount);
    appendText(item, "p", "card-meta", `规则 ${card.policy?.id || "—"} · v${card.policy?.version || "—"}`);
    appendText(item, "p", "card-meta", `有效期 ${displayStamp(card.expires_at)}`);
    const controls = el("div", "card-buttons");
    const details = el("button", "small-button", "查看详情"); details.type = "button";
    details.addEventListener("click", () => openAction(id, card.id, "view")); controls.append(details);
    if (card.status === "pending") {
      for (const [mode, label] of [["confirm", "确认"], ["reject", "拒绝"]]) {
        const button = el("button", mode === "confirm" ? "primary compact" : "small-button", label);
        button.type = "button"; button.addEventListener("click", () => openAction(id, card.id, mode)); controls.append(button);
      }
    }
    item.append(controls); box.append(item);
  }
}
async function openAction(sessionId, actionId, mode) {
  if (state.busyAction) return;
  try {
    const card = await request(`/api/v1/sessions/${encodeURIComponent(sessionId)}/actions/${encodeURIComponent(actionId)}`);
    if (state.sessionId !== sessionId) return;
    if (mode !== "view" && card.status !== "pending") {
      notice(`这张卡片目前是“${status(card.status)}”，请重新查看。`, true);
      await refreshActions(); return;
    }
    state.dialogAction = { sessionId, actionId, mode };
    $("dialog-title").textContent = `${mode === "confirm" ? "确认" : mode === "reject" ? "拒绝" : "查看"} · ${actionNames[card.action_type] || card.action_type}`;
    const details = el("details", "field-details");
    appendText(details, "summary", "", "查看完整订单字段");
    details.append(detailList([["调整前", formatObject(card.before)], ["调整后", formatObject(card.after)]]));
    $("dialog-details").replaceChildren(detailList(cardSummary(card)), details);
    $("dialog-warning").textContent = mode === "confirm"
      ? "这是一笔模拟交易。确认后才会修改本人订单，不发生真实付款或退款。"
      : mode === "reject" ? "拒绝后这张卡失效，本人订单不变。" : "这是一笔模拟交易记录。";
    $("dialog-submit").hidden = mode === "view";
    $("dialog-submit").textContent = mode === "reject" ? "拒绝这张卡" : "确认这张卡";
    $("action-dialog").showModal();
  } catch (error) { report(error); }
}
async function submitAction() {
  const item = state.dialogAction;
  if (!item || state.busyAction || item.mode === "view") return;
  state.busyAction = true; $("dialog-submit").disabled = true;
  try {
    const result = await request(`/api/v1/sessions/${encodeURIComponent(item.sessionId)}/actions/${encodeURIComponent(item.actionId)}/${item.mode}`,
      { method: "POST" });
    $("action-dialog").close();
    notice(result.status === "confirmed" ? "已确认这张模拟交易卡，正在刷新行程。" : "已拒绝这张卡，订单未改变。");
    await Promise.all([refreshActions(), refreshItinerary(), refreshHistory()]);
  } catch (error) {
    report(error);
    if (error.status === 409 || error.status === 404) {
      $("action-dialog").close(); try { await refreshActions(); } catch { /* Main error already shown. */ }
    }
  } finally { state.busyAction = false; $("dialog-submit").disabled = false; state.dialogAction = null; }
}
async function sendTurn(retry = false) {
  if (state.sending) return;
  if (!state.sessionId) { notice("请先新建或选择会话。", true); return; }
  const content = retry ? state.pendingTurn?.content : $("chat-input").value.trim();
  if (!content) return;
  const sessionId = state.sessionId;
  const turn = retry && state.pendingTurn?.sessionId === sessionId ? state.pendingTurn :
    { sessionId, content, requestKey: crypto.randomUUID() };
  state.pendingTurn = turn; state.sending = true; $("send-button").disabled = true;
  $("retry-turn").hidden = true; notice("正在处理这一轮对话…");
  try {
    const outcome = await request(`/api/v1/sessions/${encodeURIComponent(sessionId)}/turns`,
      { method: "POST", body: { content: turn.content, request_key: turn.requestKey }, turn: true });
    if (state.sessionId !== sessionId) return;
    if (!retry) $("chat-input").value = "";
    state.pendingTurn = null; notice(outcome.actions?.length ? `已生成 ${outcome.actions.length} 张模拟待确认卡，请逐张核对。` : "答复已保存。");
    await Promise.all([refreshMessages(), refreshActions(), refreshHistory()]);
    await loadSessions(sessionId);
    showReferences(outcome.references);
  } catch (error) {
    if (state.sessionId === sessionId && error.message !== "stale") {
      report(error);
      if (error.message === "timeout" || error.status === 409) $("retry-turn").hidden = false;
      else state.pendingTurn = null;
      try { await Promise.all([refreshMessages(), refreshActions()]); } catch { /* Keep the original error. */ }
    }
  } finally { state.sending = false; $("send-button").disabled = false; }
}
function record(title, lines, meta = "") {
  const item = el("article", "record"); appendText(item, "h3", "", title);
  for (const line of lines) appendText(item, "p", "", line);
  if (meta) appendText(item, "p", "meta", meta);
  return item;
}
async function refreshItinerary() {
  const data = await request("/api/v1/me/itinerary");
  const box = $("itinerary-list"); box.replaceChildren();
  for (const item of data.flight_orders || []) box.append(record(`${item.flight_no} · ${item.departure_airport} → ${item.arrival_airport}`,
    [`${local(item.departure_at)} 出发`, `状态 ${status(item.status)} · 模拟金额 ${money(item.amount)}`], `订单 ${item.id} · v${item.version}`));
  for (const item of data.hotel_orders || []) box.append(record(item.name,
    [`${item.city} · ${item.room_type}`, `${item.check_in} 入住 → ${item.check_out} 退房`, `状态 ${status(item.status)} · 模拟总价 ${money(item.total_price)}`], `订单 ${item.id} · v${item.version}`));
  for (const item of data.rental_orders || []) box.append(record(item.name,
    [`${item.airport_code} · 当地取车 ${local(item.pickup_local)}`, `当地还车 ${local(item.dropoff_local)}`, `状态 ${status(item.status)} · 模拟总价 ${money(item.total_price)}`], `订单 ${item.id} · v${item.version}`));
  if (!box.children.length) empty(box, "当前账号还没有行程订单。");
}
async function refreshHistory() {
  const data = await request("/api/v1/me/action-history");
  const box = $("history-list"); box.replaceChildren();
  for (const item of data) box.append(record(item.event_type || "动作记录",
    [formatObject(item.summary)], `${displayStamp(item.created_at)} · 动作 ${item.action_id || "—"}`));
  if (!data.length) empty(box, "还没有操作记录。");
}
function formQuery(form) {
  const params = new URLSearchParams();
  for (const [key, value] of new FormData(form)) if (String(value).trim()) params.set(key, String(value).trim());
  params.set("limit", "12"); return params;
}
async function search(kind, form) {
  const button = form.querySelector("button[type=submit]"); button.disabled = true;
  const box = $("search-results"); empty(box, "正在查询模拟候选…");
  try {
    const data = await request(`/api/v1/${kind}?${formQuery(form)}`);
    box.replaceChildren();
    for (const item of data) {
      if (kind === "flights") box.append(record(`${item.flight_no} · ${item.departure_airport} → ${item.arrival_airport}`,
        [`出发 ${local(item.departure_at)}`, `到达 ${local(item.arrival_at)}`, `模拟报价 ${money(item.demo_fare_amount)}`], `候选 ID ${item.id} · 报价版本 ${item.offer_version}`));
      else if (kind === "hotels") box.append(record(`${item.name} · ${item.room_type}`,
        [`${item.city} · 每晚 ${money(item.nightly_price)}`, item.distance_km === null ? "" : `距机场模拟直线 ${item.distance_km} 公里`], `产品 ID ${item.product_id}`));
      else box.append(record(`${item.name} · ${item.airport_code}`,
        [`每模拟日 ${money(item.daily_price)}`, item.distance_km === null ? "" : `距机场模拟直线 ${item.distance_km} 公里`], `产品 ID ${item.id}`));
    }
    if (!data.length) empty(box, "这个条件下没有可用候选，请检查演示日期、城市和机场代码。");
  } catch (error) { empty(box, errorText(error)); report(error); }
  finally { button.disabled = false; }
}

$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault(); const button = $("login-button"); button.disabled = true;
  $("login-message").textContent = "正在登录…";
  try {
    const credentials = { username: $("username").value.trim(), password: $("password").value };
    const login = await request("/api/v1/login", { method: "POST", body: credentials, public: true });
    state.token = login.access_token; $("password").value = "";
    const [profile, demo] = await Promise.all([request("/api/v1/me"), request("/api/v1/demo-state")]);
    $("identity").textContent = profile.display_name || profile.username;
    initDates(demo); $("login-view").hidden = true; $("workspace").hidden = false;
    $("logout").hidden = false; $("login-message").textContent = ""; notice("");
    await Promise.all([loadSessions(), refreshItinerary(), refreshHistory()]);
  } catch (error) { if (state.token) logout(); $("login-message").textContent = errorText(error); }
  finally { button.disabled = false; }
});
$("logout").addEventListener("click", () => logout());
$("new-session").addEventListener("click", async () => {
  $("new-session").disabled = true;
  try { const created = await request("/api/v1/sessions", { method: "POST", body: {} }); await loadSessions(created.id); setPanel("conversation"); notice("已新建会话。"); }
  catch (error) { report(error); } finally { $("new-session").disabled = false; }
});
$("chat-form").addEventListener("submit", (event) => { event.preventDefault(); sendTurn(); });
$("retry-turn").addEventListener("click", () => sendTurn(true));
$("refresh-actions").addEventListener("click", async () => { try { await refreshActions(); } catch (error) { report(error); } });
$("refresh-itinerary").addEventListener("click", async () => { try { await refreshItinerary(); } catch (error) { report(error); } });
$("refresh-history").addEventListener("click", async () => { try { await refreshHistory(); } catch (error) { report(error); } });
$("dialog-close").addEventListener("click", () => $("action-dialog").close());
$("dialog-submit").addEventListener("click", submitAction);
$("action-dialog").addEventListener("close", () => { if (!state.busyAction) state.dialogAction = null; });
for (const button of document.querySelectorAll(".tab")) button.addEventListener("click", () => setPanel(button.dataset.panel));
for (const [id, kind] of [["flight-form", "flights"], ["hotel-form", "hotels"], ["rental-form", "rentals"]])
  $(id).addEventListener("submit", (event) => { event.preventDefault(); search(kind, $(id)); });
