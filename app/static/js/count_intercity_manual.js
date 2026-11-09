/**
 * Міжмісто з ручною хронологією: рахувальник за відео відмічає початок маршруту,
 * зупинки та кінець маршруту, а кількість людей вводить на кожен відрізок.
 *
 * Стан — chronology: [{uid, route_id, route_name, closed, marks: [{uid, stop_id,
 * stop_name, utc, count_after}]}]. Відрізок = пара сусідніх відміток ходки,
 * його кількість — count_after першої з них. Сервер виводить із chronology ті ж
 * segments, що й автоматичний режим.
 *
 * Відмітки додаються з бічної панелі («+» або клавіша +) на поточному часі відео;
 * що саме додати (початок маршруту, зупинку, кінець), визначає контекст часу.
 *
 * Вікно плеєра: закритий відрізок — [початок, кінець]; відкритий «A – …» —
 * [A, кінець дня]; без вибору — від кінця останньої ходки (або весь день).
 */
import { MultiCamPlayer, clock, clockHm } from "./count_core.js";

const cfg = JSON.parse(document.getElementById("ccConfig").textContent);
const $ = (id) => document.getElementById(id);

const readOnly = !!cfg.readOnly;
const maxPeople = Number(cfg.maxPeopleCountInCar) || 100;
const routes = cfg.routesCatalog || [];
const routeById = new Map(routes.map((r) => [r.id, r]));
const TICK_MS = 300;

let trips = (cfg.chronology || []).map((t) => ({
    uid: t.uid || newUid(),
    route_id: t.route_id,
    route_name: t.route_name || "",
    closed: !!t.closed,
    marks: (t.marks || []).map((m) => ({
        uid: m.uid || newUid(),
        stop_id: m.stop_id,
        stop_name: m.stop_name || "",
        utc: m.utc,
        count_after: m.count_after ?? null,
    })),
}));
// Вибрана відмітка: початок відрізка або кінцева відмітка закритої ходки.
let sel = null;
// Відкрита форма додавання відмітки: { key } — ключ контексту, щоб перебудовувати форму лише при його зміні.
let composer = null;
let externalDigitEntry = false;
let shiftAlone = false;

function newUid() {
    return Math.random().toString(36).slice(2, 10) + Date.now().toString(36).slice(-4);
}

function escapeHtml(s) {
    return String(s)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
}

// ── модель ───────────────────────────────────────────────────────────

const tripStart = (t) => (t.marks.length ? t.marks[0].utc : Infinity);
const tripEnd = (t) => (t.marks.length ? t.marks[t.marks.length - 1].utc : -Infinity);
const openTrip = () => trips.find((t) => !t.closed) || null;
const findTrip = (uid) => trips.find((t) => t.uid === uid) || null;
const stopsOf = (routeId) => (routeById.get(routeId) || {}).stops || [];
const inverseOf = (routeId) => routeById.get((routeById.get(routeId) || {}).inverse_route_id) || null;

function routeName(routeId, fallback = "") {
    const r = routeById.get(routeId);
    return r ? r.name : (fallback || `Маршрут ${routeId}`);
}

function stopName(routeId, stopId, fallback = "") {
    const s = stopsOf(routeId).find((x) => x.id === stopId);
    return s ? s.name : (fallback || `Зупинка #${stopId}`);
}

const markName = (trip, mark) => mark.stop_name || stopName(trip.route_id, mark.stop_id);

function sortTrips() {
    trips.forEach((t) => t.marks.sort((a, b) => a.utc - b.utc));
    trips.sort((a, b) => tripStart(a) - tripStart(b));
}

function dayWindow() {
    const w = (player.media && player.media.window) || cfg.window || {};
    return { start: w.start_utc, end: w.end_utc };
}

function nowUtc() {
    return Math.round(player.winStart + player.master);
}

// Ходка, всередині якої лежить utc (для відкритої — будь-що після початку).
function tripCovering(utc) {
    return trips.find((t) => t.marks.length && utc >= tripStart(t)
        && (!t.closed || utc < tripEnd(t))) || null;
}

// Початок найближчої ходки після trip (межа, за яку trip не може тягнутися).
function nextTripStart(trip) {
    const starts = trips.filter((t) => t !== trip && tripStart(t) > tripStart(trip)).map(tripStart);
    return starts.length ? Math.min(...starts) : Infinity;
}

