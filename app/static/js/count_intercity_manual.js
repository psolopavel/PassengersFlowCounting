/**
 * Міжмісто з ручною хронологією: рахувальник за відео відмічає початок маршруту,
 * зупинки та кінець маршруту, а кількість людей вводить на кожен відрізок.
 *
 * Стан — chronology: [{uid, route_id, route_name, closed, marks: [{uid, stop_id,
 * stop_name, utc, count_after}]}]. Відрізок = пара сусідніх відміток ходки,
 * його кількість — count_after першої з них. Сервер виводить із chronology ті ж
 * segments, що й автоматичний режим.
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
let externalDigitEntry = false;

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

function routeName(routeId, fallback = "") {
    const r = routeById.get(routeId);
    return r ? r.name : (fallback || `Маршрут ${routeId}`);
}

function stopName(routeId, stopId, fallback = "") {
    const s = stopsOf(routeId).find((x) => x.id === stopId);
    return s ? s.name : (fallback || `Зупинка #${stopId}`);
}

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
    refreshStopSuggestion();
    if (focusCount && !$("cmCount").disabled) {
        $("cmCount").focus();
        $("cmCount").select();
    }
}

// ── дії ──────────────────────────────────────────────────────────────

function fail(msg) {
    $("ccSaveState").textContent = msg;
    return null;
}

function changed() {
    sortTrips();
    $("ccSaveState").textContent = "";
    render();
}

function selectedRouteId() {
    return Number($("cmRoute")?.value) || null;
}

function selectedStopId() {
    return Number($("cmStop")?.value) || null;
}

function makeMark(routeId, stopId, utc) {
    return { uid: newUid(), stop_id: stopId, stop_name: stopName(routeId, stopId), utc, count_after: null };
}

function startTrip(routeId = selectedRouteId(), stopId = selectedStopId(), utc = nowUtc()) {
    if (openTrip()) return fail("Спочатку завершіть поточну ходку.");
    if (!routeById.has(routeId)) return fail("Оберіть маршрут.");
    if (!stopsOf(routeId).some((s) => s.id === stopId)) return fail("Оберіть зупинку маршруту.");
    if (tripCovering(utc)) return fail("Поточний час усередині іншої ходки.");
    const mark = makeMark(routeId, stopId, utc);
    const trip = { uid: newUid(), route_id: routeId, route_name: routeName(routeId), closed: false, marks: [mark] };
    trips.push(trip);
    changed();
    select(trip.uid, mark.uid, { focusCount: true });
    return trip;
}

/** Ходка, в яку піде нова зупинка: вибраний відрізок, що містить utc, або відкрита ходка. */
function stopTarget(utc) {
    const seg = selectedSegment();
    if (seg && utc > seg.from.utc && (!seg.to || utc < seg.to.utc)) return seg.trip;
    const open = openTrip();
    if (open && utc > tripEnd(open)) return open;
    const covering = tripCovering(utc);
    if (covering && utc > tripStart(covering)) return covering;
    return null;
}

function addStop() {
    const utc = nowUtc();
    const trip = stopTarget(utc);
    if (!trip) return fail("Немає ходки на поточний час — спочатку почніть маршрут.");
    const stopId = selectedStopId();
    if (!stopsOf(trip.route_id).some((s) => s.id === stopId)) return fail("Оберіть зупинку маршруту.");
    if (trip.marks.some((m) => m.utc === utc)) return fail("На цей час уже є відмітка.");
    const mark = makeMark(trip.route_id, stopId, utc);
    trip.marks.push(mark);
    changed();
    select(trip.uid, mark.uid, { focusCount: true });
    return mark;
}

function finishTrip() {
    const trip = openTrip();
    if (!trip) return fail("Немає незавершеної ходки.");
    const utc = nowUtc();
    if (utc <= tripEnd(trip)) return fail("Кінець маршруту має бути пізніше останньої зупинки.");
    const stopId = selectedStopId();
    if (!stopsOf(trip.route_id).some((s) => s.id === stopId)) return fail("Оберіть кінцеву зупинку.");
    const mark = makeMark(trip.route_id, stopId, utc);
    trip.marks.push(mark);
    trip.closed = true;
    changed();
    sel = null;
    applyWindow();
    applyRouteSuggestion();
    render();
    refreshStopSuggestion();
    return { trip, mark };
}

function reverseTrip() {
    const trip = openTrip();
    if (!trip) return fail("Немає незавершеної ходки.");
    const inverse = routeById.get((routeById.get(trip.route_id) || {}).inverse_route_id);
    if (!inverse) return fail("Для цього маршруту не вказано зворотній.");
    const done = finishTrip();
    if (!done) return null;
    const firstStop = (inverse.stops || [])[0];
    if (!firstStop) return fail("У зворотного маршруту немає зупинок.");
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
    refreshStopSuggestion();
}

