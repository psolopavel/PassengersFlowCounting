/**
 * Платформа міського підрахунку — обгортка над MultiCamPlayer:
 * одне вікно дня, одне число (in_salon_total), фінальне збереження.
 */
import { MultiCamPlayer } from "./count_core.js";

const cfg = JSON.parse(document.getElementById("ccConfig").textContent);
const $ = (id) => document.getElementById(id);

function readTotalInput() {
    const raw = $("ccTotal").value.trim().replace(",", ".");
    if (raw === "" || raw === ".") return null;
    const n = parseFloat(raw);
    return Number.isNaN(n) || n < 0 ? null : n;
}

const player = new MultiCamPlayer({
    cameras: cfg.cameras,
    autosaveUrl: cfg.autosaveUrl,
    sourcePriority: cfg.sourcePriority,
    buildPayload(pendingSec) {
        return {
            day: cfg.dayIso,
            counter_id: cfg.counterId,
            total: readTotalInput(),
            add_time_sec: pendingSec,
        };
    },
});

async function save() {
    const total = readTotalInput();
    if (total === null) {
        $("ccSaveState").textContent = "Вкажіть коректне число.";
        $("ccTotal").focus();
        return;
    }
    $("ccSave").disabled = true;
    $("ccSaveState").textContent = "Збереження…";
    try {
        await player.flushAutosave(false);
        const resp = await fetch(cfg.saveUrl, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                day: cfg.dayIso,
                counter_id: cfg.counterId,
                total,
                add_time_sec: player.takePendingTimeSec(),
            }),
        });
        const j = await resp.json();
        if (!resp.ok || !j.ok) throw new Error(j.error || resp.status);
        $("ccSaveState").textContent = "Збережено ✓ Закриваємо…";
        setTimeout(() => window.close(), 400);
        setTimeout(() => {
            $("ccSaveState").innerHTML = 'Збережено ✓ <a href="/panel/?day=' + cfg.dayIso + '">До списку</a>';
        }, 900);
    } catch (e) {
        $("ccSaveState").textContent = "Помилка збереження: " + (e.message || e);
        $("ccSave").disabled = false;
    }
}

player.bindControls();
$("ccSave").addEventListener("click", save);

(async () => {
    const ok = await player.loadMedia(cfg.mediaUrl);
    if (!ok) return;
    const w = player.media.window || cfg.window || {};
    if (w.start_utc && w.end_utc) player.setWindow(w.start_utc, w.end_utc);
    player.mount();
})();