// Остання ходка, що завершилася не пізніше utc (для підказки зворотного маршруту).
function lastClosedBefore(utc) {
    let best = null;
    for (const t of trips) {
        if (t.closed && t.marks.length && tripEnd(t) <= utc && (!best || tripEnd(t) > tripEnd(best))) best = t;
    }
    return best;
}

function occurrenceMap() {
    const byRoute = new Map();
    const out = new Map();
    for (const t of trips) {
        if (!t.marks.length) continue;
        const n = (byRoute.get(t.route_id) || 0) + 1;
        byRoute.set(t.route_id, n);
        out.set(t.uid, n);
    }
    return out;
}

/** Відрізки ходки: закриті пари відміток + відкритий «A – …» у незавершеної. */
function tripSegments(trip) {
    const segs = [];
    for (let i = 0; i < trip.marks.length; i++) {
        const from = trip.marks[i];
        const to = trip.marks[i + 1] || null;
        if (!to && trip.closed) break;
        segs.push({ trip, from, to });
    }
    return segs;
}

function selectedMark() {
    if (!sel) return null;
    const trip = findTrip(sel.tripUid);
    const mark = trip && trip.marks.find((m) => m.uid === sel.markUid);
    return mark ? { trip, mark, idx: trip.marks.indexOf(mark) } : null;
}

/** Вибраний відрізок (null, якщо вибрана кінцева відмітка або нічого). */
function selectedSegment() {
    const s = selectedMark();
    if (!s) return null;
    const to = s.trip.marks[s.idx + 1] || null;
    if (!to && s.trip.closed) return null;
    return { trip: s.trip, from: s.mark, to };
}

function segmentFilled(seg) {
    return seg.from.count_after !== null && seg.from.count_after !== undefined;
}

/** Наступна зупинка за порядком маршруту після відмітки mark. */
function nextStopId(routeId, mark) {
    const stops = stopsOf(routeId);
    if (!stops.length) return null;
    if (!mark) return stops[0].id;
    const idx = stops.findIndex((s) => s.id === mark.stop_id);
    return (stops[idx + 1] || stops[idx] || stops[0]).id;
}

function isLastStop(routeId, stopId) {
    const stops = stopsOf(routeId);
    return stops.length > 0 && stops[stops.length - 1].id === stopId;
}

// ── вікно плеєра ─────────────────────────────────────────────────────

function applyWindow(seekUtc = null) {
    const day = dayWindow();
    if (!day.start || !day.end) return;
    let start = day.start;
    let end = day.end;
    const s = selectedMark();
    const seg = selectedSegment();
    if (seg) {
        start = seg.from.utc;
        end = seg.to ? seg.to.utc : day.end;
    } else if (!s) {
        const closedEnds = trips.filter((t) => t.closed && t.marks.length).map(tripEnd);
        const lastEnd = closedEnds.length ? Math.max(...closedEnds) : null;
        if (lastEnd && lastEnd < day.end - 60 && !openTrip()) start = lastEnd;
    }
    if (end - start < 1) end = start + 1;
    player.setWindow(start, end);
    if (seekUtc !== null) player.seekTo(seekUtc - start);
}

function showWholeDay() {
    syncCountInput();
    sel = null;
    const day = dayWindow();
    if (day.start && day.end) player.setWindow(day.start, day.end);
    render();
}

function select(tripUid, markUid, { focusCount = false, seekUtc = null } = {}) {
    syncCountInput();
    sel = tripUid && markUid ? { tripUid, markUid } : null;
    externalDigitEntry = false;
    const s = selectedMark();
    if (s && !selectedSegment()) {
        // Кінцева відмітка — весь день, плеєр на ній.
        const day = dayWindow();
        if (day.start && day.end) {
            player.setWindow(day.start, day.end);
            player.seekTo(s.mark.utc - day.start);
        }
    } else {
        applyWindow(seekUtc);
    }
    render();
    if (focusCount && !$("cmCount").disabled) {
        $("cmCount").focus();
        $("cmCount").select();
    }
}

// ── навігація по відрізках (Enter / Shift) ──────────────────────────

function navList() {
    return trips.flatMap((t) => tripSegments(t).map((s) => ({ tripUid: t.uid, markUid: s.from.uid })));
}

