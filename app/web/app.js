"use strict";

const SESSION_KEY = "jev-demo-session";
const DEV_KEY = "jev-demo-dev";
const RESET = new Error("the demo session was reset");
const UNREACHABLE = "The store could not be reached. Check your connection and try again.";
const PENDING_AFTER_MS = 500; // a reply slower than this gets the "Checking your order..." status
const PENDING_MIN_MS = 400; // once shown, the status stays this long: a reply just behind it must not make it flash
const GESTURE_MS = 400; // the second click of a double click, or the second tap of a double tap, comes within this
const REPEAT_MS = 1000; // the same action again within this of its last try or its answer is not a new action

// Local product images by item id, as paths relative to this page: { "ITM-1001": "img/blue-kurta.jpg" }.
// The project has none yet and the orders API names none, so for now every garment gets the same neutral tile.
const IMAGES = {};

const INTENTS = ["track", "cancel", "return", "missing", "refund"];

// The shortcuts shown for each status, most useful first. The other intents sit under "More help".
const RELEVANT = {
  packed: ["track", "cancel", "return"],
  shipped: ["track", "cancel", "missing"],
  delivered: ["return", "missing"],
  cancelled: ["refund", "track"],
  return_requested: ["track", "refund"],
  refund_processing: ["refund", "track"],
};

// What each route means, for the lead of the trace drawer. The server's own reason is shown with it.
const ROUTES = {
  rule: ["Rule reply", "The reply is the draft written from the order record. Jev scored it and accepted it."],
  fallback: ["Fallback reply", "There was no rule draft, or Jev rejected it. The fallback model wrote this reply; it passed the backend checks and Jev accepted it."],
  fallback_clarify: ["Fallback question", "There was no rule draft, or Jev rejected it. The fallback model asked a question instead; it passed the backend checks and Jev accepted it."],
  clarify_item: ["Item needed", "The backend could not tell which item the message is about, so it asked. No model was called."],
  mandated_ticket: ["Ticket required by a backend rule", "A backend rule decided this before any draft was written. Jev was not asked."],
  handoff: ["Ticket decided by Jev or the fallback", "No backend rule required this ticket. It was opened because Jev, the fallback model or the checks on its reply called for a person."],
  gate_error: ["Ticket after a gate error", "Jev returned no usable scores, so nothing unverified was sent and the message went to a person."],
  ticket_failed: ["Ticket could not be stored", "A ticket was needed but storing it failed, so the customer was offered a retry."],
};

// The gate thresholds ticked on each score meter.
const TICKS = {
  supported: ["supported_min"],
  complete: ["complete_min"],
  requires_human: ["requires_human_low", "requires_human_handoff"],
};

const $ = (id) => document.getElementById(id);
const rail = $("rail");
const panel = $("orders-panel");
const ordersList = $("orders");
const messages = $("messages");
const quick = $("quick");
const quickFirst = $("quick-first");
const moreToggle = $("more-help");
const moreBox = $("quick-more");
const input = $("chat-input");
const devToggle = $("dev-toggle");
const picker = $("item-picker");
const drawer = $("trace-drawer");
const pendingStatus = el("p", { class: "pending", "data-testid": "pending-status", role: "status" }, "Checking your order...");

let session = recall(SESSION_KEY) || null;
let starting = null; // the POST /api/session in flight, shared by concurrent callers
let resetting = false;
let busy = false; // one chat or confirm request at a time
let pendingTimer = 0;
let pendingSince = 0; // when the status went up; 0 while it is not shown
let last = { key: "", at: 0 }; // the action last tried, and when: see accept()
let settleTimer = 0;
// ponytail: items are found by linear scan; fine for a demo's dozen items, index them by id in a Map if the list grows.
let items = []; // every item of the last GET /api/orders
let selected = null; // id of the item the chat is about
let gate = null; // the gate of the last GET /api/trace: version, model and thresholds
const replies = []; // the replies the server keeps a trace for, oldest first: {node, message, text, trace, open}
let traceReads = 0; // numbers the reads of GET /api/trace, so that the answer to an overtaken read is dropped
let traceOpener = null; // the control whose trace the drawer shows
let refocus = true; // closing the drawer hands focus back to that control
let ids = 0; // numbers the elements that another element's description points to
let atEnd = true; // the end of the thread is in view

