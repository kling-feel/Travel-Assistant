"use strict";

const $ = (id) => document.getElementById(id);
const state = { token: null, generation: 0, controllers: new Set(), sessionId: null,
  sessionRevision: 0, sending: false, pendingTurn: null, busyAction: false, dialogAction: null,
  locations: [], airports: new Map(), cities: new Map(), airportInputs: new Map(), cityInputs: new Map() };
const actionNames = { flight_book: "预订机票", flight_change: "改签机票", flight_cancel: "取消机票",
  hotel_book: "预订酒店", hotel_change: "修改酒店", hotel_cancel: "取消酒店",
  rental_book: "预订租车", rental_change: "修改租车日期", rental_cancel: "取消租车" };
const statusNames = { pending: "待确认", confirmed: "已确认", rejected: "已拒绝",
  expired: "已过期", cancelled: "已取消", canceled: "已取消" };
const fieldNames = { order_id: "订单 ID", flight_id: "航班 ID", flight_no: "航班号",
  action_type: "操作", reason: "原因",
  departure_airport: "出发机场", arrival_airport: "到达机场", departure_local: "当地出发",
  arrival_local: "当地到达", departure_at: "出发时间", arrival_at: "到达时间",
  status: "状态", amount: "票价", product_id: "产品 ID", product_name: "车辆",
  hotel_name: "酒店", hotel_city: "城市", room_type: "房型", check_in: "入住日期",
  check_out: "退房日期", total_price: "总价", airport_code: "机场",
  airport_timezone: "机场时区", pickup_local: "当地取车", dropoff_local: "当地还车",
  pickup_at: "取车时间", dropoff_at: "还车时间", daily_price: "日价",
  billable_days: "计费日数", expected_version: "订单版本", new_flight_id: "新航班 ID",
  price: "报价", difference: "差价", payable: "需补差价", refundable: "可退金额",
  cancellation_fee: "取消费", nearby_airport: "附近机场" };

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
  if (error.name === "InputError") return error.message;
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
  state.locations = []; state.airports.clear(); state.cities.clear();
  state.airportInputs.clear(); state.cityInputs.clear();
  $("airport-options").replaceChildren(); $("city-options").replaceChildren();
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
function localClock(value) {
  if (!value) return "—";
  return `${local(value)}（当地）`;
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
function money(value) {
  if (value === null || value === undefined || value === "") return "—";
  const number = Number(value);
  return Number.isFinite(number) ? `¥${new Intl.NumberFormat("zh-CN").format(number)}` : "—";
}
function status(value) { return statusNames[value] || value || "—"; }
function airportName(code) { return state.airports.get(String(code || "").toUpperCase())?.name_zh || code || "—"; }
function airportCity(code) { return state.airports.get(String(code || "").toUpperCase())?.city_zh || airportName(code); }
function cityName(city) { return state.cities.get(city) || city || "—"; }
function airportDetail(code) { return `${airportName(code)}（${code}）`; }
function productName(value) {
  if (!value) return "—";
  const hotel = String(value).match(/^虚构·(.+)机场周边酒店 ([123])$/);
  if (hotel) return `${cityName(hotel[1])}机场周边酒店 · ${["", "悦居", "悠享", "臻选"][Number(hotel[2])]}`;
  const rental = String(value).match(/^虚构·([A-Z]{3})机场租车 ([12])$/);
  if (rental) return `${airportName(rental[1])}租车 · ${rental[2] === "1" ? "经济型" : "舒适型"}`;
  return value;
}
function roomName(value) { return ["演示标准间", "演示双床间", "演示家庭间"].includes(value) ? value.slice(2) : value; }
function localAtAirport(value, code) {
  if (!value) return "—";
  const zone = state.airports.get(code)?.timezone;
  if (!zone) return local(value);
  const text = String(value);
  const stamp = /^\d{4}-\d\d-\d\dT/.test(text) && !/(Z|[+-]\d\d:\d\d)$/.test(text) ? `${text}Z` : text;
  const date = new Date(stamp);
  if (Number.isNaN(date.getTime())) return local(value);
  return `${new Intl.DateTimeFormat("zh-CN", { timeZone: zone, year: "numeric", month: "2-digit",
    day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(date)}（当地）`;
}
function fieldValue(key, item, object) {
  if (item === null || item === undefined) return "—";
  if (["departure_airport", "arrival_airport", "airport_code", "nearby_airport"].includes(key)) return airportDetail(item);
  if (["city", "hotel_city"].includes(key)) return cityName(item);
  if (["hotel_name", "product_name", "name"].includes(key)) return productName(item);
  if (key === "room_type") return roomName(item);
  if (["amount", "total_price", "daily_price", "nightly_price", "old_amount", "new_amount",
    "payable", "refundable", "cancellation_fee", "difference"].includes(key)) return money(item);
  if (key === "status") return status(item);
  if (key === "action_type") return actionNames[item] || item;
  if (key === "departure_at") return localAtAirport(item, object.departure_airport);
  if (key === "arrival_at") return localAtAirport(item, object.arrival_airport);
  if (typeof item === "object") return Object.entries(item).map(([name, value]) => `${fieldNames[name] || name}：${fieldValue(name, value, item)}`).join("；");
  return local(item);
}
function formatObject(value) {
  if (!value) return "无（新建订单）";
  if (typeof value !== "object") return String(value);
  return Object.entries(value).map(([key, item]) => `${fieldNames[key] || key}：${fieldValue(key, item, value)}`).join("\n");
}
function compactSnapshot(value, kind) {
  if (!value) return "新建订单";
  if (kind.startsWith("flight_"))
    return `${airportName(value.departure_airport)} → ${airportName(value.arrival_airport)} · ${value.departure_local ? localClock(value.departure_local) : localAtAirport(value.departure_at, value.departure_airport)}`;
  if (kind.startsWith("hotel_")) {
    const start = Date.parse(`${value.check_in}T00:00:00Z`);
    const end = Date.parse(`${value.check_out}T00:00:00Z`);
    const nights = Number.isFinite(start) && Number.isFinite(end) && end > start
      ? ` · ${(end - start) / 86400000} 晚` : "";
    return `${productName(value.hotel_name || "酒店")} · ${roomName(value.room_type || "房型")} · ${value.check_in || "—"} 至 ${value.check_out || "—"}${nights}`;
  }
  return `${productName(value.product_name || "车辆")} · ${localClock(value.pickup_local || value.pickup_at)} 至 ${localClock(value.dropoff_local || value.dropoff_at)}${value.billable_days ? ` · 计费 ${value.billable_days} 日` : ""}`;
}
function cardSummary(card) {
  const price = card.price || {};
  const payableLabel = card.action_type.endsWith("_book") ? "参考总价" : "需补差价";
  return [
    ["动作", actionNames[card.action_type] || card.action_type],
    ["状态", status(card.status)], ["动作 ID", card.id],
    ["原订单", card.target_order_id || "新订单"],
    ["调整前", compactSnapshot(card.before, card.action_type)],
    ["调整后", compactSnapshot(card.after, card.action_type)],
    ["原金额", money(price.old_amount)], ["新金额", money(price.new_amount)],
    ...(card.action_type.startsWith("rental_") && card.after?.daily_price
      ? [["每日单价", money(card.after.daily_price)]] : []),
    [payableLabel, money(price.payable)], ["可退金额", money(price.refundable)],
    ["取消费", money(price.cancellation_fee)],
    ["规则", card.policy ? `${card.policy.id} / v${card.policy.version}` : "—"],
    ["报价版本", card.offer_version || "—"], ["有效期（本机时间）", displayStamp(card.expires_at)],
    ...(card.cancellation_notice ? [["取消条件", card.cancellation_notice]] : []),
    ...(card.change_notice ? [["改期条件", card.change_notice]] : []),
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
    const place = ref.display_name && ref.place && ref.display_name !== ref.place
      ? `${ref.display_name}（${ref.place}）` : (ref.display_name || ref.place);
    const parts = [labels[ref.type] || ref.type, place, ref.city ? cityName(ref.city) : null,
      id, ref.version ? `v${ref.version}` : null]
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
  $("demo-window").textContent = `可查询日期 ${data.bookable_from} 至 ${data.bookable_until}`;
  for (const id of ["flight-date", "check-in", "pickup-date"]) {
    $(id).min = data.bookable_from; $(id).max = data.bookable_until; $(id).value = data.bookable_from;
  }
  for (const id of ["check-out", "dropoff-date"]) {
    $(id).min = data.bookable_from; $(id).max = data.hotel_checkout_until;
    const next = new Date(`${data.bookable_from}T12:00:00Z`);
    next.setUTCDate(next.getUTCDate() + 1); $(id).value = next.toISOString().slice(0, 10);
  }
}
function loadLocationOptions(rows) {
  state.locations = rows;
  state.airports.clear(); state.cities.clear(); state.airportInputs.clear(); state.cityInputs.clear();
  const airportList = $("airport-options"), cityList = $("city-options");
  airportList.replaceChildren(); cityList.replaceChildren();
  const counts = new Map();
  for (const row of rows) counts.set(row.city, (counts.get(row.city) || 0) + 1);
  for (const row of rows) {
    state.airports.set(row.code, row);
    state.cities.set(row.city, row.city_zh);
    const label = `${row.name_zh} · ${row.code}`;
    state.airportInputs.set(label, row.code);
    airportList.append(el("option", "", label));
    if (counts.get(row.city) === 1) state.airportInputs.set(row.city_zh, row.code);
  }
  for (const [city, chinese] of [...state.cities].sort((a, b) => a[1].localeCompare(b[1], "zh-CN"))) {
    state.cityInputs.set(chinese, city);
    cityList.append(el("option", "", chinese));
  }
  if (state.airports.has("HAM") && state.airports.has("BSL")) {
    $("flight-from").value = `${airportName("HAM")} · HAM`;
    $("flight-to").value = `${airportName("BSL")} · BSL`;
    $("hotel-city").value = cityName("Basel");
    $("hotel-airport").value = `${airportName("BSL")} · BSL`;
    $("rental-airport").value = `${airportName("BSL")} · BSL`;
  }
}
function queryValue(key, value) {
  if (["departure", "arrival", "airport"].includes(key)) {
    const code = state.airportInputs.get(value) || (state.airports.has(value.toUpperCase()) ? value.toUpperCase() : null);
    if (!code) throw Object.assign(new Error("请选择建议中的机场；同城有多座机场时请选具体机场。"), { name: "InputError" });
    return code;
  }
  if (key === "city") {
    const city = state.cityInputs.get(value) || (state.cities.has(value) ? value : null);
    if (!city) throw Object.assign(new Error("请选择建议中的城市。"), { name: "InputError" });
    return city;
  }
  return value;
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
    appendText(item, "p", "card-target", card.target_order_id ? "调整已有订单" : "新建订单");
    const change = el("div", "change-preview");
    appendText(change, "small", "", "变更前"); appendText(change, "div", "", compactSnapshot(card.before, card.action_type));
    appendText(change, "small", "", "变更后"); appendText(change, "div", "", compactSnapshot(card.after, card.action_type));
    item.append(change);
    const amount = el("div", "amount-preview");
    if (card.action_type.endsWith("_book")) {
      appendText(amount, "span", "", `参考总价 ${money(card.price?.new_amount)}`);
    } else if (card.action_type.endsWith("_cancel")) {
      appendText(amount, "span", "", `取消费 ${money(card.price?.cancellation_fee)}`);
      appendText(amount, "span", "", `可退金额 ${money(card.price?.refundable)}`);
    } else {
      appendText(amount, "span", "", `需补差价 ${money(card.price?.payable)}`);
      appendText(amount, "span", "", `可退金额 ${money(card.price?.refundable)}`);
    }
    item.append(amount);
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
      ? "这是作品体验环境。确认后仅修改本人的示例订单，不会产生真实预订、付款或退款。"
      : mode === "reject" ? "拒绝后这张卡失效，本人订单不变。" : "此卡仅记录示例订单的操作，不涉及真实交易。";
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
    notice(result.status === "confirmed" ? "已确认，正在刷新行程。" : "已拒绝，订单未改变。");
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
    state.pendingTurn = null; notice(outcome.actions?.length ? `已生成 ${outcome.actions.length} 张待确认卡，请逐张核对。` : "答复已保存。");
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
  for (const item of data.flight_orders || []) box.append(record(`${airportName(item.departure_airport)} → ${airportName(item.arrival_airport)}`,
    [`当地出发 ${localAtAirport(item.departure_at, item.departure_airport)}`, `当地到达 ${localAtAirport(item.arrival_at, item.arrival_airport)}`,
      `状态 ${status(item.status)} · 票价 ${money(item.amount)}`],
    `航班 ${item.flight_no} · ${item.departure_airport} → ${item.arrival_airport} · 订单 ${item.id} v${item.version}`));
  for (const item of data.hotel_orders || []) box.append(record(productName(item.name),
    [`${cityName(item.city)} · ${roomName(item.room_type)}`, `${item.check_in} 入住 → ${item.check_out} 退房`,
      `状态 ${status(item.status)} · 总价 ${money(item.total_price)}`], `订单 ${item.id} v${item.version}`));
  for (const item of data.rental_orders || []) box.append(record(productName(item.name),
    [`${airportName(item.airport_code)} · 当地取车 ${local(item.pickup_local)}`, `当地还车 ${local(item.dropoff_local)}`,
      `状态 ${status(item.status)} · 总价 ${money(item.total_price)}`], `机场 ${item.airport_code} · 订单 ${item.id} v${item.version}`));
  if (!box.children.length) empty(box, "当前账号还没有行程订单。");
}
async function refreshHistory() {
  const data = await request("/api/v1/me/action-history");
  const box = $("history-list"); box.replaceChildren();
  for (const item of data) box.append(record(({ proposed: "已提交待确认", confirmed: "已确认", rejected: "已拒绝",
    expired: "已过期" })[item.event_type] || "操作记录",
    [formatObject(item.summary)], `${displayStamp(item.created_at)} · 动作 ${item.action_id || "—"}`));
  if (!data.length) empty(box, "还没有操作记录。");
}
function formQuery(form) {
  const params = new URLSearchParams();
  for (const [key, value] of new FormData(form)) if (String(value).trim()) params.set(key, queryValue(key, String(value).trim()));
  params.set("limit", "12"); return params;
}
async function search(kind, form) {
  const button = form.querySelector("button[type=submit]"); button.disabled = true;
  const box = $("search-results"); empty(box, "正在查询可用候选…");
  try {
    const data = await request(`/api/v1/${kind}?${formQuery(form)}`);
    box.replaceChildren();
    for (const item of data) {
      if (kind === "flights") box.append(record(`${airportName(item.departure_airport)} → ${airportName(item.arrival_airport)}`,
        [`当地出发 ${localAtAirport(item.departure_at, item.departure_airport)}`,
          `当地到达 ${localAtAirport(item.arrival_at, item.arrival_airport)}`,
          `参考报价 ${money(item.demo_fare_amount)}`],
        `航班 ${item.flight_no} · ${item.departure_airport} → ${item.arrival_airport} · 候选 ${item.id} v${item.offer_version}`));
      else if (kind === "hotels") box.append(record(`${productName(item.name)} · ${roomName(item.room_type)}`,
        [`${cityName(item.city)} · 每晚 ${money(item.nightly_price)}`,
          item.distance_km === null ? "" : `距机场约 ${item.distance_km} 公里（直线）`], `产品 ${item.product_id}`));
      else box.append(record(productName(item.name),
        [`${airportName(item.airport_code)} · 每日 ${money(item.daily_price)}`,
          item.distance_km === null ? "" : `距机场约 ${item.distance_km} 公里（直线）`],
        `机场 ${item.airport_code} · 产品 ${item.id}`));
    }
    if (!data.length) empty(box, "这个条件下没有可用候选，请调整日期或地点。");
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
    const [profile, demo, locations] = await Promise.all([
      request("/api/v1/me"), request("/api/v1/demo-state"), request("/api/v1/locations")]);
    $("identity").textContent = (profile.display_name || profile.username).replace(/^演示旅客\s*/, "旅客 ");
    initDates(demo); loadLocationOptions(locations); $("login-view").hidden = true; $("workspace").hidden = false;
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