function navStep(dir) {
    syncCountInput();
    const list = navList();
    if (!list.length) return;
    let pos = sel ? list.findIndex((x) => x.markUid === sel.markUid) : -1;
    if (pos < 0 && sel) {
        // Вибрано кінець ходки — крокуємо від її останнього відрізка.
        const trip = findTrip(sel.tripUid);
        const lastSeg = trip && trip.marks[trip.marks.length - 2];
        pos = lastSeg ? list.findIndex((x) => x.markUid === lastSeg.uid) : -1;
    }
    if (pos < 0) pos = dir > 0 ? -1 : list.length;
    const next = pos + dir;
    if (next < 0) return;
    const focusCount = document.activeElement === $("cmCount");
    if (next >= list.length) {
        renderSidebar();
        $("ccSaveState").textContent = "Це останній відрізок.";
        $("ccSave")?.focus();
        return;
    }
    select(list[next].tripUid, list[next].markUid, { focusCount });
}

// ── дії ──────────────────────────────────────────────────────────────

function fail(msg) {
    $("ccSaveState").textContent = msg;
    const box = composer && $("cmComposer")?.querySelector(".cm-composer-msg");
    if (box) box.textContent = msg;
    return null;
}

function changed() {
    sortTrips();
    $("ccSaveState").textContent = "";
    render();
}

function makeMark(routeId, stopId, utc) {
    return { uid: newUid(), stop_id: stopId, stop_name: stopName(routeId, stopId), utc, count_after: null };
}

function hasStop(routeId, stopId) {
    return stopsOf(routeId).some((s) => s.id === stopId);
}

function startTrip(routeId, stopId, utc) {
    if (openTrip()) return fail("Спочатку завершіть поточну ходку.");
    if (!routeById.has(routeId)) return fail("Оберіть маршрут.");
    if (!hasStop(routeId, stopId)) return fail("Оберіть зупинку маршруту.");
    if (tripCovering(utc)) return fail("Поточний час усередині іншої ходки.");
    const mark = makeMark(routeId, stopId, utc);
    const trip = { uid: newUid(), route_id: routeId, route_name: routeName(routeId), closed: false, marks: [mark] };
    trips.push(trip);
    changed();
    select(trip.uid, mark.uid, { focusCount: true });
    return trip;
}

function addStop(trip, stopId, utc) {
    if (!hasStop(trip.route_id, stopId)) return fail("Оберіть зупинку маршруту.");
    if (trip.marks.some((m) => m.utc === utc)) return fail("На цей час уже є відмітка.");
    if (utc >= nextTripStart(trip)) return fail("Час заходить на наступну ходку.");
    const mark = makeMark(trip.route_id, stopId, utc);
    trip.marks.push(mark);
    changed();
    select(trip.uid, mark.uid, { focusCount: true });
    return mark;
}

function finishTrip(trip, stopId, utc) {
    if (trip.closed) return fail("Ходку вже завершено.");
    if (utc <= tripEnd(trip)) return fail("Кінець маршруту має бути пізніше останньої зупинки.");
    if (utc > nextTripStart(trip)) return fail("Час заходить на наступну ходку.");
    if (!hasStop(trip.route_id, stopId)) return fail("Оберіть кінцеву зупинку.");
    syncCountInput();
    const mark = makeMark(trip.route_id, stopId, utc);
    trip.marks.push(mark);
    trip.closed = true;
    sel = null;
    changed();
    applyWindow();
    render();
    return { trip, mark };
}

function reverseTrip(trip, stopId, utc) {
    const inverse = inverseOf(trip.route_id);
    if (!inverse) return fail("Для цього маршруту не вказано зворотній.");
    const firstStop = (inverse.stops || [])[0];
    if (!firstStop) return fail("У зворотного маршруту немає зупинок.");
    const done = finishTrip(trip, stopId, utc);
    if (!done) return null;
    return startTrip(inverse.id, firstStop.id, done.mark.utc);
}

function reopenTrip(trip) {
    if (openTrip()) return fail("Спочатку завершіть поточну ходку.");
    trip.closed = false;
    changed();
    const last = trip.marks[trip.marks.length - 1];
    if (last) select(trip.uid, last.uid, { focusCount: true });
    return trip;
}

function deleteTrip(trip) {
    const name = routeName(trip.route_id, trip.route_name);
    if (!window.confirm(`Видалити ходку «${name}» з усіма зупинками?`)) return;
    trips = trips.filter((t) => t !== trip);
    if (sel && sel.tripUid === trip.uid) sel = null;
    changed();
    applyWindow();
}