// Server and user text only ever enters the page here, as text nodes. Nothing is parsed as HTML.
function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attrs || {})) node.setAttribute(name, value);
  for (const child of children) {
    if (child == null || child === false || child === "") continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

// sessionStorage can throw inside a third-party iframe (the Hugging Face page); the demo then lives in memory only.
function recall(key) {
  try {
    return sessionStorage.getItem(key);
  } catch {
    return null;
  }
}

function remember(key, value) {
  try {
    sessionStorage.setItem(key, value);
  } catch {
    // storage is blocked: carry on without it
  }
}

const fmtDate = (iso) => new Date(iso).toLocaleDateString("en-US", { month: "long", day: "numeric", timeZone: "UTC" });
// ponytail: lower-cases the first letter blindly, which suits names like "Blue kurta";
// a name that starts with a proper noun or acronym needs a sentence form sent by the server.
const lowerFirst = (text) => text.charAt(0).toLowerCase() + text.slice(1);
const upperFirst = (text) => text.charAt(0).toUpperCase() + text.slice(1);
const find = (id) => items.find((it) => it.id === id);
const uid = (kind) => `${kind}-${++ids}`;
const narrow = () => rail.offsetParent === null; // the stylesheet hides the orders column on a narrow screen
// The stylesheet also says whether the trace drawer has room beside the workspace.
const beside = () => getComputedStyle(drawer).getPropertyValue("--beside").trim() === "1";

// ---- session and API ----

function startSession() {
  starting ||= fetch("api/session", { method: "POST" })
    .then((res) => (res.ok ? res.json() : Promise.reject(new Error(`session ${res.status}`))))
    .then((data) => {
      session = data.session;
      remember(SESSION_KEY, session);
    })
    .finally(() => {
      starting = null;
    });
  return starting;
}

// Resolves to {status, data}. A 401 resets the demo and throws RESET, which callers ignore.
async function api(method, path, body) {
  if (!session) await startSession();
  const used = session;
  const res = await fetch(path, {
    method,
    headers: body ? { "X-Session": used, "Content-Type": "application/json" } : { "X-Session": used },
    body: body && JSON.stringify(body),
  });
  if (res.status === 401) {
    if (used !== session) throw RESET; // another request already started the reset
    if (resetting) throw new Error("the new session was rejected"); // never loop on 401
    await resetDemo();
    throw RESET;
  }
  return { status: res.status, data: await res.json().catch(() => null) };
}

async function resetDemo() {
  resetting = true;
  session = null;
  try {
    closeTrace();
    replies.length = 0;
    // A button of the thread that has the focus is about to go, and would drop it: the thread takes it.
    if (messages.contains(document.activeElement)) messages.focus({ preventScroll: true });
    messages.replaceChildren();
    note("The demo was reset, so earlier messages and order changes are gone. You can start again.");
    await loadOrders();
  } finally {
    resetting = false;
  }
}

// ---- orders ----

async function loadOrders() {
  try {
    const { status, data } = await api("GET", "api/orders");
    if (status !== 200) throw new Error(`orders ${status}`);
    items = data.orders.flatMap((order) => order.items.map((item) => ({ ...item, order_id: order.id })));
    $("orders-meta").replaceChildren(el("span", null, `Shopping as ${data.customer.name}.`), " ", el("span", null, `Today is ${fmtDate(data.today)}.`));
    // A row that has the focus is about to be replaced, and would drop it: the new row of that item takes it.
    const held = ordersList.contains(document.activeElement) && document.activeElement.dataset.item;
    ordersList.replaceChildren(...data.orders.map(orderGroup));
    if (held) [...ordersList.querySelectorAll("[data-item]")].find((row) => row.dataset.item === held)?.focus({ preventScroll: true });
    select(selected);
  } catch (err) {
    if (err === RESET) return;
    console.error(err);
    note("Your orders could not be loaded. Reload the page to try again.", true);
  }
}

function orderGroup(order) {
  return el("section", { class: "group" },
    el("h3", { class: "group__head" }, el("span", { class: "group__id" }, order.id), " ", el("span", null, `Placed ${fmtDate(order.placed_at)}`)),
    el("ul", null, ...items.filter((item) => item.order_id === order.id).map(orderRow)));
}

// The whole row is one button. The single spaces keep its accessible name readable.
function orderRow(item) {
  const row = el("button", { type: "button", class: "row", "data-testid": `order-card-${item.id}`, "data-item": item.id },
    thumb(item),
    el("span", { class: "row__text" },
      el("span", { class: "row__name" }, item.name), " ",
      el("span", { class: "row__size" }, item.variant), " ",
      el("span", { class: "row__state" }, el("span", { class: "status" }, statusWord(item)), " ", when(item))));
  return el("li", null, row);
}

// The neutral tile every garment shares. A local image listed in IMAGES covers the drawing; if it fails to
// load, the drawing is still there.
function thumb(item) {
  const tile = $("thumb-template").content.firstElementChild.cloneNode(true);
  const src = item && IMAGES[item.id];
  if (src) {
    const img = el("img", { src, alt: "", loading: "lazy" });
    img.addEventListener("error", () => img.remove());
    tile.append(img);
  }
  return tile;
}

const statusWord = (item) => upperFirst(item.status.replace(/_/g, " "));

// The date that goes with the status: when a parcel on its way is due, or when a delivered one came.
function when(item) {
  if ((item.status === "packed" || item.status === "shipped") && item.eta) return `Expected ${fmtDate(item.eta)}`;
  if (item.status === "delivered" && item.delivered_at) return `on ${fmtDate(item.delivered_at)}`;
  return "";
}

// Make `id` the item the chat is about; null is a general question. `settled` is a customer message whose item
// the server has just resolved.
function select(id, settled) {
  const item = find(id);
  selected = item ? item.id : null;
  for (const row of panel.querySelectorAll("[data-item]")) {
    const current = row.dataset.item === (selected || "");
    row.setAttribute("aria-current", String(current));
    if (current && ordersList.contains(row)) reveal(row, ordersList, 0); // the marked row is in view in its list
  }
  rail.classList.remove("rail--choosing");

  // The header says what the orders API knows. It has no refund, return or eligibility details, so for those
  // the status word is all there is, and the delivery date stands in as the most useful fact.
  $("context-thumb").replaceChildren(...(item ? [thumb(item)] : []));
  $("context-thumb").hidden = !item;
  $("context-name").textContent = item ? item.name : "No item selected";
  $("context-meta").replaceChildren(...(item
    ? [el("span", null, `Order ${item.order_id}`), " ", el("span", null, item.variant)]
    : ["Ask a general question, or choose an item from your orders."]));
  $("context-state").replaceChildren(...(item
    ? [el("span", { class: "status" }, statusWord(item)), " ",
      el("span", null, when(item) || (item.delivered_at ? `Delivered ${fmtDate(item.delivered_at)}` : ""))]
    : []));
  $("change-item").textContent = item ? "Change item" : "Choose item";
  $("chat-label").textContent = item ? `Message about ${item.name}` : "Message";
  input.placeholder = item ? `Ask about your ${item.noun}` : "Write a message";

  showShortcuts(item);
  markContext(settled);
}

// ---- shortcuts ----

// The label on a shortcut and the sentence it sends; {item} becomes the item's name. Each sentence names the
// item and asks one thing, because the store checks its draft answer against the message.
// The orders API says nothing about eligibility, so cancelling and returning are asked as questions and the
// store decides. If it ever sends permitted_actions with an item, a permitted action is requested outright.
function shortcut(intent, item) {
  const permitted = Array.isArray(item.permitted_actions) && item.permitted_actions.includes(intent);
  if (intent === "track") {
    if (item.status === "packed" || item.status === "shipped") return ["When will it arrive?", "When will my {item} arrive?"];
    if (item.status === "delivered") return ["When was it delivered?", "When was my {item} delivered?"];
    return ["What is its status?", "What is the status of my {item}?"];
  }
  if (intent === "cancel") return permitted ? ["Cancel this item", "I would like to cancel my {item}."] : ["Can I cancel this?", "Can I cancel my {item}?"];
  if (intent === "return") return permitted ? ["Return this item", "I would like to return my {item}."] : ["Can I return this?", "Can I return my {item}?"];
  if (intent === "missing") return ["I have not received it", "I have not received my {item}."];
  return ["What is the refund status?", "What is the refund status for my {item}?"];
}

function showShortcuts(item) {
  const held = quick.contains(document.activeElement) && document.activeElement;
  quick.hidden = !item;
  showMore(false);
  if (item) {
    const first = RELEVANT[item.status] || INTENTS.slice(0, 3);
    for (const intent of [...first, ...INTENTS.filter((other) => !first.includes(other))]) {
      const chip = quick.querySelector(`[data-intent="${intent}"]`);
      chip.textContent = shortcut(intent, item)[0];
      (first.includes(intent) ? quickFirst : moreBox).append(chip);
    }
    // On a short screen each line scrolls sideways. Another item starts at its first shortcut.
    quickFirst.scrollLeft = moreBox.scrollLeft = 0;
  }
  // Putting a shortcut in its new place drops the focus it had. It gets it back, or the message box does
  // when the shortcut is no longer shown.
  if (held) (held.offsetParent ? held : input).focus({ preventScroll: true });
}

function showMore(open) {
  moreBox.hidden = !open;
  moreToggle.setAttribute("aria-expanded", String(open));
}

// ---- transcript ----

// Bring `node` into view inside `box`, its start winning when both ends cannot show: a reply taller than the
// thread is read from its first line. Only `box` scrolls: scrollIntoView would also move the page that embeds
// this one.
function reveal(node, box, gap) {
  const frame = box.getBoundingClientRect();
  const rect = node.getBoundingClientRect();
  const above = frame.top + gap - rect.top; // how far its start is above the frame
  const below = rect.bottom + gap - frame.bottom; // how far its end is below it
  if (above > 0) box.scrollTop -= Math.ceil(above);
  else if (below > 0) box.scrollTop += Math.ceil(Math.min(below, -above)); // whole pixels: a fraction would be dropped
}

const atBottom = () => messages.scrollHeight - messages.scrollTop - messages.clientHeight < 8;

// Scroll the thread to `node`, and note at once whether that left it at its end: the scroll event comes later.
function show(node) {
  reveal(node, messages, 16);
  atEnd = atBottom();
}

// Put a node at the end of the thread, or right after the node it answers, and bring it into view.
function place(node, after) {
  $("intro")?.remove(); // the opening hint has done its work once something is chosen or said
  if (after && after.isConnected) after.after(node);
  else messages.append(node);
  show(node);
  return node;
}

// itemId is the item the message concerns; the dividers are worked out from it. The inner element holds
// what was said and nothing else, so the developer line `tail` of a reply stays outside the message.
function addMessage(who, text, itemId, extras, after, tail) {
  return place(el("div", { class: `msg msg--${who}`, "data-item": itemId || "" },
    el("div", { "data-testid": who === "agent" ? "message-agent" : "message-customer" },
      el("p", { class: "msg__text", id: uid("said") }, text), extras), tail), after);
}

function note(text, isError, after) {
  place(el("p", { class: isError ? "note note--error" : "note" }, text), after);
}

// When the thread's box gets shorter or its content taller without a new message, the end stays in view
// if it was in view.
function keepEnd() {
  if (atEnd) messages.scrollTop = messages.scrollHeight;
}

// The item the thread was about just before `node`, or at its end: "" for a general question, undefined
// when no message precedes it.
function contextBefore(node) {
  let prev = node ? node.previousElementSibling : messages.lastElementChild;
  while (prev && !prev.matches(".msg")) prev = prev.previousElementSibling;
  return prev ? prev.dataset.item : undefined;
}

// `opening` is the label of the thread's first part: nothing was said before it, so it divides nothing.
function divider(id, opening) {
  const item = find(id);
  return el("p", { class: "divider", "data-testid": opening ? "thread-start" : "thread-divider", "data-item": id || "" },
    item ? `${opening ? "Discussing" : "Now discussing"} ${item.name}.` : "Now asking a general question.");
}

// Every part of the thread that is about an item starts with a line that names it: a label when it is the
// first part, a divider where the conversation moves on from another item. The history stays above it.
// The end of the thread carries one while the selected item is not the one the newest message concerns.
function markContext(settled) {
  if (settled) {
    const lead = settled.previousElementSibling;
    if (lead && lead.matches(".divider")) lead.remove();
    const was = contextBefore(settled);
    if (was !== settled.dataset.item) settled.before(divider(settled.dataset.item, was === undefined));
  }
  let old = null; // the divider after the newest message, if there is one
  for (let node = messages.lastElementChild; node && !node.matches(".msg"); node = node.previousElementSibling) {
    if (node.matches(".divider")) old = node;
  }
  const now = contextBefore(null);
  const needed = now === undefined ? Boolean(selected) : now !== (selected || "");
  if (old && needed && old.dataset.item === (selected || "")) return; // it already says so: leave the thread where it is
  if (old) old.remove();
  if (needed) place(divider(selected, now === undefined));
}

// The controls that send a message or confirm an action. An answered confirmation is not one of them.
const SENDERS = '#send, #quick .chip, .choices .chip, [data-testid="retry"], [data-testid="confirm-action"]:not([data-outcome])';

// One customer action is one request. Every path that sends or confirms asks here first, with a key that
// names the action. Nothing is accepted while a request is in flight. The same action again within a moment
// of its last try or of its answer is dropped too, and starts that moment again: it is the rest of a double
// click, a held key, or a click made while waiting that lands just after the answer. Some answers come back
// in a few milliseconds, so the time in flight alone does not cover these.
function accept(key) {
  if (busy) return false;
  const now = performance.now();
  const again = key === last.key && now - last.at < REPEAT_MS;
  last = { key, at: now };
  return !again;
}

// While a request is in flight nothing else is sent, and the controls that would send say so. They are marked
// unavailable, not disabled: a disabled button drops the keyboard focus, and the customer would lose their place.
// The status appears only if the reply is slow, right after the node that is waiting for it. The server
// reports no stages, so there are none to show.
function setBusy(on, waiting) {
  busy = on;
  for (const control of document.querySelectorAll(SENDERS)) control.setAttribute("aria-disabled", String(on));
  clearTimeout(pendingTimer);
  pendingStatus.remove();
  pendingSince = 0;
  if (on) {
    settle(); // the thread is about to move under the pointer
    pendingTimer = setTimeout(() => {
      place(pendingStatus, waiting);
      pendingSince = performance.now();
    }, PENDING_AFTER_MS);
  } else {
    // The moment in which the same action is not a new one runs from here: from its answer, or from when the
    // buttons take clicks again if the answer came sooner than that.
    last.at = Math.max(performance.now(), last.at + GESTURE_MS);
  }
}

// One chat or confirm request. If the status came up while it ran, the answer waits until the status has been
// readable for a moment: a reply that lands just after half a second would otherwise make it flash.
async function ask(path, body) {
  try {
    return await api("POST", path, body);
  } finally {
    const left = pendingSince && pendingSince + PENDING_MIN_MS - performance.now();
    if (left > 0) await new Promise((done) => setTimeout(done, left));
  }
}

// Every message goes out through here: the message box, a shortcut, an item choice and "Try again".
// Resolves to true when the store answered with a reply.
async function send(message, itemId, quickAction) {
  if (!accept(JSON.stringify([message, itemId, quickAction]))) return false;
  if (itemId !== selected) select(itemId); // the message is about this item, so the chat turns to it
  const asked = addMessage("customer", message, itemId);
  setBusy(true, asked);
  try {
    const { status, data } = await ask("api/chat", { message, item_id: itemId, quick_action: quickAction });
    if ((status === 200 || status === 429) && data && typeof data.reply === "string") {
      addReply(data, { message, itemId, quickAction }, asked);
      // The limit reply says to reload: forget the token so that a reload starts a fresh session.
      if (data.limit_reached) remember(SESSION_KEY, "");
      return true;
    }
    note(status === 422
      ? "Not sent. A message needs between 1 and 500 characters."
      : `Not sent. The store answered with an error (${status}). Try again in a moment.`, true, asked);
  } catch (err) {
    if (err !== RESET) {
      console.error(err);
      note(`Not sent. ${UNREACHABLE}`, true, asked);
    }
  } finally {
    setBusy(false);
  }
  return false;
}

// sent = {message, itemId, quickAction} of the request this payload answers; asked is its message in the thread.
function addReply(payload, sent, asked) {
  // The request named no item and the server worked one out from the message itself. If nothing is selected
  // by now either, the chat is about that item from here on.
  if (payload.item_id && !sent.itemId && !selected) {
    asked.dataset.item = payload.item_id;
    select(payload.item_id, asked);
  }
  const extras = el("div", { class: "msg__extras" });
  if (payload.ticket_ref) extras.append(receipt(payload.ticket_ref, find(payload.item_id)));
  if (payload.pending_action) extras.append(confirmCard(payload.pending_action));
  const choices = (payload.item_choices || []).map((choice) => {
    const button = el("button", { type: "button", class: "chip", "data-testid": `item-choice-${choice.id}` },
      choice.name, " ", el("span", { class: "chip__meta" }, choice.order_id));
    button.addEventListener("click", () => send(sent.message, choice.id, sent.quickAction));
    return button;
  });
  if (choices.length) extras.append(el("div", { class: "choices" }, ...choices));
  if (payload.retry) {
    const button = el("button", { type: "button", class: "btn btn--quiet", "data-testid": "retry" }, "Try again");
    button.addEventListener("click", () => send(sent.message, sent.itemId, sent.quickAction));
    extras.append(button);
  }
  const untraced = payload.limit_reached
    && el("p", { class: "trace-none" }, "No trace: the server records none once the message limit is reached.");
  // The reply is the server's verified text and is shown whole, also when its last sentence repeats the
  // ticket reference that the receipt carries.
  const node = addMessage("agent", payload.reply, asked.dataset.item, extras.childNodes.length ? extras : null, asked, untraced);
  if (untraced) return;
  replies.push({ node, message: sent.message, text: payload.reply, trace: null, open: null });
  loadTraces(node);
}

// The confirmation names its garment and action in the server's words, so it stays clear what it concerns
// after the customer has moved on to another item.
function confirmCard(pending) {
  const item = find(pending.item_id);
  const id = uid("action");
  const button = el("button", { type: "button", class: "btn btn--solid", "data-testid": "confirm-action", "aria-describedby": id },
    { cancel: "Confirm cancellation", return: "Confirm return" }[pending.action] || "Confirm");
  button.addEventListener("click", () => confirmAction(pending.token, button));
  return el("div", { class: "card" },
    thumb(item),
    el("p", { class: "card__title", id }, pending.label),
    item && el("p", { class: "card__meta" }, `Order ${item.order_id}, ${item.variant}`),
    button);
}

// One receipt per ticket, with the reference exactly as the server sent it.
function receipt(ref, item) {
  const shown = el("strong", { "data-testid": "ticket-ref", id: uid("ticket") }, ref);
  const copy = el("button", { type: "button", class: "btn btn--quiet", "data-testid": "ticket-copy", "aria-describedby": shown.id },
    "Copy reference");
  copy.addEventListener("click", async () => {
    copy.textContent = (await copyText(ref, shown)) ? "Copied" : "Selected, now copy";
    setTimeout(() => {
      copy.textContent = "Copy reference";
    }, 2000);
  });
  return el("div", { class: "receipt" },
    el("p", { class: "receipt__label" }, "Support ticket"),
    el("p", { class: "receipt__ref" }, shown),
    copy,
    item && el("p", { class: "receipt__item" }, `${item.name}, order ${item.order_id}`));
}

// Resolves to true when `text` is on the clipboard. The clipboard API is refused inside some iframes and
// missing on old browsers. The same text on screen, in `shown`, is then selected and copied, which leaves the
// keyboard focus on the button. If that fails too it stays selected, so the customer's own copy command works.
async function copyText(text, shown) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    getSelection().selectAllChildren(shown);
    let copied = false;
    try {
      copied = document.execCommand("copy");
    } catch {
      copied = false;
    }
    if (copied) getSelection().removeAllRanges();
    return copied;
  }
}

