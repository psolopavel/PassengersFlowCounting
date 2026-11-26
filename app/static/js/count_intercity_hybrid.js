/**
 * Міжмісто, гібрид: відрізки визначено за GPS-треком (autoSegs, рахувальник
 * вводить лише кількість людей), а ділянки без треку чи поза маршрутом (windows)
 * розмічаються ходками, як у ручній хронології.
 *
 * Ходки ділянок — trips: [{uid, window_uid, from_left, route_id, route_name, closed,
 * start_stop_id, end_stop_id, marks: [{uid, utc, count_after, anchor?}]}].
 * Ходка from_left — продовження проїзду з треку перед ділянкою: її перша відмітка —
 * якір (кінець треку), маршрут і початкова зупинка незмінні. Незавершена остання
 * ходка ділянки переходить у трек після неї (continuesIntoRight), її останній
 * відрізок закінчується на першій зупинці після ділянки.
 *
 * Зупинки відміток — resolveTrip, та сама логіка, що й
 * hybrid_counting_service.resolve_trip на сервері.
 */
import { MultiCamPlayer, clock, clockHm } from "./count_core.js";

const cfg = JSON.parse(document.getElementById("ccConfig").textContent);
const $ = (id) => document.getElementById(id);

const readOnly = !!cfg.readOnly;
const maxPeople = Number(cfg.maxPeopleCountInCar) || 100;
const catalog = cfg.routesCatalog || [];
const routeById = new Map(catalog.map((r) => [r.id, r]));
// Відв'язані від машини маршрути лишаються в каталозі лише заради наявних ходок.
const routes = catalog.filter((r) => !r.unbound);
const isUnbound = (routeId) => !!(routeById.get(routeId) || {}).unbound;
const TICK_MS = 300;

const REASONS = {
    gap: "розрив треку",
    skip: "трек перескочив зупинки",
    off_route: "поза маршрутом",
    no_track: "немає треку",
    no_route: "трек не ліг на маршрути",
};

const blocks = (cfg.autoBlocks || []).slice().sort((a, b) => a.start_utc - b.start_utc);
const windows = (cfg.windows || []).slice().sort((a, b) => a.start_utc - b.start_utc);
const windowById = new Map(windows.map((w) => [w.uid, w]));
const autoSegs = (cfg.autoSegments || []).map((s) => ({ ...s }))
    .sort((a, b) => a.period_start_utc - b.period_start_utc);
const autoByKey = new Map(autoSegs.map((s) => [s.seg_key, s]));

let trips = (cfg.chronology || []).filter((t) => windowById.has(t.window_uid)).map((t) => ({
    uid: t.uid || newUid(),
    window_uid: t.window_uid,
    from_left: !!t.from_left,
    route_id: t.route_id,
    route_name: t.route_name || "",
    closed: !!t.closed,
    start_stop_id: t.start_stop_id ?? null,
    end_stop_id: t.closed ? (t.end_stop_id ?? null) : null,
    marks: (t.marks || []).map((m) => ({
        uid: m.uid || newUid(),
        utc: m.utc,
        count_after: m.count_after ?? null,
        ...(m.anchor ? { anchor: true } : {}),
    })),
}));
// Вибір: {kind: "auto", key} | {kind: "mark", tripUid, markUid} | {kind: "window", uid}.
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
const findTrip = (uid) => trips.find((t) => t.uid === uid) || null;
const windowOf = (trip) => windowById.get(trip.window_uid);
const inverseOf = (routeId) => routeById.get((routeById.get(routeId) || {}).inverse_route_id) || null;
const operatorMarks = (t) => t.marks.filter((m) => !m.anchor);
const isBareLeft = (t) => t.from_left && !t.closed && !operatorMarks(t).length;

function routeName(routeId, fallback = "") {
    const r = routeById.get(routeId);
    return r ? r.name : (fallback || `Маршрут ${routeId}`);
}

const positionsCache = new Map();

/** Зупинки маршруту з позицією pos (км; без лінії маршруту — номер), за pos. */
function stopsOf(routeId) {
    if (positionsCache.has(routeId)) return positionsCache.get(routeId);
    const stops = (routeById.get(routeId) || {}).stops || [];
    let out;
    if (stops.some((s) => s.km === null || s.km === undefined)) {
        out = stops.map((s, i) => ({ ...s, pos: i }));
    } else {
        out = stops.map((s) => ({ ...s, pos: Number(s.km) }));
        out.sort((a, b) => a.pos - b.pos || (a.order || 0) - (b.order || 0) || a.id - b.id);
    }
    positionsCache.set(routeId, out);
    return out;
}

const hasKm = (routeId) => {
    const stops = (routeById.get(routeId) || {}).stops || [];
    return stops.length > 0 && stops.every((s) => s.km !== null && s.km !== undefined);
};

function stopName(routeId, stopId) {
    const s = stopsOf(routeId).find((x) => x.id === stopId);
    return s ? s.name : `Зупинка #${stopId}`;
}

const stopPos = (routeId, stopId) => (stopsOf(routeId).find((s) => s.id === stopId) || {}).pos;
const hasStop = (routeId, stopId) => stopsOf(routeId).some((s) => s.id === stopId);
const firstStopId = (routeId) => (stopsOf(routeId)[0] || {}).id ?? null;
const lastStopId = (routeId) => (stopsOf(routeId).slice(-1)[0] || {}).id ?? null;

/** Ходки ділянки за часом (ходка from_left — перша). */
function tripsIn(w) {
    return trips.filter((t) => t.window_uid === w.uid)
        .sort((a, b) => (a.from_left === b.from_left ? tripStart(a) - tripStart(b) : (a.from_left ? -1 : 1)));
}

function isLastIn(trip) {
    const list = tripsIn(windowOf(trip));
    return list[list.length - 1] === trip;
}