function deleteSelectedMark() {
    const s = selectedMark();
    if (!s) return;
    if (!window.confirm(`Видалити зупинку «${markName(s.trip, s.mark)}» (${clock(s.mark.utc)})?`)) return;
    const wasLast = s.idx === s.trip.marks.length - 1;
    s.trip.marks.splice(s.idx, 1);
    if (!s.trip.marks.length) {
        trips = trips.filter((t) => t !== s.trip);
        sel = null;
    } else {
        if (wasLast && s.trip.closed && !openTrip()) s.trip.closed = false;
        const prev = s.trip.marks[Math.max(0, s.idx - 1)];
        sel = { tripUid: s.trip.uid, markUid: prev.uid };
    }
    changed();
    applyWindow();
}

function setSelectedTime() {
    const s = selectedMark();
    if (!s) return;
    const utc = nowUtc();
    if (s.trip.marks.some((m) => m !== s.mark && m.utc === utc)) {
        fail("На цей час уже є відмітка цієї ходки.");
        return;
    }
    s.mark.utc = utc;
    changed();
    applyWindow(utc);
}

function setSelectedStop(stopId) {
    const s = selectedMark();
    if (!s || !stopId) return;
    s.mark.stop_id = stopId;
    s.mark.stop_name = stopName(s.trip.route_id, stopId);
    changed();
}

// ── форма «+»: відмітка на поточному часі ───────────────────────────

/**
 * Що можна відмітити на utc:
 *  new      — початок маршруту (поза ходками);
 *  continue — зупинка або кінець відкритої ходки (після її останньої відмітки);
 *  insert   — проміжна зупинка всередині ходки;
 *  error    — нічого (на цей час уже є відмітка або є незавершена ходка).
 */
function composerContext(utc) {
    const covering = tripCovering(utc);
    if (covering) {
        if (covering.marks.some((m) => m.utc === utc)) {
            return { kind: "error", message: "На цей час уже є відмітка. Зсуньте відео." };
        }
        if (!covering.closed && utc > tripEnd(covering)) {
            return { kind: "continue", trip: covering, prev: covering.marks[covering.marks.length - 1] };
        }
        const prev = [...covering.marks].reverse().find((m) => m.utc < utc);
        return { kind: "insert", trip: covering, prev };
    }
    const open = openTrip();
    if (open) {
        return {
            kind: "error",
            message: `Спочатку завершіть ходку «${routeName(open.route_id, open.route_name)}» `
                + `(вона почалася о ${clock(tripStart(open))}).`,
        };
    }
    return { kind: "new", after: lastClosedBefore(utc) };
}

function contextKey(ctx) {
    return [ctx.kind, ctx.trip ? ctx.trip.uid : "", ctx.prev ? ctx.prev.uid : "",
        ctx.after ? ctx.after.uid : "", ctx.message || ""].join(":");
}

function openComposer() {
    if (readOnly) return;
    if (!routes.length) {
        fail("До машини не прив'язано маршрутів.");
        return;
    }
    syncCountInput();
    player.pause();
    composer = { key: null };
    renderComposer(true);
}

function closeComposer() {
    composer = null;
    renderComposer();
}

function optionHtml(item, label = item.name) {
    return `<option value="${item.id}">${escapeHtml(label)}</option>`;
}

function routeOptionsHtml(ctx) {
    const inverse = ctx.after ? inverseOf(ctx.after.route_id) : null;
    if (!inverse) return routes.map((r) => optionHtml(r)).join("");
    const rest = routes.filter((r) => r.id !== inverse.id);
    return optionHtml(inverse, `↩ Зворотній: ${inverse.name}`)
        + (rest.length
            ? `<optgroup label="Інші прив'язані маршрути">${rest.map((r) => optionHtml(r)).join("")}</optgroup>`
            : "");
}

function composerButton(label, title, onClick) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn btn-sm";
    btn.textContent = label;
    btn.title = title;
    btn.addEventListener("click", onClick);
    return btn;
}