async function confirmAction(token, button) {
  if (button.dataset.outcome || !accept(token)) return;
  const offer = button.closest(".msg"); // the outcome goes right under the reply that offered the action
  // Once answered, the button is the record of what happened and no longer a control: it stays marked
  // unavailable and leaves the tab order, keeping the focus if it has it.
  const answered = (label, outcome) => {
    button.textContent = label;
    button.dataset.outcome = outcome;
    button.tabIndex = -1;
  };
  setBusy(true, offer);
  try {
    const { status, data } = await ask(`api/actions/${encodeURIComponent(token)}/confirm`);
    if (status === 200 && data && typeof data.reply === "string") {
      answered(data.status === "done" ? "Confirmed" : "Not carried out", data.status);
      addMessage("agent", data.reply, offer.dataset.item, null, offer,
        el("p", { class: "trace-none" }, "No trace: the server records none for a confirmation."));
      loadOrders();
    } else if (status === 404) {
      answered("No longer available", "gone");
      note("That confirmation is no longer available. Ask again to get a new one.", true, offer);
    } else {
      // The button is released with the rest: the store repeats the same outcome for the same token, so
      // retrying is safe.
      note(`Not confirmed. The store answered with an error (${status}). Try again in a moment.`, true, offer);
    }
  } catch (err) {
    if (err !== RESET) {
      console.error(err);
      note(`Not confirmed. ${UNREACHABLE}`, true, offer);
    }
  } finally {
    setBusy(false);
  }
}

