/**
 * Платформа міжміського / final_stop підрахунку — MultiCamPlayer + навігатор відрізків.
 */
import { MultiCamPlayer, clockHm } from "./count_core.js";

const cfg = JSON.parse(document.getElementById("ccConfig").textContent);
const $ = (id) => document.getElementById(id);

const isFinalStop = cfg.kind === "final_stop";
const segments = (cfg.segments || []).map((s) => ({ ...s }));
const maxPeople = Number(cfg.maxPeopleCountInCar) || 100;
let current = -1;
let externalDigitEntry = false;
let activeCountField = "entered";

function clampCount(n, max = null) {
    if (n === null || Number.isNaN(n)) return null;
    if (n < 0) return null;
    if (max !== null && n > max) return max;
    return n;
}

function sanitizeDecimalInput(raw) {
    let v = String(raw ?? "").trim().replace(",", ".");
    v = v.replace(/[^\d.]/g, "");
    const dot = v.indexOf(".");
    if (dot >= 0) {
        v = v.slice(0, dot + 1) + v.slice(dot + 1).replace(/\./g, "");
    }
    return v;
}

function parseCount(raw, max = null) {
    const v = sanitizeDecimalInput(raw);
    if (v === "" || v === ".") return null;
    const n = parseFloat(v);
    return clampCount(Number.isNaN(n) ? null : n, max);
}

function normalizeTotalInput(raw, max = null) {
    const v = sanitizeDecimalInput(raw);
    if (v === "" || v === ".") return v;
    const n = parseCount(v, max);
    return n === null ? "" : String(n);
}

function segmentFilled(seg) {
    if (isFinalStop) {
        return seg.entered !== null && seg.entered !== undefined
            && seg.exited !== null && seg.exited !== undefined;
    }
    return seg.count !== null && seg.count !== undefined;
}

function readInput() {
    if (isFinalStop) {
        return {
            entered: parseCount($("ccEntered").value),
            exited: parseCount($("ccExited").value),
        };
    }
    return parseCount($("ccTotal").value, maxPeople);
}

function countsMap() {
    const out = {};
    for (const s of segments) {
        if (isFinalStop) {
            out[s.seg_key] = { entered: s.entered, exited: s.exited };
        } else {
            out[s.seg_key] = s.count;
        }
    }
    return out;
}

function filledCount() {
    return segments.filter((s) => segmentFilled(s)).length;
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
    buildPayload(pendingSec) {
        syncInputToSegment();
        return {
            ...payloadBase(pendingSec),
            counts: countsMap(),
        };
    },
});

function syncInputToSegment() {
    if (current < 0 || !segments[current]) return;
    if (isFinalStop) {
        const vals = readInput();
        segments[current].entered = vals.entered;
        segments[current].exited = vals.exited;
    } else {
        segments[current].count = readInput();
    }
}

function groupTrips() {
    const trips = [];
    const map = new Map();
    for (const seg of segments) {
        const key = isFinalStop
            ? String(seg.zone_id)
            : `${seg.route_id}:${seg.occurrence}`;
        if (!map.has(key)) {
            const trip = isFinalStop
                ? { key, zone_name: seg.zone_name, segs: [] }
                : {
                    key,
                    route_name: seg.route_name,
                    occurrence: seg.occurrence,
                    segs: [],
                };
            map.set(key, trip);
            trips.push(trip);
        }
        map.get(key).segs.push(seg);
    }
    return trips;
}