/** Незавершена остання ходка ділянки переходить у проїзд після неї. */
function continuesIntoRight(trip) {
    const w = windowOf(trip);
    const right = w && w.right;
    if (!right || trip.closed || right.route_id !== trip.route_id || !isLastIn(trip)) return false;
    const a = stopPos(trip.route_id, trip.start_stop_id);
    const b = stopPos(trip.route_id, right.stop_id);
    if (a === undefined || b === undefined || b < a) return false;
    return trip.marks.every((m) => m.utc < right.utc);
}

function interp(knots, utc) {
    if (utc <= knots[0][0]) return knots[0][1];
    for (let i = 1; i < knots.length; i++) {
        const [t0, p0] = knots[i - 1];
        const [t1, p1] = knots[i];
        if (utc <= t1) return t1 === t0 ? p0 : p0 + (utc - t0) / (t1 - t0) * (p1 - p0);
    }
    return knots[knots.length - 1][1];
}

/**
 * Зупинки відміток ходки: {points: [{stop, estimated, mark}], continues} або null.
 * Проміжна відмітка: позиція — лінійно за часом між відомими точками (початок,
 * край треку, кінець), далі найближча зупинка, не раніше попередньої.
 */
function resolveTrip(trip) {
    const marks = trip.marks;
    const stops = stopsOf(trip.route_id);
    if (!marks.length || !stops.length) return null;
    const ia = stops.findIndex((s) => s.id === trip.start_stop_id);
    if (ia < 0) return null;
    const w = windowOf(trip);
    const continues = continuesIntoRight(trip);
    let ib;
    let inner;
    let endMark;
    if (trip.closed) {
        ib = stops.findIndex((s) => s.id === trip.end_stop_id);
        if (ib < 0 || marks.length < 2 || stops[ib].pos <= stops[ia].pos) return null;
        inner = marks.slice(1, -1);
        endMark = marks[marks.length - 1];
    } else if (continues) {
        ib = stops.findIndex((s) => s.id === w.right.stop_id);
        inner = marks.slice(1);
        endMark = { uid: `R${w.uid}`, utc: w.right.utc, count_after: null, virtual: true };
    } else {
        return null;
    }
    const t0 = marks[0].utc;
    const t1 = endMark.utc;
    if (t1 <= t0) return null;
    const posA = stops[ia].pos;
    const posB = stops[ib].pos;

    let knots = [[t0, posA]];
    if (hasKm(trip.route_id)) {
        const { left, right } = w;
        if (trip.from_left && left && left.edge_utc && t0 < left.edge_utc && left.edge_utc < t1) {
            knots.push([left.edge_utc, Number(left.edge_km)]);
        }
        if (continues && right.edge_utc && knots[knots.length - 1][0] < right.edge_utc && right.edge_utc < t1) {
            knots.push([right.edge_utc, Number(right.edge_km)]);
        }
    }
    knots.push([t1, posB]);
    let level = posA;
    knots = knots.map(([t, p]) => {
        level = Math.min(posB, Math.max(level, p));
        return [t, level];
    });

    const points = [{ stop: stops[ia], estimated: false, mark: marks[0] }];
    let lo = ia;
    for (const mark of inner) {
        const pos = interp(knots, mark.utc);
        let best = lo;
        for (let j = lo + 1; j <= ib; j++) {
            if (Math.abs(stops[j].pos - pos) < Math.abs(stops[best].pos - pos)) best = j;
        }
        lo = best;
        points.push({ stop: stops[best], estimated: true, mark });
    }
    points.push({ stop: stops[ib], estimated: false, mark: endMark });
    return { points, continues };
}

/** Назви відміток ходки (+ віртуальної кінцевої): обчислені — з «≈», невідомі — «зупинка N». */
function markLabels(trip, resolved = resolveTrip(trip)) {
    const labels = trip.marks.map((m, i) => {
        if (resolved) return (resolved.points[i].estimated ? "≈ " : "") + resolved.points[i].stop.name;
        if (i === 0 && trip.start_stop_id) return stopName(trip.route_id, trip.start_stop_id);
        if (i === trip.marks.length - 1 && trip.closed && trip.end_stop_id) {
            return stopName(trip.route_id, trip.end_stop_id);
        }
        return `зупинка ${i + 1}`;
    });
    if (resolved && resolved.continues) labels.push(resolved.points[resolved.points.length - 1].stop.name);
    return labels;
}

/**
 * Відрізки ходки: пари сусідніх відміток; у незавершеної — відкритий «A – …»,
 * а якщо вона переходить у трек — останній до першої зупинки після ділянки.
 */
function tripSegments(trip) {
    const resolved = resolveTrip(trip);
    if (resolved && resolved.continues && resolved.points.length === 2
        && resolved.points[0].stop.id === resolved.points[1].stop.id) return [];
    const segs = [];
    for (let i = 0; i < trip.marks.length; i++) {
        const from = trip.marks[i];
        let to = trip.marks[i + 1] || null;
        if (!to && trip.closed) break;
        if (!to && resolved && resolved.continues) to = resolved.points[resolved.points.length - 1].mark;
        segs.push({ kind: "mark", trip, from, to });
    }
    return segs;
}

/** Блоки, у які переходить ходка ділянки перед ними (їхній ведучий «припущений» відрізок не потрібен). */
function continuedBlocks() {
    const out = new Set();
    for (const w of windows) {
        const list = tripsIn(w);
        const last = list[list.length - 1];
        if (last && w.right && continuesIntoRight(last)) out.add(w.right.block_uid);
    }
    return out;
}