// ---- developer mode ----

function showDev(on) {
  document.body.classList.toggle("dev", on);
  devToggle.setAttribute("aria-pressed", String(on));
  $("dev-state").textContent = on ? "On" : "Off";
  remember(DEV_KEY, on ? "1" : "");
  if (on) {
    keepEnd(); // the developer lines make the replies taller
    loadTraces();
  } else {
    closeTrace();
  }
}

// Which trace belongs to which reply. A trace has no id and a chat reply does not name its trace, so they are
// paired by what was said: `asked` lists the replies oldest first as {message, text}, and the result gives
// each one's place in `traces`, or -1. The pairing runs from the newest back, each reply taking the newest
// trace with its message and reply that no later reply has taken, so identical exchanges keep their order.
function matchTraces(asked, traces) {
  const found = asked.map(() => -1);
  let end = traces.length;
  for (let i = asked.length - 1; i >= 0; i -= 1) {
    let at = end - 1;
    while (at >= 0 && (traces[at].message !== asked[i].message || traces[at].reply !== asked[i].text)) at -= 1;
    if (at >= 0) end = found[i] = at;
  }
  return found;
}

// Diagnostics stay closed in customer mode: the traces are read only while developer mode is on, after a
// reply (`fresh`) arrives and when the mode is switched on. Every read pairs all the replies again.
async function loadTraces(fresh) {
  if (!document.body.classList.contains("dev") || !replies.length) return;
  const read = ++traceReads;
  let traces;
  try {
    const { status, data } = await api("GET", "api/trace");
    if (status !== 200) throw new Error(`trace ${status}`);
    // A later read is on its way and knows more; or the mode was switched off, and replies may have come since
    // that this read knows nothing of. Switching it on again reads afresh.
    if (read !== traceReads || !document.body.classList.contains("dev")) return;
    gate = data.gate;
    traces = data.traces;
  } catch (err) {
    if (err !== RESET) console.error(err);
    return;
  }
  matchTraces(replies, traces).forEach((at, i) => {
    if (at < 0) return;
    const entry = replies[i];
    entry.trace = traces[at];
    if (!entry.open) {
      entry.open = el("button", { type: "button", class: "trace-open", "data-testid": "trace-open", "aria-haspopup": "dialog",
        "aria-expanded": "false", "aria-describedby": entry.node.querySelector(".msg__text").id });
      entry.open.addEventListener("click", () => openTrace(entry));
      entry.node.append(entry.open);
    }
    entry.open.replaceChildren(el("span", { class: "route", "data-route": entry.trace.route }, entry.trace.route), " Inspect trace");
  });
  // The controls make the replies taller: the new reply is shown whole, or the thread stays at its end.
  if (fresh && fresh.isConnected) show(fresh);
  else keepEnd();
}