function renderComposer(focus = false) {
    const box = $("cmComposer");
    if (!box) return;
    $("cmAdd")?.classList.toggle("is-active", !!composer);
    if (!composer) {
        box.hidden = true;
        box.innerHTML = "";
        return;
    }
    const utc = nowUtc();
    const ctx = composerContext(utc);
    composer.key = contextKey(ctx);
    box.hidden = false;

    const titles = {
        new: "Початок маршруту",
        continue: `${routeName(ctx.trip?.route_id, ctx.trip?.route_name)}: зупинка або кінець`,
        insert: `${routeName(ctx.trip?.route_id, ctx.trip?.route_name)}: проміжна зупинка`,
        error: "Відмітка неможлива",
    };
    box.innerHTML =
        `<div class="cm-composer-head">`
        + `<span class="cm-composer-title">${escapeHtml(titles[ctx.kind])}</span>`
        + `<span class="cm-composer-time">${clock(utc)}</span>`
        + `<button type="button" class="cm-icon-btn" data-close title="Закрити (Esc)">✕</button>`
        + `</div>`;
    const closeBtn = box.querySelector("[data-close]");
    closeBtn.addEventListener("click", closeComposer);
    box.onkeydown = (e) => {
        if (e.key === "Escape") {
            e.preventDefault();
            closeComposer();
        } else if ((e.key === "Enter" && e.target.tagName !== "BUTTON") || e.key === "+" || e.code === "NumpadAdd") {
            e.preventDefault();
            composer?.primary?.click();
        }
    };

    const msg = document.createElement("div");
    msg.className = "cm-composer-msg";

    if (ctx.kind === "error") {
        composer.primary = null;
        msg.textContent = ctx.message;
        box.appendChild(msg);
        if (focus) closeBtn.focus();
        return;
    }

    const routeId = () => (ctx.kind === "new" ? Number(routeSel.value) : ctx.trip.route_id);
    let routeSel = null;
    if (ctx.kind === "new") {
        routeSel = document.createElement("select");
        routeSel.setAttribute("aria-label", "Маршрут");
        routeSel.innerHTML = routeOptionsHtml(ctx);
        box.appendChild(routeSel);
    }
    const stopSel = document.createElement("select");
    stopSel.setAttribute("aria-label", "Зупинка");
    box.appendChild(stopSel);

    const actions = document.createElement("div");
    actions.className = "cm-composer-actions";
    box.append(actions, msg);

    const stopId = () => Number(stopSel.value);
    const now = () => nowUtc();
    const done = (res) => { if (res) closeComposer(); };
    let buttons = [];

    if (ctx.kind === "new") {
        buttons = [composerButton("Почати маршрут", "Enter",
            () => done(startTrip(routeId(), stopId(), now())))];
    } else if (ctx.kind === "insert") {
        buttons = [composerButton("Додати зупинку", "Enter",
            () => done(addStop(ctx.trip, stopId(), now())))];
    } else {
        buttons = [
            composerButton("Зупинка", "Проміжна зупинка", () => done(addStop(ctx.trip, stopId(), now()))),
            composerButton("Кінець маршруту", "Завершити ходку на цій зупинці",
                () => done(finishTrip(ctx.trip, stopId(), now()))),
        ];
        const inverse = inverseOf(ctx.trip.route_id);
        if (inverse) {
            buttons.push(composerButton("Кінець + зворотній", `Завершити і одразу почати «${inverse.name}»`,
                () => done(reverseTrip(ctx.trip, stopId(), now()))));
        }
    }
    actions.append(...buttons);

    // Основна дія (Enter / +): для відкритої ходки на кінцевій зупинці — завершення.
    const refreshPrimary = () => {
        const primaryIdx = ctx.kind === "continue" && isLastStop(routeId(), stopId()) ? 1 : 0;
        buttons.forEach((b, i) => b.classList.toggle("btn-primary", i === primaryIdx));
        composer.primary = buttons[primaryIdx];
    };
    const fillStops = () => {
        const rid = routeId();
        const suggestion = ctx.kind === "new" ? nextStopId(rid, null) : nextStopId(rid, ctx.prev);
        stopSel.innerHTML = stopsOf(rid).map((s) => optionHtml(s)).join("");
        if (suggestion) stopSel.value = String(suggestion);
        refreshPrimary();
    };
    routeSel?.addEventListener("change", fillStops);
    stopSel.addEventListener("change", refreshPrimary);
    fillStops();
    if (focus) (routeSel || stopSel).focus();
}

function tickComposer() {
    if (!composer) return;
    const utc = nowUtc();
    if (contextKey(composerContext(utc)) !== composer.key) {
        const hadFocus = $("cmComposer").contains(document.activeElement);
        renderComposer(hadFocus);
        return;
    }
    const time = $("cmComposer").querySelector(".cm-composer-time");
    if (time) time.textContent = clock(utc);
}

// ── кількість людей ──────────────────────────────────────────────────

function parseCount(raw) {
    let v = String(raw ?? "").trim().replace(",", ".").replace(/[^\d.]/g, "");
    if (v === "" || v === ".") return null;
    const n = parseFloat(v);
    if (Number.isNaN(n) || n < 0) return null;
    return Math.min(n, maxPeople);
}

function syncCountInput() {
    const seg = selectedSegment();
    const input = $("cmCount");
    if (!seg || !input || input.disabled) return;
    seg.from.count_after = parseCount(input.value);
}