function renderSidebar() {
    const body = $("ccSidebarBody");
    const progress = $("ccSegProgress");
    if (progress) progress.textContent = `${filledCount()} / ${segments.length}`;
    if (!body) return;
    body.innerHTML = "";

    if (!segments.length) {
        const empty = document.createElement("div");
        empty.className = "cc-sidebar-empty";
        empty.textContent = isFinalStop
            ? "Не вдалося визначити візити зон за треком цього дня."
            : "Не вдалося визначити відрізки за треком цього дня.";
        body.appendChild(empty);
        return;
    }

    const trips = groupTrips();
    for (const trip of trips) {
        const tripEl = document.createElement("div");
        tripEl.className = "cc-trip";
        const done = trip.segs.filter((s) => segmentFilled(s)).length;
        const head = document.createElement("div");
        head.className = "cc-trip-head";
        if (isFinalStop) {
            head.innerHTML = `<span class="cc-trip-name">${escapeHtml(trip.zone_name)}</span>`
                + `<span class="cc-trip-meta">${done}/${trip.segs.length}</span>`;
        } else {
            head.innerHTML = `<span class="cc-trip-name">${escapeHtml(trip.route_name)} · ходка #${trip.occurrence}</span>`
                + `<span class="cc-trip-meta">${done}/${trip.segs.length}</span>`;
        }
        tripEl.appendChild(head);

        for (const seg of trip.segs) {
            const idx = segments.indexOf(seg);
            const btn = document.createElement("button");
            btn.type = "button";
            btn.className = "cc-seg";
            if (idx === current) btn.classList.add("is-active");
            if (segmentFilled(seg)) btn.classList.add("is-filled");
            if (!isFinalStop && seg.gap_generated) btn.classList.add("is-gap-generated");
            const t0 = clockHm(seg.period_start_utc);
            const t1 = clockHm(seg.period_end_utc);
            let countLabel;
            if (isFinalStop) {
                const en = (seg.entered !== null && seg.entered !== undefined) ? seg.entered : "—";
                const ex = (seg.exited !== null && seg.exited !== undefined) ? seg.exited : "—";
                countLabel = `${en} / ${ex}`;
                btn.innerHTML =
                    `<span class="cc-seg-stops">Візит #${seg.occurrence}</span>`
                    + `<span class="cc-seg-row">`
                    + `<span class="cc-seg-time">${t0} – ${t1}</span>`
                    + `<span class="cc-seg-count">${countLabel}</span>`
                    + `</span>`;
            } else {
                const from = (seg.from_stop && seg.from_stop.name) || "?";
                const to = (seg.to_stop && seg.to_stop.name) || "?";
                countLabel = (seg.count !== null && seg.count !== undefined) ? String(seg.count) : "—";
                const gapNote = seg.gap_generated
                    ? `<span class="cc-seg-gap-note">Участок згенеровано автоматично при заповненні розриву GPS</span>`
                    : "";
                btn.innerHTML =
                    `<span class="cc-seg-stops">${escapeHtml(from)} – ${escapeHtml(to)}</span>`
                    + gapNote
                    + `<span class="cc-seg-row">`
                    + `<span class="cc-seg-time">${t0} – ${t1}</span>`
                    + `<span class="cc-seg-count">${countLabel}</span>`
                    + `</span>`;
            }
            btn.addEventListener("click", () => selectSegment(idx));
            tripEl.appendChild(btn);
        }
        body.appendChild(tripEl);
    }

    const active = body.querySelector(".cc-seg.is-active");
    if (active) active.scrollIntoView({ block: "nearest" });
}

function escapeHtml(s) {
    return String(s)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
}

function countInputFocused() {
    const el = document.activeElement;
    return !!el && (el === $("ccEntered") || el === $("ccExited"));
}

function setActiveCountField(field, focus) {
    activeCountField = field;
    externalDigitEntry = false;
    $("ccEntered")?.classList.toggle("is-active-field", field === "entered");
    $("ccExited")?.classList.toggle("is-active-field", field === "exited");
    if (focus) {
        const input = activeCountInput();
        input.focus();
        input.select();
    }
}

function selectSegment(idx, field = "entered") {
    if (!segments.length) return;
    idx = Math.max(0, Math.min(segments.length - 1, idx));
    if (current >= 0 && current !== idx) syncInputToSegment();
    const keepFocus = isFinalStop && countInputFocused();
    current = idx;
    externalDigitEntry = false;
    const seg = segments[current];
    player.setWindow(seg.period_start_utc, seg.period_end_utc);
    if (isFinalStop) {
        $("ccEntered").value = (seg.entered !== null && seg.entered !== undefined) ? String(seg.entered) : "";
        $("ccExited").value = (seg.exited !== null && seg.exited !== undefined) ? String(seg.exited) : "";
        setActiveCountField(field, keepFocus);
    } else {
        $("ccTotal").value = (seg.count !== null && seg.count !== undefined) ? String(seg.count) : "";
    }
    const pos = navPos();
    $("ccPrev").disabled = pos <= 0;
    $("ccNext").disabled = pos >= segments.length - 1;
    renderSidebar();
    player.flushAutosave(false);
}

function activeCountInput() {
    if (!isFinalStop) return $("ccTotal");
    return activeCountField === "exited" ? $("ccExited") : $("ccEntered");
}

function shouldCaptureDigitsForCount() {
    const input = activeCountInput();
    if (!input || input.disabled) return false;
    const el = document.activeElement;
    if (el === input) return false;
    if (!el || el === document.body) return true;
    const tag = el.tagName;
    if (tag === "TEXTAREA" || tag === "SELECT") return false;
    if (el.isContentEditable) return false;
    if (tag === "INPUT") {
        const type = (el.type || "").toLowerCase();
        if (["text", "search", "tel", "password", "email", "url", "number"].includes(type)) {
            return false;
        }
    }
    return true;
}