// The drawer shows one reply's trace: beside the workspace where there is room for both, over the page
// where there is not.
function openTrace(entry) {
  if (traceOpener) traceOpener.setAttribute("aria-expanded", "false");
  traceOpener = entry.open;
  refocus = true;
  entry.open.setAttribute("aria-expanded", "true");
  $("trace-body").replaceChildren(traceCard(entry.trace));
  $("trace-body").scrollTop = 0;
  if (!drawer.open) {
    if (beside()) drawer.show();
    else drawer.showModal();
    settle();
  }
  $("trace-close").focus({ preventScroll: true });
}

// Closing leaves the conversation where it was. Focus goes back to the control that opened the drawer when
// it was in the drawer or nowhere; someone who has gone on typing in the message box stays there.
function closeTrace() {
  if (!drawer.open) return;
  refocus = drawer.contains(document.activeElement) || document.activeElement === document.body;
  drawer.close();
}

drawer.addEventListener("close", () => {
  settle();
  const opener = traceOpener;
  traceOpener = null;
  if (!opener) return;
  opener.setAttribute("aria-expanded", "false");
  if (refocus && opener.isConnected) opener.focus({ preventScroll: true });
});

// A sheet or drawer that opens or closes, or a message that is sent, changes what lies under the pointer. For
// a moment after it no button takes a click, so the second tap of a double tap does not press whatever has
// just arrived there. The keyboard is not held back, and neither is scrolling.
function settle() {
  document.body.classList.add("settling");
  clearTimeout(settleTimer);
  settleTimer = setTimeout(() => document.body.classList.remove("settling"), GESTURE_MS);
}