function shouldCaptureDigits() {
    const input = $("cmCount");
    if (!input || input.disabled) return false;
    const el = document.activeElement;
    if (!el || el === document.body) return true;
    return !["INPUT", "SELECT", "TEXTAREA"].includes(el.tagName) && !el.isContentEditable;
}

function applyCountDigits(raw) {
    const input = $("cmCount");
    const n = parseCount(raw);
    input.value = n === null ? "" : String(n);
    syncCountInput();
    renderSidebar();
}

// ── відмальовка ──────────────────────────────────────────────────────

function render() {
    renderSidebar();
    renderCount();
}

function iconButton(text, title, onClick, danger = false) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "cm-icon-btn" + (danger ? " cm-icon-danger" : "");
    btn.title = title;
    btn.textContent = text;
    btn.addEventListener("click", (e) => { e.stopPropagation(); onClick(); });
    return btn;
}

/** Правка вибраної відмітки: зупинка, час (з плеєра), видалення. */
function markEditRow(trip, mark, label) {
    const row = document.createElement("div");
    row.className = "cm-edit";
    const stopSel = document.createElement("select");
    stopSel.title = label;
    stopSel.setAttribute("aria-label", label);
    stopSel.innerHTML = stopsOf(trip.route_id).map((s) => optionHtml(s)).join("");
    stopSel.value = String(mark.stop_id);
    stopSel.addEventListener("change", () => setSelectedStop(Number(stopSel.value)));
    row.append(
        stopSel,
        iconButton("⏱", "Перенести відмітку на поточний час відео", setSelectedTime),
        iconButton("✕", "Видалити цю зупинку", deleteSelectedMark, true),
    );
    return row;
}

function renderSidebar() {
    const body = $("ccSidebarBody");
    if (!body) return;
    const all = trips.flatMap(tripSegments);
    const closedSegs = all.filter((s) => s.to);
    $("ccSegProgress").textContent = `${closedSegs.filter(segmentFilled).length} / ${closedSegs.length}`;
    body.innerHTML = "";

    if (!trips.length) {
        const empty = document.createElement("div");
        empty.className = "cc-sidebar-empty";
        empty.textContent = readOnly
            ? "Хронологію ще не створено."
            : "Ходок ще немає. Знайдіть у відео початок маршруту і натисніть «+» (або клавішу +).";
        body.appendChild(empty);
        return;
    }

    const occ = occurrenceMap();
    const open = openTrip();
    const lastTrip = trips[trips.length - 1];
    for (const trip of trips) {
        const segs = tripSegments(trip);
        const tripEl = document.createElement("div");
        tripEl.className = "cc-trip" + (trip.closed ? "" : " is-open");

        const head = document.createElement("div");
        head.className = "cc-trip-head";
        const done = segs.filter((s) => s.to && segmentFilled(s)).length;
        const total = segs.filter((s) => s.to).length;
        head.innerHTML =
            `<span class="cc-trip-name">${escapeHtml(routeName(trip.route_id, trip.route_name))}`
            + ` · ходка #${occ.get(trip.uid) || "?"}</span>`
            + `<span class="cc-trip-meta">${trip.closed ? `${done}/${total}` : "триває"}</span>`;
        if (!readOnly) {
            const tools = document.createElement("span");
            tools.className = "cm-trip-tools";
            if (trip.closed && !open && trip === lastTrip) {
                tools.appendChild(iconButton("↺", "Продовжити ходку (зняти завершення)", () => reopenTrip(trip)));
            }
            tools.appendChild(iconButton("✕", "Видалити ходку", () => deleteTrip(trip), true));
            head.appendChild(tools);
        }
        tripEl.appendChild(head);

        for (const seg of segs) {
            const active = !!sel && sel.markUid === seg.from.uid;
            const btn = document.createElement("button");
            btn.type = "button";
            btn.className = "cc-seg";
            if (active) btn.classList.add("is-active");
            if (segmentFilled(seg)) btn.classList.add("is-filled");
            if (!seg.to) btn.classList.add("is-open");
            const to = seg.to ? markName(trip, seg.to) : "…";
            const t1 = seg.to ? clockHm(seg.to.utc) : "…";
            const count = segmentFilled(seg) ? String(seg.from.count_after) : "—";
            btn.innerHTML =
                `<span class="cc-seg-stops">${escapeHtml(markName(trip, seg.from))} – ${escapeHtml(to)}</span>`
                + `<span class="cc-seg-row">`
                + `<span class="cc-seg-time">${clockHm(seg.from.utc)} – ${t1}</span>`
                + `<span class="cc-seg-count">${count}</span>`
                + `</span>`;
            btn.addEventListener("click", () => select(trip.uid, seg.from.uid, { focusCount: !readOnly }));
            tripEl.appendChild(btn);
            if (active && !readOnly) tripEl.appendChild(markEditRow(trip, seg.from, "Зупинка початку відрізка"));
        }

        if (trip.closed && trip.marks.length) {
            const last = trip.marks[trip.marks.length - 1];
            const active = !!sel && sel.markUid === last.uid;
            const end = document.createElement("button");
            end.type = "button";
            end.className = "cm-trip-end" + (active ? " is-active" : "");
            end.innerHTML = `<span>Кінець: ${escapeHtml(markName(trip, last))}</span>`
                + `<span class="cc-seg-time">${clockHm(last.utc)}</span>`;
            end.addEventListener("click", () => select(trip.uid, last.uid));
            tripEl.appendChild(end);
            if (active && !readOnly) tripEl.appendChild(markEditRow(trip, last, "Кінцева зупинка"));
        }
        body.appendChild(tripEl);
    }

    const active = body.querySelector(".is-active");
    if (active) active.scrollIntoView({ block: "nearest" });
}