function deleteSelectedMark() {
    const s = selectedMark();
    if (!s) return;
    const name = s.mark.stop_name || stopName(s.trip.route_id, s.mark.stop_id);
    if (!window.confirm(`Видалити зупинку «${name}» (${clock(s.mark.utc)})?`)) return;
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
    refreshStopSuggestion();
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

// ── підказки в селектах ──────────────────────────────────────────────

function fillSelect(selectEl, items, value) {
    if (!selectEl) return;
    selectEl.innerHTML = items
        .map((it) => `<option value="${it.id}">${escapeHtml(it.name)}</option>`)
        .join("");
    if (value !== null && value !== undefined && items.some((it) => it.id === value)) {
        selectEl.value = String(value);
    }
}

function suggestedRouteId() {
    const done = trips.filter((t) => t.closed && t.marks.length);
    const last = done[done.length - 1];
    if (last) {
        const inv = (routeById.get(last.route_id) || {}).inverse_route_id;
        if (routeById.has(inv)) return inv;
        if (routeById.has(last.route_id)) return last.route_id;
    }
    return routes.length ? routes[0].id : null;
}

function applyRouteSuggestion() {
    const routeSel = $("cmRoute");
    if (!routeSel) return;
    const open = openTrip();
    fillSelect(routeSel, routes, open ? open.route_id : suggestedRouteId());
}

/** Наступна зупинка за порядком маршруту після відмітки mark. */
function nextStopId(routeId, mark) {
    const stops = stopsOf(routeId);
    if (!stops.length) return null;
    if (!mark) return stops[0].id;
    const idx = stops.findIndex((s) => s.id === mark.stop_id);
    return (stops[idx + 1] || stops[idx] || stops[0]).id;
}

function refreshStopSuggestion() {
    if (readOnly) return;
    const open = openTrip();
    const seg = selectedSegment();
    let routeId;
    let suggestion;
    if (seg) {
        routeId = seg.trip.route_id;
        suggestion = nextStopId(routeId, seg.from);
    } else if (open) {
        routeId = open.route_id;
        suggestion = nextStopId(routeId, open.marks[open.marks.length - 1]);
    } else {
        routeId = selectedRouteId();
        suggestion = nextStopId(routeId, null);
    }
    fillSelect($("cmStop"), stopsOf(routeId), suggestion);
    const routeSel = $("cmRoute");
    if (routeSel) {
        if (open || seg) routeSel.value = String((seg ? seg.trip : open).route_id);
        routeSel.disabled = !!open || !!seg || !routes.length;
    }
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
    renderSelected();
    updateButtons();
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
            : "Ходок ще немає. Знайдіть у відео початок маршруту, оберіть маршрут і зупинку та натисніть «Почати маршрут».";
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
                const reopen = document.createElement("button");
                reopen.type = "button";
                reopen.className = "cm-icon-btn";
                reopen.title = "Продовжити ходку (зняти завершення)";
                reopen.textContent = "↺";
                reopen.addEventListener("click", (e) => { e.stopPropagation(); reopenTrip(trip); });
                tools.appendChild(reopen);
            }
            const del = document.createElement("button");
            del.type = "button";
            del.className = "cm-icon-btn cm-icon-danger";
            del.title = "Видалити ходку";
            del.textContent = "✕";
            del.addEventListener("click", (e) => { e.stopPropagation(); deleteTrip(trip); });
            tools.appendChild(del);
            head.appendChild(tools);
        }
        tripEl.appendChild(head);

        for (const seg of segs) {
            const btn = document.createElement("button");
            btn.type = "button";
            btn.className = "cc-seg";
            if (sel && sel.markUid === seg.from.uid) btn.classList.add("is-active");
            if (segmentFilled(seg)) btn.classList.add("is-filled");
            if (!seg.to) btn.classList.add("is-open");
            const from = seg.from.stop_name || stopName(trip.route_id, seg.from.stop_id);
            const to = seg.to ? (seg.to.stop_name || stopName(trip.route_id, seg.to.stop_id)) : "…";
            const t1 = seg.to ? clockHm(seg.to.utc) : "…";
            const count = segmentFilled(seg) ? String(seg.from.count_after) : "—";
            btn.innerHTML =
                `<span class="cc-seg-stops">${escapeHtml(from)} – ${escapeHtml(to)}</span>`
                + `<span class="cc-seg-row">`
                + `<span class="cc-seg-time">${clockHm(seg.from.utc)} – ${t1}</span>`
                + `<span class="cc-seg-count">${count}</span>`
                + `</span>`;
            btn.addEventListener("click", () => select(trip.uid, seg.from.uid, { focusCount: !readOnly }));
            tripEl.appendChild(btn);
        }

        if (trip.closed && trip.marks.length) {
            const last = trip.marks[trip.marks.length - 1];
            const end = document.createElement("button");
            end.type = "button";
            end.className = "cm-trip-end";
            if (sel && sel.markUid === last.uid) end.classList.add("is-active");
            end.innerHTML = `<span>Кінець: ${escapeHtml(last.stop_name || stopName(trip.route_id, last.stop_id))}</span>`
                + `<span class="cc-seg-time">${clockHm(last.utc)}</span>`;
            end.addEventListener("click", () => select(trip.uid, last.uid));
            tripEl.appendChild(end);
        }
        body.appendChild(tripEl);
    }

    const active = body.querySelector(".is-active");
    if (active) active.scrollIntoView({ block: "nearest" });
}