// Route and reason first, then the rule draft, Jev's scores, the fallback when there was one, and the timing.
function traceCard(trace) {
  const item = find(trace.item_id);
  const [title, meaning] = ROUTES[trace.route] || [trace.route, ""];
  const cand = trace.candidate;
  const fb = trace.fallback;
  const calls = (count) => `${count} ${count === 1 ? "call" : "calls"}`;
  return el("article", { class: "trace", "data-testid": "trace-card" },
    el("div", null,
      el("p", null,
        el("span", { class: "route", "data-route": trace.route }, trace.route),
        el("span", { class: "trace__at" }, new Date(trace.at).toLocaleTimeString())),
      el("h3", { class: "trace__title" }, title),
      el("p", { class: "trace__reason" }, trace.reason),
      el("p", { class: "trace__meaning" }, meaning)),
    facts([
      ["Message", trace.message],
      ["Item", item ? `${item.name} (${item.id})` : trace.item_id || "none"],
      ["Intent", trace.intent || "none"],
      ["Reply sent", trace.reply],
      ["Ticket", trace.ticket_ref],
      ["Error", trace.error],
    ]),
    el("section", null,
      el("h3", null, "Rule draft"),
      cand
        ? facts([["Draft", cand.draft], ["Evidence", (cand.evidence || []).join(", ")], ["Offers", cand.action || "no action"]])
        : el("p", { class: "trace__none" }, "No rule draft was written for this message.")),
    el("section", null, el("h3", null, "Jev on the rule draft"), scores(trace.scores, trace.decision)),
    fb && el("section", null,
      el("h3", null, "Fallback"),
      facts([
        ["Kind", fb.kind],
        ["Text", fb.text],
        ["Offers", fb.action || "no action"],
        ["Backend", (fb.problems || []).length ? el("ul", null, ...fb.problems.map((problem) => el("li", null, problem))) : "no problems found"],
      ])),
    fb && el("section", null, el("h3", null, "Jev on the fallback"), scores(trace.fallback_scores, trace.fallback_decision)),
    el("section", null,
      el("h3", null, "Timing"),
      facts([
        ["Total", `${trace.elapsed_ms.total} ms`],
        ["Jev", `${trace.elapsed_ms.jev} ms in ${calls(trace.calls.jev)}`],
        ["Fallback", `${trace.elapsed_ms.fallback} ms in ${calls(trace.calls.fallback)}`],
        ["Cost", trace.cost_usd == null ? "unknown" : `$${Number(trace.cost_usd).toFixed(6)} USD`],
      ])),
    gate && el("section", null,
      el("h3", null, "Gate"),
      facts([["Version", gate.version], ["Model", gate.model], ["Policy", gate.policy_version], ["Status", gate.status]])));
}