function renderCount() {
    const seg = selectedSegment();
    const input = $("cmCount");
    input.disabled = readOnly || !seg;
    input.value = seg && segmentFilled(seg) ? String(seg.from.count_after) : "";
}

// ── графік: ходки і зупинки ──────────────────────────────────────────

function drawOverlay(ctx, p) {
    const h = p.SPEED_H;
    const now = nowUtc();
    for (const trip of trips) {
        if (!trip.marks.length) continue;
        const x1 = p.xOf(tripStart(trip));
        const x2 = p.xOf(trip.closed ? tripEnd(trip) : Math.max(tripEnd(trip), now));
        ctx.fillStyle = trip.closed ? "rgba(245, 158, 11, 0.12)" : "rgba(56, 189, 248, 0.12)";
        ctx.fillRect(x1, 0, Math.max(1, x2 - x1), h);
        for (const m of trip.marks) {
            const x = p.xOf(m.utc);
            const active = sel && sel.markUid === m.uid;
            ctx.strokeStyle = active ? "#38bdf8" : "#f59e0b";
            ctx.lineWidth = active ? 2 : 1;
            ctx.beginPath();
            ctx.moveTo(x, 0);
            ctx.lineTo(x, h);
            ctx.stroke();
        }
    }
}

// ── збереження ───────────────────────────────────────────────────────

function chronologyPayload() {
    syncCountInput();
    return trips.map((t) => ({
        uid: t.uid,
        route_id: t.route_id,
        route_name: routeName(t.route_id, t.route_name),
        closed: t.closed,
        marks: t.marks.map((m) => ({ ...m })),
    }));
}

function payloadBase(pendingSec) {
    return {
        day: cfg.dayIso,
        counter_id: cfg.counterId,
        panelType: cfg.panelType || "whole_day",
        add_time_sec: pendingSec,
    };
}

const player = new MultiCamPlayer({
    cameras: cfg.cameras,
    autosaveUrl: cfg.autosaveUrl,
    sourcePriority: cfg.sourcePriority,
    drawOverlay,
    buildPayload(pendingSec) {
        return { ...payloadBase(pendingSec), chronology: chronologyPayload() };
    },
});

function selectError(err) {
    if (!err) return;
    const trip = err.trip_uid && findTrip(err.trip_uid);
    if (!trip) return;
    const mark = (err.mark_uid && trip.marks.find((m) => m.uid === err.mark_uid)) || trip.marks[0];
    if (mark) select(trip.uid, mark.uid);
}

async function save() {
    syncCountInput();
    $("ccSave").disabled = true;
    $("ccSaveState").textContent = "Збереження…";
    try {
        await player.flushAutosave(false);
        const resp = await fetch(cfg.saveUrl, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                ...payloadBase(player.takePendingTimeSec()),
                chronology: chronologyPayload(),
            }),
        });
        const j = await resp.json();
        if (!resp.ok || !j.ok) {
            if (j.error === "invalid_chronology" && (j.errors || []).length) {
                selectError(j.errors[0]);
                const more = j.errors.length > 1 ? ` (ще помилок: ${j.errors.length - 1})` : "";
                throw new Error(j.errors[0].message + more);
            }
            throw new Error(j.error || resp.status);
        }
        $("ccSaveState").textContent = "Збережено ✓ Закриваємо…";
        setTimeout(() => window.close(), 400);
        setTimeout(() => {
            const typeQs = cfg.panelType && cfg.panelType !== "whole_day" ? `&type=${cfg.panelType}` : "";
            $("ccSaveState").innerHTML = 'Збережено ✓ <a href="/panel/?day=' + cfg.dayIso + typeQs + '">До списку</a>';
        }, 900);
    } catch (e) {
        $("ccSaveState").textContent = "Не збережено: " + (e.message || e);
        $("ccSave").disabled = false;
    }
}