function applyCountDigits(raw) {
    const input = activeCountInput();
    const max = isFinalStop ? null : maxPeople;
    input.value = normalizeTotalInput(raw, max);
    syncInputToSegment();
    renderSidebar();
}

function bindPassiveDigitInput() {
    if (isFinalStop) {
        $("ccEntered")?.addEventListener("focus", () => setActiveCountField("entered", false));
        $("ccExited")?.addEventListener("focus", () => setActiveCountField("exited", false));
    }

    const blurHandler = () => { externalDigitEntry = false; };
    $("ccTotal")?.addEventListener("blur", blurHandler);
    $("ccEntered")?.addEventListener("blur", blurHandler);
    $("ccExited")?.addEventListener("blur", blurHandler);

    window.addEventListener("keydown", (e) => {
        if (e.ctrlKey || e.metaKey || e.altKey || e.isComposing) return;
        if (!shouldCaptureDigitsForCount()) return;

        if (/^\d$/.test(e.key)) {
            e.preventDefault();
            const input = activeCountInput();
            const next = externalDigitEntry ? input.value + e.key : e.key;
            externalDigitEntry = true;
            applyCountDigits(next);
            return;
        }

        if (e.key === "." || e.key === ",") {
            e.preventDefault();
            const input = activeCountInput();
            const cur = externalDigitEntry ? input.value : "";
            if (cur.includes(".")) return;
            const next = cur === "" ? "0." : cur + ".";
            externalDigitEntry = true;
            applyCountDigits(next);
            return;
        }

        if (e.key === "Backspace" || e.key === "Delete") {
            e.preventDefault();
            const input = activeCountInput();
            if (!input.value) {
                externalDigitEntry = false;
                return;
            }
            const next = input.value.slice(0, -1);
            externalDigitEntry = next.length > 0;
            applyCountDigits(next);
        }
    });
}

// Порядок навігації. Для isFinalStop йдемо за візуальним порядком бічної
// панелі (згруповано за зонами, лінійно згори вниз), бо segments відсортовані
// за часом і машина стрибала б між зонами. Для решти — старий порядок за часом.
function navOrder() {
    if (!isFinalStop) return segments.map((_, i) => i);
    const order = [];
    for (const trip of groupTrips()) {
        for (const seg of trip.segs) order.push(segments.indexOf(seg));
    }
    return order;
}

function navPos() {
    return navOrder().indexOf(current);
}

function goNextSegment() {
    const order = navOrder();
    const pos = order.indexOf(current);
    if (pos >= 0 && pos < order.length - 1) selectSegment(order[pos + 1]);
    else {
        syncInputToSegment();
        renderSidebar();
        $("ccSaveState").textContent = isFinalStop ? "Це останній візит." : "Це останній відрізок.";
        $("ccSave").focus();
    }
}

function goPrevSegment(field = "entered") {
    const order = navOrder();
    const pos = order.indexOf(current);
    if (pos > 0) selectSegment(order[pos - 1], field);
}

// Enter, Ctrl+Enter або Shift (назад). Для final_stop крокуємо по полях:
// Увійшли → Вийшли → Увійшли наступного візиту (і назад).
function goNext() {
    if (isFinalStop && current >= 0 && activeCountField === "entered") {
        syncInputToSegment();
        renderSidebar();
        setActiveCountField("exited", countInputFocused());
        return;
    }
    goNextSegment();
}

function goPrev() {
    if (isFinalStop && current >= 0 && activeCountField === "exited") {
        syncInputToSegment();
        renderSidebar();
        setActiveCountField("entered", countInputFocused());
        return;
    }
    goPrevSegment(isFinalStop ? "exited" : "entered");
}

function onCountNavKeydown(e) {
    if (e.repeat) return;
    if (e.key === "Shift") {
        e.preventDefault();
        goPrev();
        return;
    }
    if (e.key !== "Enter") return;
    e.preventDefault();
    if (e.ctrlKey) goPrev();
    else goNext();
}