function visibleAutoSegs() {
    const cont = continuedBlocks();
    return autoSegs.filter((s) => !(s.assumed_start && cont.has(s.block_uid)));
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

/** Час, на якому є що розмічати: від початку до кінця відео або треку (не далі моменту збірки). */
function dataLimits() {
    let start = cfg.trackStartUtc ?? Infinity;
    let end = cfg.trackEndUtc ?? -Infinity;
    const dayMedia = (player.media && player.media.day_media) || {};
    for (const block of Object.values(dayMedia)) {
        for (const v of (block && block.videos) || []) {
            start = Math.min(start, v.chunk_start_utc);
            end = Math.max(end, v.chunk_stop_utc);
        }
        for (const p of (block && block.photos) || []) {
            start = Math.min(start, p.utc);
            end = Math.max(end, p.utc);
        }
    }
    if (cfg.dataEndUtc) end = Math.min(end, cfg.dataEndUtc);
    return start <= end ? { start, end } : null;
}

const windowAt = (utc) => windows.find((w) => utc > w.start_utc && utc < w.end_utc) || null;

// Ходка ділянки, всередині якої лежить utc (для відкритої — будь-що після початку).
function tripCovering(w, utc) {
    return tripsIn(w).find((t) => t.marks.length && utc >= tripStart(t)
        && (!t.closed || utc < tripEnd(t))) || null;
}

// Межа, за яку trip не може тягнутися: початок наступної ходки ділянки або кінець ділянки.
function tripLimit(trip) {
    const w = windowOf(trip);
    const starts = tripsIn(w).filter((t) => t !== trip && tripStart(t) > tripStart(trip)).map(tripStart);
    return Math.min(w.end_utc, ...starts);
}

function lastClosedBefore(w, utc) {
    let best = null;
    for (const t of tripsIn(w)) {
        if (t.closed && t.marks.length && tripEnd(t) <= utc && (!best || tripEnd(t) > tripEnd(best))) best = t;
    }
    return best;
}

/** Стан ділянки для списку: {state: "ok" | "empty" | "error", text — лише для помилки}. */
function windowStatus(w) {
    const list = tripsIn(w);
    const engaged = list.some((t) => !isBareLeft(t));
    const right = w.right;
    for (const [i, trip] of list.entries()) {
        const name = routeName(trip.route_id, trip.route_name);
        if (isUnbound(trip.route_id)) return { state: "error", text: `Маршрут «${name}» більше не прив'язаний до машини.` };
        if (trip.closed) {
            if (!resolveTrip(trip)) return { state: "error", text: `Ходка «${name}»: кінцева зупинка має бути далі за маршрутом, ніж початкова.` };
            continue;
        }
        if (continuesIntoRight(trip)) continue;
        if (isBareLeft(trip) && !(right && engaged)) continue;
        if (i < list.length - 1) return { state: "error", text: `Ходка «${name}» не завершена — позначте її кінець перед наступною ходкою.` };
        if (right) {
            const after = `після ділянки — «${routeName(right.route_id)}» від «${stopName(right.route_id, right.stop_id)}»`;
            return { state: "error", text: `Ходка «${name}» не стикується з треком: ${after}. Позначте кінець ходки.` };
        }
        return { state: "error", text: `Ходка «${name}» не завершена — вкажіть кінцеву зупинку.` };
    }
    const last = list[list.length - 1];
    if (engaged && right && right.open && (!last || last.closed)) {
        return {
            state: "error",
            text: `Після ділянки машина вже їде маршрутом «${routeName(right.route_id)}» `
                + `(«${stopName(right.route_id, right.stop_id)}» о ${clockHm(right.utc)}). `
                + "Почніть останню ходку цим маршрутом і не завершуйте — вона продовжиться за треком.",
        };
    }
    return { state: engaged || (last && continuesIntoRight(last)) ? "ok" : "empty", text: "" };
}

// ── вибір ────────────────────────────────────────────────────────────

function selectedMark() {
    if (!sel || sel.kind !== "mark") return null;
    const trip = findTrip(sel.tripUid);
    const mark = trip && trip.marks.find((m) => m.uid === sel.markUid);
    return mark ? { trip, mark, idx: trip.marks.indexOf(mark) } : null;
}

/** Вибраний відрізок: {kind: "auto", seg} | {kind: "mark", trip, from, to} | null. */
function selectedSegment() {
    if (sel && sel.kind === "auto") {
        const seg = autoByKey.get(sel.key);
        return seg ? { kind: "auto", seg } : null;
    }
    const s = selectedMark();
    if (!s) return null;
    return tripSegments(s.trip).find((x) => x.from === s.mark) || null;
}

function segCount(seg) {
    return seg.kind === "auto" ? seg.seg.count : seg.from.count_after;
}

const isFilled = (seg) => segCount(seg) !== null && segCount(seg) !== undefined;
const autoSel = (seg) => ({ kind: "auto", seg });

// ── вікно плеєра ─────────────────────────────────────────────────────

function playerWindowOpts(playUtc) {
    return {
        preserveView: true,
        playheadUtc: playUtc,
        anchorUtc: playUtc,
        anchorEdge: "left",
    };
}

function applyWindow(seekUtc = null) {
    const day = dayWindow();
    if (!day.start || !day.end) return;
    let start = day.start;
    let end = day.end;
    const seg = selectedSegment();
    if (seg && seg.kind === "auto") {
        start = seg.seg.period_start_utc;
        end = seg.seg.period_end_utc;
    } else if (seg) {
        start = seg.from.utc;
        end = seg.to ? seg.to.utc : windowOf(seg.trip).end_utc;
    } else if (sel && sel.kind === "window" && windowById.has(sel.uid)) {
        start = windowById.get(sel.uid).start_utc;
        end = windowById.get(sel.uid).end_utc;
    }
    if (end - start < 1) end = start + 1;
    const playUtc = seekUtc !== null ? seekUtc : (player.winStart + player.master);
    player.setWindow(start, end, playerWindowOpts(playUtc));
}

function showWholeDay() {
    syncCountInput();
    sel = null;
    const day = dayWindow();
    if (day.start && day.end) player.setWindow(day.start, day.end);
    render();
}

/**
 * Вибір відрізка / відмітки / ділянки. Фокус на поле кількості не переводиться
 * (сторінка не стрибає вниз) — цифри ловить глобальний обробник.
 */
function select(next, { seekUtc = null } = {}) {
    syncCountInput();
    sel = next;
    externalDigitEntry = false;
    const s = selectedMark();
    if (s && !selectedSegment()) {
        // Кінцева відмітка — весь день, плеєр на ній.
        const day = dayWindow();
        if (day.start && day.end) player.setWindow(day.start, day.end, playerWindowOpts(s.mark.utc));
    } else if (sel && sel.kind === "window") {
        applyWindow(seekUtc ?? windowById.get(sel.uid)?.start_utc ?? null);
    } else {
        applyWindow(seekUtc);
    }
    render();
    const input = $("cmCount");
    if (document.activeElement === input) {
        if (input.disabled) input.blur();
        else input.select();
    }
}

const selectAuto = (key) => select({ kind: "auto", key });
const selectMark = (tripUid, markUid, opts) => select({ kind: "mark", tripUid, markUid }, opts);
const selectWindow = (uid) => select({ kind: "window", uid });

function isSelected(item) {
    if (!sel || !item) return false;
    if (item.kind === "auto") return sel.kind === "auto" && sel.key === item.seg.seg_key;
    return sel.kind === "mark" && sel.markUid === item.from.uid;
}

// ── навігація по відрізках (Enter / Shift) ──────────────────────────

/** Усі відрізки дня за часом: з треку і з ділянок. */
function navList() {
    const list = visibleAutoSegs().map((seg) => ({ utc: seg.period_start_utc, item: autoSel(seg) }));
    for (const trip of trips) {
        for (const seg of tripSegments(trip)) list.push({ utc: seg.from.utc, item: seg });
    }
    return list.sort((a, b) => a.utc - b.utc);
}

function navStep(dir) {
    syncCountInput();
    const list = navList();
    if (!list.length) return;
    const pos = list.findIndex((x) => isSelected(x.item));
    let next;
    if (pos >= 0) {
        next = pos + dir;
    } else {
        // Вибрано кінець ходки чи ділянку — крокуємо від їхнього часу.
        const s = selectedMark();
        const w = sel && sel.kind === "window" ? windowById.get(sel.uid) : null;
        const utc = s ? s.mark.utc : (w ? w.start_utc : null);
        if (utc === null) {
            next = dir > 0 ? 0 : list.length - 1;
        } else if (dir > 0) {
            next = list.findIndex((x) => x.utc >= utc);
            if (next < 0) next = list.length;
        } else {
            next = list.map((x) => x.utc < utc).lastIndexOf(true);
        }
    }
    if (next < 0) return;
    if (next >= list.length) {
        renderSidebar();
        $("ccSaveState").textContent = "Це останній відрізок.";
        $("ccSave")?.focus();
        return;
    }
    const item = list[next].item;
    if (item.kind === "auto") selectAuto(item.seg.seg_key);
    else selectMark(item.trip.uid, item.from.uid);
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

function makeMark(utc) {
    return { uid: newUid(), utc, count_after: null };
}

function startTrip(w, routeId, stopId, utc) {
    if (!routeById.has(routeId)) return fail("Оберіть маршрут.");
    if (!hasStop(routeId, stopId)) return fail("Оберіть початкову зупинку маршруту.");
    if (!(utc > w.start_utc && utc < w.end_utc)) return fail("Час поза ділянкою ручної розмітки.");
    if (tripsIn(w).some((t) => !t.closed)) return fail("Спочатку завершіть поточну ходку ділянки.");
    if (tripCovering(w, utc)) return fail("Поточний час усередині іншої ходки.");
    const mark = makeMark(utc);
    const trip = {
        uid: newUid(), window_uid: w.uid, from_left: false, route_id: routeId, route_name: routeName(routeId),
        closed: false, start_stop_id: stopId, end_stop_id: null, marks: [mark],
    };
    trips.push(trip);
    changed();
    selectMark(trip.uid, mark.uid);
    return trip;
}

function addStop(trip, utc) {
    if (trip.marks.some((m) => m.utc === utc)) return fail("На цей час уже є відмітка.");
    if (utc <= windowOf(trip).start_utc) return fail("Час поза ділянкою ручної розмітки.");
    if (utc >= tripLimit(trip)) return fail("Час заходить на наступну ходку або за межі ділянки.");
    const mark = makeMark(utc);
    trip.marks.push(mark);
    changed();
    selectMark(trip.uid, mark.uid);
    return mark;
}

function finishTrip(trip, stopId, utc) {
    if (trip.closed) return fail("Ходку вже завершено.");
    if (utc <= tripEnd(trip)) return fail("Кінець маршруту має бути пізніше останньої зупинки.");
    if (utc >= tripLimit(trip)) return fail("Час заходить на наступну ходку або за межі ділянки.");
    if (!hasStop(trip.route_id, stopId)) return fail("Оберіть кінцеву зупинку.");
    const startPos = stopPos(trip.route_id, trip.start_stop_id);
    if (startPos !== undefined && stopPos(trip.route_id, stopId) <= startPos) {
        return fail("Кінцева зупинка має бути далі за маршрутом, ніж початкова.");
    }
    syncCountInput();
    const mark = makeMark(utc);
    trip.marks.push(mark);
    trip.closed = true;
    trip.end_stop_id = stopId;
    sel = null;
    changed();
    applyWindow();
    render();
    return { trip, mark };
}

function reverseTrip(trip, stopId, utc) {
    const inverse = inverseOf(trip.route_id);
    if (!inverse) return fail("Для цього маршруту не вказано зворотній.");
    const firstStop = firstStopId(inverse.id);
    if (firstStop === null) return fail("У зворотного маршруту немає зупинок.");
    const done = finishTrip(trip, stopId, utc);
    if (!done) return null;
    return startTrip(windowOf(trip), inverse.id, firstStop, done.mark.utc);
}

function reopenTrip(trip) {
    if (tripsIn(windowOf(trip)).some((t) => !t.closed)) return fail("Спочатку завершіть поточну ходку ділянки.");
    trip.closed = false;
    trip.end_stop_id = null;
    changed();
    const last = trip.marks[trip.marks.length - 1];
    if (last) selectMark(trip.uid, last.uid);
    return trip;
}

/** Видалення ходки; продовження з треку не видаляється, а скидається до якоря. */
function deleteTrip(trip) {
    const name = routeName(trip.route_id, trip.route_name);
    if (trip.from_left) {
        if (!window.confirm(`Скинути розмітку продовження ходки «${name}» (зупинки і кінець)?`)) return;
        trip.marks = trip.marks.filter((m) => m.anchor);
        trip.closed = false;
        trip.end_stop_id = null;
        if (sel && sel.kind === "mark" && sel.tripUid === trip.uid) sel = null;
    } else {
        if (!window.confirm(`Видалити ходку «${name}» з усіма зупинками?`)) return;
        trips = trips.filter((t) => t !== trip);
        if (sel && sel.kind === "mark" && sel.tripUid === trip.uid) sel = null;
    }
    changed();
    applyWindow();
}

function deleteSelectedMark() {
    const s = selectedMark();
    if (!s) return;
    if (s.mark.anchor) {
        fail("Це кінець треку — його не можна видалити.");
        return;
    }
    const label = markLabels(s.trip)[s.idx];
    if (!window.confirm(`Видалити відмітку «${label}» (${clock(s.mark.utc)})?`)) return;
    const wasLast = s.idx === s.trip.marks.length - 1;
    s.trip.marks.splice(s.idx, 1);
    if (!s.trip.marks.length) {
        trips = trips.filter((t) => t !== s.trip);
        sel = null;
    } else {
        if (wasLast && s.trip.closed && !tripsIn(windowOf(s.trip)).some((t) => !t.closed)) {
            s.trip.closed = false;
            s.trip.end_stop_id = null;
        }
        const prev = s.trip.marks[Math.max(0, s.idx - 1)];
        sel = { kind: "mark", tripUid: s.trip.uid, markUid: prev.uid };
    }
    changed();
    applyWindow();
}

function setSelectedTime() {
    const s = selectedMark();
    if (!s || s.mark.anchor) return;
    const utc = nowUtc();
    const w = windowOf(s.trip);
    if (!(utc > w.start_utc && utc < w.end_utc)) {
        fail("Час поза ділянкою ручної розмітки.");
        return;
    }
    if (s.trip.marks.some((m) => m !== s.mark && m.utc === utc)) {
        fail("На цей час уже є відмітка цієї ходки.");
        return;
    }
    s.mark.utc = utc;
    changed();
    applyWindow(utc);
}

/** Початкова / кінцева зупинка ходки (лише для першої та кінцевої відмітки). */
function setTripStop(trip, field, stopId) {
    if (!stopId || (trip.from_left && field === "start_stop_id")) return;
    trip[field] = stopId;
    changed();
}

/** Інший маршрут ходки: зупинки, яких у ньому немає, — на його першу / останню. */
function setTripRoute(trip, routeId) {
    if (trip.from_left || !routeById.has(routeId) || routeId === trip.route_id) return;
    trip.route_id = routeId;
    trip.route_name = routeName(routeId);
    if (!hasStop(routeId, trip.start_stop_id)) trip.start_stop_id = firstStopId(routeId);
    if (trip.closed && !hasStop(routeId, trip.end_stop_id)) trip.end_stop_id = lastStopId(routeId);
    changed();
}

// ── форма «+»: відмітка на поточному часі ───────────────────────────

/**
 * Що можна відмітити на utc (лише в ділянці без треку):
 *  new      — початок маршруту (поза ходками);
 *  continue — зупинка або кінець відкритої ходки (після її останньої відмітки);
 *  insert   — проміжна зупинка всередині ходки;
 *  error    — нічого.
 */
function composerContext(utc) {
    const limits = dataLimits();
    if (!limits || utc < limits.start || utc > limits.end) {
        return { kind: "error", message: "Тут немає ні відео, ні треку — розмічати нічого." };
    }
    const w = windowAt(utc);
    if (!w) {
        return {
            kind: "error",
            message: "Тут є трек — відрізки вже визначено автоматично. "
                + "Вручну розмічаються лише ділянки без треку (виділені фіолетовим на графіку).",
        };
    }
    const covering = tripCovering(w, utc);
    if (covering) {
        if (covering.marks.some((m) => m.utc === utc)) {
            return { kind: "error", message: "На цей час уже є відмітка. Зсуньте відео." };
        }
        if (!covering.closed && utc > tripEnd(covering)) return { kind: "continue", window: w, trip: covering };
        return { kind: "insert", window: w, trip: covering };
    }
    const open = tripsIn(w).find((t) => !t.closed);
    if (open) {
        return {
            kind: "error",
            message: `Спочатку завершіть ходку «${routeName(open.route_id, open.route_name)}» `
                + `(вона почалася о ${clock(tripStart(open))}).`,
        };
    }
    return { kind: "new", window: w, after: lastClosedBefore(w, utc) };
}

function contextKey(ctx) {
    return [ctx.kind, ctx.window ? ctx.window.uid : "", ctx.trip ? ctx.trip.uid : "",
        ctx.trip ? ctx.trip.marks.length : "", ctx.after ? ctx.after.uid : "", ctx.message || ""].join(":");
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

/** Маршрути нової ходки: спершу зворотній до попередньої і той, яким машина їде після ділянки. */
function routeOptionsHtml(ctx) {
    const preferred = [];
    const prevRouteId = ctx.after ? ctx.after.route_id
        : (ctx.window.left && !ctx.window.left.open ? ctx.window.left.route_id : null);
    const inverse = prevRouteId !== null ? inverseOf(prevRouteId) : null;
    if (inverse && !inverse.unbound) preferred.push([inverse, `↩ Зворотній: ${inverse.name}`]);
    const right = ctx.window.right && routeById.get(ctx.window.right.route_id);
    if (right && !right.unbound && !preferred.some(([r]) => r.id === right.id)) {
        preferred.push([right, `→ Як після ділянки: ${right.name}`]);
    }
    if (!preferred.length) return routes.map((r) => optionHtml(r)).join("");
    const rest = routes.filter((r) => !preferred.some(([p]) => p.id === r.id));
    return preferred.map(([r, label]) => optionHtml(r, label)).join("")
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

    const tripName = routeName(ctx.trip?.route_id, ctx.trip?.route_name);
    const titles = {
        new: "Початок маршруту",
        continue: `${tripName}: зупинка або кінець`,
        insert: `${tripName}: проміжна зупинка`,
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

    const now = () => nowUtc();
    const done = (res) => { if (res) closeComposer(); };
    const actions = (...buttons) => {
        const row = document.createElement("div");
        row.className = "cm-composer-actions";
        row.append(...buttons);
        return row;
    };
    let primary;

    if (ctx.kind === "new") {
        const routeSel = document.createElement("select");
        routeSel.setAttribute("aria-label", "Маршрут");
        routeSel.innerHTML = routeOptionsHtml(ctx);
        const stopSel = stopSelect(Number(routeSel.value), null, "Початкова зупинка");
        routeSel.addEventListener("change", () => {
            fillStopSelect(stopSel, Number(routeSel.value), firstStopId(Number(routeSel.value)));
        });
        primary = composerButton("Почати маршрут", "Enter",
            () => done(startTrip(ctx.window, Number(routeSel.value), Number(stopSel.value), now())));
        box.append(routeSel, labeled("Початкова зупинка", stopSel), actions(primary));
        if (focus) routeSel.focus();
    } else if (ctx.kind === "insert") {
        primary = composerButton("Додати зупинку", "Enter",
            () => done(addStop(ctx.trip, now())));
        box.append(hint("Зупинку програма визначить за часом."), actions(primary));
        if (focus) primary.focus();
    } else {
        primary = composerButton("Зупинка", "Enter — проміжна зупинка (визначиться за часом)",
            () => done(addStop(ctx.trip, now())));
        const endSel = stopSelect(ctx.trip.route_id, lastStopId(ctx.trip.route_id), "Кінцева зупинка");
        const endButtons = [composerButton("Кінець маршруту", "Завершити ходку на вибраній кінцевій зупинці",
            () => done(finishTrip(ctx.trip, Number(endSel.value), now())))];
        const inverse = inverseOf(ctx.trip.route_id);
        if (inverse) {
            endButtons.push(composerButton("Кінець + зворотній", `Завершити і одразу почати «${inverse.name}»`,
                () => done(reverseTrip(ctx.trip, Number(endSel.value), now()))));
        }
        box.append(actions(primary), labeled("Кінцева зупинка", endSel), actions(...endButtons));
        if (ctx.window.right && ctx.window.right.route_id === ctx.trip.route_id && isLastIn(ctx.trip)) {
            box.append(hint("Якщо ходка триває і після ділянки — не завершуйте її, вона продовжиться за треком."));
        }
        if (focus) primary.focus();
    }
    primary.classList.add("btn-primary");
    composer.primary = primary;
    box.appendChild(msg);
}

function fillStopSelect(select, routeId, value) {
    select.innerHTML = stopsOf(routeId).map((s) => optionHtml(s)).join("");
    if (value !== null && value !== undefined) select.value = String(value);
}

function stopSelect(routeId, value, label) {
    const select = document.createElement("select");
    select.setAttribute("aria-label", label);
    fillStopSelect(select, routeId, value ?? firstStopId(routeId));
    return select;
}

function labeled(text, control) {
    const label = document.createElement("label");
    label.className = "cm-composer-field";
    const span = document.createElement("span");
    span.textContent = text;
    label.append(span, control);
    return label;
}

function hint(text) {
    const el = document.createElement("div");
    el.className = "cm-composer-hint";
    el.textContent = text;
    return el;
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
    const n = parseCount(input.value);
    if (seg.kind === "auto") seg.seg.count = n;
    else seg.from.count_after = n;
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

function segButton(item, fromLabel, toLabel, t0, t1, onClick) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "cc-seg";
    if (isSelected(item)) btn.classList.add("is-active");
    if (isFilled(item)) btn.classList.add("is-filled");
    if (item.kind === "mark" && !item.to) btn.classList.add("is-open");
    const count = isFilled(item) ? String(segCount(item)) : "—";
    btn.innerHTML =
        `<span class="cc-seg-stops">${escapeHtml(fromLabel)} – ${escapeHtml(toLabel)}</span>`
        + `<span class="cc-seg-row">`
        + `<span class="cc-seg-time">${clockHm(t0)} – ${t1 === null ? "…" : clockHm(t1)}</span>`
        + `<span class="cc-seg-count">${count}</span>`
        + `</span>`;
    btn.addEventListener("click", onClick);
    return btn;
}

/**
 * Правка вибраної відмітки: час (з плеєра), видалення; для першої / кінцевої —
 * ще й зупинка ходки. Якір продовження з треку не змінюється (null).
 */
function markEditRow(trip, idx) {
    const mark = trip.marks[idx];
    if (mark.anchor) return null;
    const row = document.createElement("div");
    row.className = "cm-edit";
    const isFirst = idx === 0;
    const isEnd = trip.closed && idx === trip.marks.length - 1;
    if (isFirst) {
        const routeSel = document.createElement("select");
        routeSel.className = "cm-edit-route";
        routeSel.title = "Маршрут ходки";
        routeSel.setAttribute("aria-label", "Маршрут ходки");
        const choices = isUnbound(trip.route_id) ? [routeById.get(trip.route_id), ...routes] : routes;
        routeSel.innerHTML = choices
            .map((r) => optionHtml(r, r.unbound ? `⚠ ${r.name} (відв'язаний)` : r.name))
            .join("");
        routeSel.value = String(trip.route_id);
        routeSel.addEventListener("change", () => setTripRoute(trip, Number(routeSel.value)));
        row.appendChild(routeSel);
    }
    if (isFirst || isEnd) {
        const field = isFirst ? "start_stop_id" : "end_stop_id";
        const label = isFirst ? "Початкова зупинка ходки" : "Кінцева зупинка ходки";
        const stopSel = stopSelect(trip.route_id, trip[field], label);
        stopSel.title = label;
        stopSel.addEventListener("change", () => setTripStop(trip, field, Number(stopSel.value)));
        row.appendChild(stopSel);
    } else {
        const note = document.createElement("span");
        note.className = "cm-edit-note";
        note.textContent = trip.closed || continuesIntoRight(trip)
            ? "зупинка — за часом"
            : "зупинка — після завершення ходки";
        row.appendChild(note);
    }
    row.append(
        iconButton("⏱", "Перенести відмітку на поточний час відео", setSelectedTime),
        iconButton("✕", "Видалити цю відмітку", deleteSelectedMark, true),
    );
    return row;
}

function blockEl(block, visible) {
    const segs = visible.filter((s) => s.block_uid === block.uid);
    // Проїзд з однієї зупинки (розворот на кінцевій) не дає відрізків.
    // Заголовок «0/0» поряд із наступним рейсом того ж маршруту лише плутає.
    if (!segs.length) return null;
    const el = document.createElement("div");
    el.className = "cc-trip ch-block";
    const head = document.createElement("div");
    head.className = "cc-trip-head";
    const done = segs.filter((s) => isFilled(autoSel(s))).length;
    head.innerHTML =
        `<span class="cc-trip-name">${escapeHtml(routeName(block.route_id, block.route_name))}</span>`
        + `<span class="cc-trip-meta">${done}/${segs.length}</span>`;
    el.appendChild(head);
    for (const seg of segs) {
        const item = autoSel(seg);
        el.appendChild(segButton(item, seg.from_stop.name, seg.to_stop.name,
            seg.period_start_utc, seg.period_end_utc, () => selectAuto(seg.seg_key)));
    }
    return el;
}

function tripEl(trip, w) {
    const resolved = resolveTrip(trip);
    const segs = tripSegments(trip);
    const labels = markLabels(trip, resolved);
    const el = document.createElement("div");
    el.className = "cc-trip ch-manual-trip" + (trip.closed ? "" : " is-open");

    const head = document.createElement("div");
    head.className = "cc-trip-head";
    const closedSegs = segs.filter((s) => s.to);
    const done = closedSegs.filter(isFilled).length;
    let meta = `${done}/${closedSegs.length}`;
    let metaTitle = "";
    let warn = false;
    if (isUnbound(trip.route_id)) {
        meta = "⚠ маршрут";
        metaTitle = "Маршрут більше не прив'язаний до машини — змініть маршрут ходки (клік на перший відрізок)";
        warn = true;
    } else if (trip.closed && !resolved) {
        meta = "⚠ зупинки";
        metaTitle = "Кінцева зупинка має бути далі за маршрутом, ніж початкова";
        warn = true;
    }
    // Продовження ходки з треку: маршрут видно в блоці треку над ділянкою.
    if (!trip.from_left || warn) {
        head.innerHTML =
            `<span class="cc-trip-name">${trip.from_left ? "" : escapeHtml(routeName(trip.route_id, trip.route_name))}</span>`
            + `<span class="cc-trip-meta"${metaTitle ? ` title="${escapeHtml(metaTitle)}"` : ""}>${meta}</span>`;
    }
    if (!readOnly) {
        const tools = document.createElement("span");
        tools.className = "cm-trip-tools";
        const list = tripsIn(w);
        if (trip.closed && !list.some((t) => !t.closed) && list[list.length - 1] === trip) {
            tools.appendChild(iconButton("↺", "Продовжити ходку (зняти завершення)", () => reopenTrip(trip)));
        }
        if (!trip.from_left) {
            tools.appendChild(iconButton("✕", "Видалити ходку", () => deleteTrip(trip), true));
        } else if (!isBareLeft(trip)) {
            tools.appendChild(iconButton("✕", "Скинути зупинки і кінець продовження", () => deleteTrip(trip), true));
        }
        if (tools.childElementCount) head.appendChild(tools);
    }
    if (head.childElementCount) el.appendChild(head);

    for (const seg of segs) {
        const idx = trip.marks.indexOf(seg.from);
        const toLabel = seg.to ? labels[idx + 1] + (seg.to.virtual ? " (трек)" : "") : "…";
        el.appendChild(segButton(seg, labels[idx], toLabel, seg.from.utc, seg.to ? seg.to.utc : null,
            () => selectMark(trip.uid, seg.from.uid)));
        const edit = isSelected(seg) && !readOnly ? markEditRow(trip, idx) : null;
        if (edit) el.appendChild(edit);
    }

    if (trip.closed && trip.marks.length) {
        const lastIdx = trip.marks.length - 1;
        const last = trip.marks[lastIdx];
        const active = !!sel && sel.kind === "mark" && sel.markUid === last.uid;
        const end = document.createElement("button");
        end.type = "button";
        end.className = "cm-trip-end" + (active ? " is-active" : "");
        end.innerHTML = `<span>Кінець: ${escapeHtml(labels[lastIdx])}</span>`
            + `<span class="cc-seg-time">${clockHm(last.utc)}</span>`;
        end.addEventListener("click", () => selectMark(trip.uid, last.uid));
        el.appendChild(end);
        const edit = active && !readOnly ? markEditRow(trip, lastIdx) : null;
        if (edit) el.appendChild(edit);
    }
    return el;
}

function windowEl(w) {
    const status = windowStatus(w);
    const el = document.createElement("div");
    el.className = `ch-window is-${status.state}`
        + (sel && sel.kind === "window" && sel.uid === w.uid ? " is-selected" : "");

    const head = document.createElement("button");
    head.type = "button";
    head.className = "ch-window-head" + (sel && sel.kind === "window" && sel.uid === w.uid ? " is-active" : "");
    head.title = `${REASONS[w.reason] || w.reason} — показати ділянку у плеєрі`;
    head.innerHTML = `<span class="cc-seg-time">${clockHm(w.start_utc)} – ${clockHm(w.end_utc)}</span>`;
    head.addEventListener("click", () => selectWindow(w.uid));
    el.appendChild(head);

    if (status.text) {
        const st = document.createElement("div");
        st.className = "ch-window-status";
        st.textContent = status.text;
        el.appendChild(st);
    }

    for (const trip of tripsIn(w)) el.appendChild(tripEl(trip, w));
    return el;
}

function renderSidebar() {
    const body = $("ccSidebarBody");
    if (!body) return;
    const visible = visibleAutoSegs();
    const manual = trips.flatMap(tripSegments).filter((s) => s.to);
    const done = visible.filter((s) => isFilled(autoSel(s))).length + manual.filter(isFilled).length;
    $("ccSegProgress").textContent = `${done} / ${visible.length + manual.length}`;
    body.innerHTML = "";

    if (!blocks.length && !windows.length) {
        const empty = document.createElement("div");
        empty.className = "cc-sidebar-empty";
        empty.textContent = "За цей день немає ні треку, ні ділянок для розмітки.";
        body.appendChild(empty);
        return;
    }

    const items = [
        ...blocks.map((b) => ({ utc: b.start_utc, order: 1, el: () => blockEl(b, visible) })),
        ...windows.map((w) => ({ utc: w.start_utc, order: 0, el: () => windowEl(w) })),
    ].sort((a, b) => a.utc - b.utc || a.order - b.order);
    for (const item of items) {
        const node = item.el();
        if (node) body.appendChild(node);
    }

    // Прокручуємо лише список (scrollIntoView зсунув би й саму сторінку).
    const active = body.querySelector(".is-active");
    if (active) {
        const b = body.getBoundingClientRect();
        const a = active.getBoundingClientRect();
        if (a.top < b.top) body.scrollTop -= b.top - a.top;
        else if (a.bottom > b.bottom) body.scrollTop += a.bottom - b.bottom;
    }
}

function renderCount() {
    const seg = selectedSegment();
    const input = $("cmCount");
    input.disabled = readOnly || !seg;
    input.value = seg && isFilled(seg) ? String(segCount(seg)) : "";
    // Підсвітка: цифри з клавіатури підуть сюди навіть без фокуса.
    input.classList.toggle("is-active-field", !input.disabled);
}

// ── графік: ділянки, ходки і зупинки ─────────────────────────────────

function drawOverlay(ctx, p) {
    const h = p.SPEED_H;
    const now = nowUtc();
    for (const w of windows) {
        const x1 = p.xOf(w.start_utc);
        const x2 = p.xOf(w.end_utc);
        ctx.fillStyle = "rgba(168, 85, 247, 0.10)";
        ctx.fillRect(x1, 0, Math.max(1, x2 - x1), h);
    }
    for (const trip of trips) {
        const own = operatorMarks(trip);
        const w = windowOf(trip);
        const continues = continuesIntoRight(trip);
        if (isBareLeft(trip) && !continues) continue;
        const end = trip.closed ? tripEnd(trip)
            : (continues ? w.right.utc : Math.min(w.end_utc, Math.max(tripEnd(trip), now)));
        const x1 = p.xOf(tripStart(trip));
        const x2 = p.xOf(end);
        ctx.fillStyle = trip.closed || continues ? "rgba(245, 158, 11, 0.12)" : "rgba(56, 189, 248, 0.12)";
        ctx.fillRect(x1, 0, Math.max(1, x2 - x1), h);
        for (const m of own) {
            const x = p.xOf(m.utc);
            const active = sel && sel.kind === "mark" && sel.markUid === m.uid;
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
        window_uid: t.window_uid,
        from_left: t.from_left,
        route_id: t.route_id,
        route_name: routeName(t.route_id, t.route_name),
        closed: t.closed,
        start_stop_id: t.start_stop_id,
        end_stop_id: t.closed ? t.end_stop_id : null,
        marks: t.marks.map((m) => ({ ...m })),
    }));
}

function countsPayload() {
    syncCountInput();
    return Object.fromEntries(autoSegs.map((s) => [s.seg_key, s.count ?? null]));
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
        return { ...payloadBase(pendingSec), counts: countsPayload(), chronology: chronologyPayload() };
    },
});

function selectError(err) {
    if (!err) return;
    if (err.seg_key) {
        if (autoByKey.has(err.seg_key)) {
            selectAuto(err.seg_key);
            return;
        }
        const m = /^m:([^:]+):/.exec(err.seg_key);
        const trip = m && trips.find((t) => t.marks.some((x) => x.uid === m[1]));
        if (trip) {
            selectMark(trip.uid, m[1]);
            return;
        }
    }
    const trip = err.trip_uid && findTrip(err.trip_uid);
    if (trip && trip.marks.length) {
        const mark = (err.mark_uid && trip.marks.find((x) => x.uid === err.mark_uid)) || trip.marks[0];
        selectMark(trip.uid, mark.uid);
        return;
    }
    if (err.window_uid && windowById.has(err.window_uid)) selectWindow(err.window_uid);
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
                counts: countsPayload(),
                chronology: chronologyPayload(),
            }),
        });
        const j = await resp.json();
        if (!resp.ok || !j.ok) {
            if (j.error === "invalid_hybrid" && (j.errors || []).length) {
                selectError(j.errors[0]);
                const more = j.errors.length > 1 ? ` (ще помилок: ${j.errors.length - 1})` : "";
                throw new Error(j.errors[0].message + more);
            }
            if (j.error === "count_too_high") throw new Error(`більше людей, ніж максимум (${j.max}).`);
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
    if (el === $("cmCount") || el.type === "range") return true;
    if ($("cmComposer")?.contains(el)) return false;
    if (["INPUT", "SELECT", "TEXTAREA"].includes(el.tagName) || el.isContentEditable) return false;
    // Кнопки-картки відрізків — так; інші кнопки (Зберегти, ✕ …) обробляють Enter самі.
    return el.tagName !== "BUTTON" || el.matches(".cc-seg, .cm-trip-end, .ch-window-head");
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
    const first = navList().find((x) => !isFilled(x.item));
    if (first && first.item.kind === "auto") selectAuto(first.item.seg.seg_key);
    else if (first) selectMark(first.item.trip.uid, first.item.from.uid);
    else {
        applyWindow();
        render();
    }
    if (!readOnly) setInterval(tickComposer, TICK_MS);
})();