function renderSelected() {
    const s = selectedMark();
    const seg = selectedSegment();
    const info = $("cmSelectedInfo");
    const input = $("cmCount");

    if (!s) {
        info.textContent = "Відрізок не вибрано";
    } else if (seg) {
        const from = seg.from.stop_name || stopName(seg.trip.route_id, seg.from.stop_id);
        const to = seg.to ? (seg.to.stop_name || stopName(seg.trip.route_id, seg.to.stop_id)) : "…";
        info.textContent = `${from} – ${to} · ${clock(seg.from.utc)}${seg.to ? " – " + clock(seg.to.utc) : ""}`;
    } else {
        info.textContent = `Кінець ходки: ${s.mark.stop_name || stopName(s.trip.route_id, s.mark.stop_id)} · ${clock(s.mark.utc)}`;
    }

    input.disabled = readOnly || !seg;
    input.value = seg && segmentFilled(seg) ? String(seg.from.count_after) : "";

    if (readOnly) return;
    const markStop = $("cmMarkStop");
    if (s) {
        fillSelect(markStop, stopsOf(s.trip.route_id), s.mark.stop_id);
        $("cmMarkStop").parentElement.querySelector(".muted").textContent =
            seg ? "Початкова зупинка:" : "Кінцева зупинка:";
    } else {
        markStop.innerHTML = "";
    }
    markStop.disabled = !s;
    $("cmSetTime").disabled = !s;
    $("cmDeleteMark").disabled = !s;
}

function updateButtons() {
    if (readOnly) return;
    const utc = nowUtc();
    const open = openTrip();
    $("cmStart").disabled = !!open || !routes.length || !!tripCovering(utc);
    $("cmAddStop").disabled = !stopTarget(utc);
    const canFinish = !!open && utc > tripEnd(open);
    $("cmFinish").disabled = !canFinish;
    const inverse = open && routeById.get((routeById.get(open.route_id) || {}).inverse_route_id);
    $("cmReverse").disabled = !canFinish || !inverse;
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
    input.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
            e.preventDefault();
            input.blur();
        }
    });
    input.addEventListener("blur", () => { externalDigitEntry = false; });

    if (readOnly) return;
    $("cmRoute").addEventListener("change", refreshStopSuggestion);
    $("cmStart").addEventListener("click", () => startTrip());
    $("cmAddStop").addEventListener("click", addStop);
    $("cmFinish").addEventListener("click", finishTrip);
    $("cmReverse").addEventListener("click", reverseTrip);
    $("cmMarkStop").addEventListener("change", (e) => setSelectedStop(Number(e.target.value)));
    $("cmSetTime").addEventListener("click", setSelectedTime);
    $("cmDeleteMark").addEventListener("click", deleteSelectedMark);
    $("ccSave").addEventListener("click", save);

    const hotkeys = { KeyB: () => startTrip(), KeyS: addStop, KeyE: finishTrip, KeyR: reverseTrip };
    window.addEventListener("keydown", (e) => {
        if (e.ctrlKey || e.metaKey || e.altKey || e.isComposing || e.repeat) return;
        const el = document.activeElement;
        if (el && ["INPUT", "SELECT", "TEXTAREA"].includes(el.tagName)) return;
        const action = hotkeys[e.code];
        if (action) {
            const btn = { KeyB: "cmStart", KeyS: "cmAddStop", KeyE: "cmFinish", KeyR: "cmReverse" }[e.code];
            e.preventDefault();
            if (!$(btn).disabled) action();
            return;
        }
        if (!shouldCaptureDigits()) return;
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
}

player.bindControls();
bindSidebarToggle();
bindEditor();
sortTrips();
renderSidebar();
renderSelected();

(async () => {
    const ok = await player.loadMedia(cfg.mediaUrl);
    if (!ok && !player.media) return;
    player.mount();
    applyRouteSuggestion();
    const open = openTrip();
    if (open) {
        const last = open.marks[open.marks.length - 1];
        select(open.uid, last.uid);
    } else {
        applyWindow();
        render();
        refreshStopSuggestion();
    }
    if (!readOnly) setInterval(updateButtons, TICK_MS);
})();