// ── прив'язка подій ──────────────────────────────────────────────────

function bindSidebarToggle() {
    const root = $("cc");
    const btn = $("ccSidebarToggle");
    btn?.addEventListener("click", () => {
        root.classList.toggle("is-sidebar-collapsed");
        btn.classList.toggle("is-collapsed");
        btn.title = root.classList.contains("is-sidebar-collapsed") ? "Розгорнути список" : "Згорнути список";
        player.resizeCanvas();
    });
}

const isPlusKey = (e) => e.key === "+" || e.code === "NumpadAdd";

/** Фокус там, де Enter/Shift/цифри мають працювати як гарячі клавіші панелі. */
function hotkeysAllowed() {
    const el = document.activeElement;
    if (!el || el === document.body) return true;
    if (el === $("cmCount")) return true;
    if ($("cmComposer")?.contains(el)) return false;
    if (["INPUT", "SELECT", "TEXTAREA"].includes(el.tagName) || el.isContentEditable) return false;
    // Кнопки-картки відрізків — так; інші кнопки (Зберегти, ✕ …) обробляють Enter самі.
    return el.tagName !== "BUTTON" || el.matches(".cc-seg, .cm-trip-end");
}

function bindEditor() {
    $("cmWholeDay")?.addEventListener("click", showWholeDay);
    const input = $("cmCount");
    input.addEventListener("input", () => { syncCountInput(); renderSidebar(); });
    input.addEventListener("change", () => {
        const n = parseCount(input.value);
        input.value = n === null ? "" : String(n);
        syncCountInput();
        renderSidebar();
    });
    input.addEventListener("blur", () => { externalDigitEntry = false; });

    if (!readOnly) {
        $("cmAdd")?.addEventListener("click", (e) => {
            e.currentTarget.blur();
            if (composer) closeComposer();
            else openComposer();
        });
        $("ccSave").addEventListener("click", save);
    }

    window.addEventListener("keydown", (e) => {
        if (e.key === "Shift") {
            shiftAlone = !e.repeat;
            return;
        }
        shiftAlone = false;
        if (e.metaKey || e.altKey || e.isComposing) return;
        if (!hotkeysAllowed()) return;

        if (!readOnly && !e.ctrlKey && isPlusKey(e)) {
            e.preventDefault();
            if (!e.repeat) openComposer();
            return;
        }
        if (e.key === "Enter") {
            e.preventDefault();
            if (!e.repeat) navStep(e.ctrlKey ? -1 : 1);
            return;
        }
        if (e.ctrlKey || e.repeat || readOnly || !shouldCaptureDigits()) return;
        if (/^\d$/.test(e.key)) {
            e.preventDefault();
            const next = externalDigitEntry ? $("cmCount").value + e.key : e.key;
            externalDigitEntry = true;
            applyCountDigits(next);
        } else if (e.key === "Backspace" || e.key === "Delete") {
            e.preventDefault();
            const next = $("cmCount").value.slice(0, -1);
            externalDigitEntry = next.length > 0;
            applyCountDigits(next);
        }
    });
    // Shift сам по собі (без іншої клавіші, напр. Shift+= для «+») — попередній відрізок.
    window.addEventListener("keyup", (e) => {
        if (e.key !== "Shift" || !shiftAlone) return;
        shiftAlone = false;
        if (hotkeysAllowed()) navStep(-1);
    });
}

player.bindControls();
bindSidebarToggle();
bindEditor();
sortTrips();
render();

(async () => {
    const ok = await player.loadMedia(cfg.mediaUrl);
    if (!ok && !player.media) return;
    player.mount();
    const open = openTrip();
    if (open) {
        const last = open.marks[open.marks.length - 1];
        select(open.uid, last.uid);
    } else {
        applyWindow();
        render();
    }
    if (!readOnly) setInterval(tickComposer, TICK_MS);
})();