// A definition list of [label, value] rows; rows without a value are left out.
function facts(pairs) {
  const list = el("dl", { class: "facts" });
  for (const [label, value] of pairs) {
    if (value == null || value === "") continue;
    list.append(el("dt", null, label), el("dd", null, value));
  }
  return list;
}

// Three meters and the router's decision. The decision is the server's; nothing is re-derived here.
function scores(values, decision) {
  if (!values) return el("p", { class: "trace__none" }, "Not scored.");
  return el("div", { class: "scores" },
    el("p", null, "Decision: ", el("strong", null, decision || "none")),
    ...Object.keys(TICKS).map((name) => meter(name, values[name])));
}

function meter(name, value) {
  const thresholds = (gate && gate.thresholds) || {};
  const known = typeof value === "number";
  const fill = el("span", { class: "meter__fill" });
  fill.style.width = `${known ? Math.min(Math.max(value, 0), 1) * 100 : 0}%`;
  const bar = el("span", { class: "meter__bar", "aria-hidden": "true" }, fill);
  const marks = [];
  for (const key of TICKS[name]) {
    if (typeof thresholds[key] !== "number") continue;
    const tick = el("span", { class: "meter__tick" });
    tick.style.left = `${thresholds[key] * 100}%`;
    bar.append(tick);
    marks.push(`${key} ${thresholds[key].toFixed(2)}`);
  }
  return el("div", { class: "meter" },
    el("span", { class: "meter__name" }, name), bar, el("span", { class: "meter__value" }, known ? value.toFixed(2) : "n/a"),
    el("span", { class: "meter__marks" }, marks.join(", ")));
}