async function save() {
    syncInputToSegment();
    renderSidebar();
    const missing = segments.filter((s) => !segmentFilled(s)).length;
    if (!segments.length) {
        $("ccSaveState").textContent = isFinalStop
            ? "Немає візитів для збереження."
            : "Немає відрізків для збереження.";
        return;
    }
    let allowEmpty = false;
    if (missing) {
        if (isFinalStop) {
            const emptyFields = segments.reduce((n, s) =>
                n + (s.entered === null || s.entered === undefined ? 1 : 0)
                + (s.exited === null || s.exited === undefined ? 1 : 0), 0);
            allowEmpty = window.confirm(
                `Не заповнено полів: ${emptyFields} (візитів: ${missing}).\n\n`
                + "Пропустити їх і зберегти як порожні?",
            );
        }
        if (!allowEmpty) {
            $("ccSaveState").textContent = `Заповніть усі ${isFinalStop ? "візити" : "відрізки"} (лишилось ${missing}).`;
            const firstEmpty = segments.findIndex((s) => !segmentFilled(s));
            if (firstEmpty >= 0) {
                const s = segments[firstEmpty];
                const field = isFinalStop && s.entered !== null && s.entered !== undefined ? "exited" : "entered";
                selectSegment(firstEmpty, field);
            }
            return;
        }
    }
    if (!isFinalStop) {
        const tooHigh = segments.find((s) => s.count !== null && s.count > maxPeople);
        if (tooHigh) {
            $("ccSaveState").textContent = `Максимум ${maxPeople} людей у салоні.`;
            const idx = segments.indexOf(tooHigh);
            if (idx >= 0) selectSegment(idx);
            return;
        }
    }
    $("ccSave").disabled = true;
    $("ccSaveState").textContent = "Збереження…";
    try {
        await player.flushAutosave(false);
        const resp = await fetch(cfg.saveUrl, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                ...payloadBase(player.takePendingTimeSec()),
                counts: countsMap(),
                ...(allowEmpty ? { allow_empty: true } : {}),
            }),
        });
        const j = await resp.json();
        if (!resp.ok || !j.ok) {
            if (j.error === "incomplete") {
                throw new Error(`Заповніть усі ${isFinalStop ? "візити" : "відрізки"} (лишилось ${j.missing}).`);
            }
            if (j.error === "count_too_high") {
                throw new Error(`Максимум ${j.max || maxPeople} людей у салоні.`);
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
        $("ccSaveState").textContent = "Помилка збереження: " + (e.message || e);
        $("ccSave").disabled = false;
    }
}

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

function bindCountInputs() {
    if (isFinalStop) {
        const onChange = () => {
            syncInputToSegment();
            renderSidebar();
        };
        $("ccEntered")?.addEventListener("change", () => {
            $("ccEntered").value = normalizeTotalInput($("ccEntered").value);
            onChange();
        });
        $("ccExited")?.addEventListener("change", () => {
            $("ccExited").value = normalizeTotalInput($("ccExited").value);
            onChange();
        });
        $("ccEntered")?.addEventListener("keydown", onCountNavKeydown);
        $("ccExited")?.addEventListener("keydown", onCountNavKeydown);
        return;
    }
    $("ccTotal").addEventListener("change", () => {
        $("ccTotal").value = normalizeTotalInput($("ccTotal").value, maxPeople);
        syncInputToSegment();
        renderSidebar();
    });
    $("ccTotal").addEventListener("keydown", onCountNavKeydown);
}

player.bindControls();
$("ccSave").addEventListener("click", save);
$("ccPrev").addEventListener("click", () => goPrevSegment());
$("ccNext").addEventListener("click", goNextSegment);
bindCountInputs();
window.addEventListener("keydown", (e) => {
    if (e.repeat) return;
    if (["TEXTAREA"].includes(document.activeElement.tagName)) return;
    if (document.activeElement === $("ccTotal")) return;
    if (document.activeElement === $("ccEntered") || document.activeElement === $("ccExited")) return;
    if (e.key === "Shift") {
        goPrev();
        return;
    }
    if (e.key !== "Enter") return;
    e.preventDefault();
    if (e.ctrlKey) goPrev();
    else goNext();
});
bindSidebarToggle();
bindPassiveDigitInput();

(async () => {
    const ok = await player.loadMedia(cfg.mediaUrl);
    if (!ok && !player.media) return;
    player.mount();
    if (segments.length) {
        const firstEmpty = segments.findIndex((s) => !segmentFilled(s));
        selectSegment(firstEmpty >= 0 ? firstEmpty : 0);
    } else {
        $("ccPrev").disabled = true;
        $("ccNext").disabled = true;
        if (isFinalStop) {
            $("ccEntered").disabled = true;
            $("ccExited").disabled = true;
        } else {
            $("ccTotal").disabled = true;
        }
        renderSidebar();
    }
})();