// ---- wiring ----

$("composer").addEventListener("submit", async (event) => {
  event.preventDefault();
  const message = input.value.trim();
  if (!message || busy) return;
  input.value = "";
  input.focus();
  // Once answered, a typed message is done with: the box is empty, so the same words typed again are a new
  // message. When the send fails nothing typed is lost, and a second press does not send it again at once.
  if (await send(message, selected, null)) last.key = "";
  else if (!input.value) input.value = message;
});

quick.addEventListener("click", (event) => {
  const intent = event.target.dataset.intent;
  const item = find(selected);
  if (!intent || !item) return;
  send(shortcut(intent, item)[1].replace("{item}", () => lowerFirst(item.name)), item.id, intent);
});

moreToggle.addEventListener("click", () => showMore(moreBox.hidden));

// On a short screen a line of shortcuts scrolls sideways, and the browser leaves a shortcut that is partly in
// view where it is when the keyboard moves focus to it. It is brought in whole, with its ring. Not for a
// pointer: the shortcut must not move between the press and the release.
quick.addEventListener("focusin", (event) => {
  if (event.target.matches(":focus-visible")) event.target.scrollIntoView({ block: "nearest", inline: "nearest" });
});

messages.addEventListener("scroll", () => {
  atEnd = atBottom();
});
new ResizeObserver(keepEnd).observe(messages); // the on-screen keyboard opens, "More help" unfolds

// Every row of the orders panel selects on click, and Enter and Space click a button.
panel.addEventListener("click", (event) => {
  const row = event.target.closest("[data-item]");
  if (!row) return;
  const led = rail.classList.contains("rail--choosing"); // the customer came here through "Change item"
  select(row.dataset.item || null);
  picker.close();
  if (led) $("change-item").focus(); // and goes back to the conversation, as the sheet does on a narrow screen
});

// "Change item" leads to the orders: the column beside the chat, or on a narrow screen the same panel in a sheet.
$("change-item").addEventListener("click", () => {
  if (narrow()) {
    $("picker-slot").append(panel);
    picker.showModal();
    settle();
  } else {
    rail.classList.add("rail--choosing");
  }
  // Focus brings the selected item into view; with none selected the list starts at its top.
  panel.querySelector(selected ? '[aria-current="true"]' : "[data-item]").focus();
});

rail.addEventListener("focusout", (event) => {
  if (!rail.contains(event.relatedTarget)) rail.classList.remove("rail--choosing");
});

picker.addEventListener("close", () => {
  rail.append(panel);
  settle();
  $("change-item").focus({ preventScroll: true }); // Safari does not focus a clicked button, so the dialog cannot hand focus back itself
});
picker.addEventListener("click", (event) => {
  // A click on the backdrop closes the sheet, but not the second tap of the double tap that opened it.
  if (event.target === picker && !document.body.classList.contains("settling")) picker.close();
});
$("picker-close").addEventListener("click", () => picker.close());

devToggle.addEventListener("click", () => showDev(!document.body.classList.contains("dev")));
$("trace-close").addEventListener("click", closeTrace);
document.addEventListener("keydown", (event) => {
  // A dialog closes itself on Escape only when it is modal; beside the workspace it is not.
  if (event.key === "Escape") closeTrace();
});
// The drawer was opened for the width the window had. If that changes under it, it closes, so it never
// covers the conversation's controls without making them inert.
addEventListener("resize", () => {
  if (drawer.open && drawer.matches(":modal") === beside()) closeTrace();
  // A line of shortcuts that scrolls sideways again, as when the on-screen keyboard opens, starts at its first
  // shortcut: the browser would put it back where it was scrolled to the last time, for another item.
  quickFirst.scrollLeft = moreBox.scrollLeft = 0;
  // A phone turned on its side wraps every line anew, and the browser then moves the thread to hold its place.
  // That scroll is reported before the observer above runs and would read as the reader leaving the end, so
  // the end is kept here, first.
  keepEnd();
});

$("skip").addEventListener("click", (event) => {
  event.preventDefault(); // focus moves; the address, which the embedding page may show, stays as it is
  input.focus();
});

select(null);
showDev(recall(DEV_KEY) === "1");
loadOrders();
