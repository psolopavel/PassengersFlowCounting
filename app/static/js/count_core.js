/**
 * Спільне ядро мультикамерного плеєра (місто + міжмісто).
 *
 * Підхід: ОДИН майстер-годинник (rAF) веде логічний час вікна; усі камери —
 * підлеглі. Медіа НЕ в data — тягнемо з /panel/count/{car}/media.
 *
 * Публічно: attachMedia / loadMedia / setWindow / play-pause-seek / autosave.
 */

const $ = (id) => document.getElementById(id);

export const LOCAL_OFFSET = 3 * 3600; // UTC+3
const RATES = [0.25, 0.5, 1, 2, 4, 8, 16, 24, 32, 64];
// Стеля швидкості, коли на екрані є відео: браузер не тягне playbackRate > 16×.
// Для «тільки фото» стеля знімається (аж до кінця RATES).
const VIDEO_MAX_RATE = 16;
const MAX_CAMS = 4;
const DRIFT_SEC = 2;
const PHOTO_MAX_GAP = 30;
// Попереднє завантаження фото: вікно наперед у секундах РЕАЛЬНОГО часу
// (на 64× це ~2 хв запису), але в межах [MIN, MAX] кадрів.
const PHOTO_AHEAD_SEC = 2;
const PHOTO_AHEAD_MIN = 3;
const PHOTO_AHEAD_MAX = 40;
const PHOTO_KEEP_BEHIND = 20;
// HTTP/1.1 дає браузеру 6 з'єднань на домен — на всі камери разом.
const PHOTO_PARALLEL = 3;
// Скільки максимум чекаємо одне фото, перш ніж пустити плеєр далі без нього.
const PHOTO_WAIT_MAX_MS = 5000;
// Завислий запит фото обриваємо, щоб він не тримав слот паралельності вічно.
const PHOTO_LOAD_TIMEOUT_MS = 15000;
const STALL_TIMEOUT_MS = 1500;
const END_EPS = 0.25;
const MIN_VIEW_SEC = 60;
const ZOOM_STEP = 1.25;
const AUTOSAVE_MS = 10000;
const ASPECT = { portrait: 9 / 16, landscape: 16 / 9 };

const clamp = (x, a, b) => Math.max(a, Math.min(b, x));

export function clock(utc) {
    const d = new Date((utc + LOCAL_OFFSET) * 1000);
    const h = String(d.getUTCHours()).padStart(2, "0");
    const m = String(d.getUTCMinutes()).padStart(2, "0");
    const s = String(d.getUTCSeconds()).padStart(2, "0");
    return `${h}:${m}:${s}`;
}

export function clockHm(utc) {
    const d = new Date((utc + LOCAL_OFFSET) * 1000);
    const h = String(d.getUTCHours()).padStart(2, "0");
    const m = String(d.getUTCMinutes()).padStart(2, "0");
    return `${h}:${m}`;
}

function camId(c) {
    return typeof c === "string" ? c : c.id;
}

function findChunk(videos, utc) {
    let lo = 0, hi = videos.length - 1, res = null;
    while (lo <= hi) {
        const mid = (lo + hi) >> 1;
        const v = videos[mid];
        if (v.chunk_start_utc <= utc) {
            if (utc < v.chunk_stop_utc) { res = v; break; }
            lo = mid + 1;
        } else {
            hi = mid - 1;
        }
    }
    return res;
}

// Індекс останнього фото з utc <= заданого (або -1).
function photoIndex(photos, utc) {
    let lo = 0, hi = photos.length - 1, res = -1;
    while (lo <= hi) {
        const mid = (lo + hi) >> 1;
        if (photos[mid].utc <= utc) { res = mid; lo = mid + 1; }
        else hi = mid - 1;
    }
    return res;
}

// Фото «відпрацьоване»: завантажене, з помилкою або пропущене за таймаутом.
const photoSettled = (e) => !!e && (e.state !== "loading" || e.skip);

function partitions(n) {
    const res = [];
    const rec = (rem, max, cur) => {
        if (rem === 0) { res.push(cur.slice()); return; }
        for (let k = Math.min(rem, max); k >= 1; k--) { cur.push(k); rec(rem - k, k, cur); cur.pop(); }
    };
    rec(n, n, []);
    return res;
}

function fit(a, b) { return Math.min(a, b) / Math.max(a, b); }

function computeLayout(camsSel, R) {
    const n = camsSel.length;
    if (n <= 1) return n ? [{ k: 1, ids: [camsSel[0].id] }] : [];
    const aspects = camsSel.map((c) => ASPECT[c.orient] || ASPECT.landscape);

    let best = null;
    for (const part of partitions(n)) {
        const slots = [];
        part.forEach((k, ci) => {
            const ca = (k * k * R) / n;
            for (let s = 0; s < k; s++) slots.push({ col: ci, ca });
        });
        const camOrder = camsSel.map((_, i) => i).sort((a, b) => aspects[a] - aspects[b]);
        const slotOrder = slots.map((_, i) => i).sort((a, b) => slots[a].ca - slots[b].ca);
        const assign = new Array(slots.length);
        let score = 0;
        camOrder.forEach((ci, pos) => {
            const si = slotOrder[pos];
            score += fit(aspects[ci], slots[si].ca);
            assign[si] = ci;
        });
        if (!best || score > best.score + 1e-9 ||
            (Math.abs(score - best.score) < 1e-9 && part.length < best.part.length)) {
            best = { score, part, slots, assign };
        }
    }

    const cols = best.part.map((k) => ({ k, ids: [] }));
    best.slots.forEach((s, idx) => { cols[s.col].ids.push(camsSel[best.assign[idx]].id); });
    cols.sort((a, b) => a.k - b.k);
    return cols;
}

export class MultiCamPlayer {
    /**
     * @param {object} opts
     * @param {Array} opts.cameras
     * @param {string} [opts.autosaveUrl]
     * @param {(pendingSec: number) => object} [opts.buildPayload]
     * @param {string} [opts.crossOrigin]
     */
    constructor(opts) {
        this.opts = opts || {};
        this.camOrient = new Map((this.opts.cameras || []).map((c) =>
            [camId(c), (typeof c === "object" && c.orientation) ? c.orientation : "landscape"]));

        this.media = null;
        this.winStart = 0;
        this.winEnd = 0;
        this.durSec = 1;
        this.viewStart = 0;
        this.viewEnd = 1;
        this.allCams = (this.opts.cameras || []).map(camId);
        this.selected = this.allCams.slice(0, MAX_CAMS);
        this.cams = [];
        this.master = 0;
        this.playing = false;
        this.holding = false;
        this.rateIdx = 2;
        this.lastTs = 0;
        this.raf = 0;
        this.visibleSince = 0;
        this.pendingTimeSec = 0;
        this.autosaveTimer = 0;
        this.autosaveBusy = false;
        this.loopStarted = false;

        this.canvas = $("ccCanvas");
        this.ctx = this.canvas ? this.canvas.getContext("2d") : null;
        this.staticLayer = null;
        this.cssW = 0;
        this.cssH = 0;
        this.dpr = 1;
        this.SPEED_H = 90;
        this.LANE_H = 22;
        this.LANE_GAP = 4;
    }

    // Чи показує хоч одна камера відео в поточному кадрі.
    hasVideoOnScreen() {
        return this.cams.some((c) => c.els.badge.dataset.kind === "video");
    }

    // Максимально дозволений індекс швидкості для поточного кадру.
    maxRateIdx() {
        return this.hasVideoOnScreen() ? RATES.indexOf(VIDEO_MAX_RATE) : RATES.length - 1;
    }

    // Ефективна швидкість = обране значення, прикручене стелею поточного режиму.
    // Обране this.rateIdx НЕ змінюється: як тільки відео зникає з екрана,
    // швидкість автоматично повертається до значення, обраного на фото.
    rate() { return RATES[Math.min(this.rateIdx, this.maxRateIdx())]; }

    // ── медіа / вікно ────────────────────────────────────────────────
    attachMedia(media) {
        this.media = media;
    }

    async loadMedia(url) {
        const overlay = $("ccOverlay");
        try {
            const resp = await fetch(url);
            this.media = await resp.json();
        } catch (e) {
            if (overlay) overlay.textContent = "Не вдалося завантажити медіа: " + (e.message || e);
            return false;
        }

        const w = this.media.window || {};
        if (w.start_utc && w.end_utc && !this.winEnd) {
            this.setWindow(w.start_utc, w.end_utc);
        }

        const warn = (this.media.warnings || []).length
            ? " · попередження: " + this.media.warnings.length : "";
        if (overlay) {
            if (this.media.media_status === "no_media") {
                overlay.textContent = "Медіа за цей день не знайдено" + warn;
            } else {
                overlay.style.display = "none";
            }
        }
        return true;
    }

    setWindow(startUtc, endUtc) {
        this.winStart = startUtc;
        this.winEnd = endUtc;
        this.durSec = Math.max(1, endUtc - startUtc);
        this.viewStart = startUtc;
        this.viewEnd = endUtc;
        this.pause();
        this.master = 0;
        this.lastTs = 0;
        const durEl = $("ccDur");
        if (durEl) durEl.textContent = clock(endUtc);
        const seek = $("ccSeek");
        if (seek) seek.max = String(Math.floor(this.durSec));
        if (this.media) {
            this.syncAll(true);
            this.updateTimeUI();
            this.resizeCanvas();
        }
    }

    mount() {
        this.buildCameraChooser();
        this.mountGrid();
        this.resizeCanvas();
        this.lastTs = 0;
        this.startLoop();
        this.startAutosave();
    }

    startLoop() {
        if (this.loopStarted) return;
        this.loopStarted = true;
        this.raf = requestAnimationFrame((ts) => this.loop(ts));
    }

    // ── камери ───────────────────────────────────────────────────────
    buildCameraChooser() {
        const box = $("ccCams");
        if (!box) return;
        box.querySelectorAll(".cc-cam-opt").forEach((n) => n.remove());
        this.allCams.forEach((id) => {
            const label = document.createElement("label");
            label.className = "cc-cam-opt";
            const cb = document.createElement("input");
            cb.type = "checkbox";
            cb.value = id;
            cb.checked = this.selected.includes(id);
            cb.addEventListener("change", () => this.onToggleCamera(id, cb));
            label.appendChild(cb);
            label.appendChild(document.createTextNode(id));
            box.appendChild(label);
        });
    }

    onToggleCamera(id, cb) {
        if (cb.checked) {
            if (this.selected.length >= MAX_CAMS) {
                cb.checked = false;
                return;
            }
            this.selected.push(id);
        } else {
            this.selected = this.selected.filter((c) => c !== id);
        }
        this.mountGrid();
        this.resizeCanvas();
    }

    buildCell(cid) {
        const block = ((this.media && this.media.day_media) || {})[cid] || { videos: [], photos: [] };
        const cell = document.createElement("div");
        cell.className = "cc-cam";

        const video = document.createElement("video");
        video.muted = true; video.playsInline = true; video.preload = "auto";
        if (this.opts.crossOrigin) video.crossOrigin = "anonymous";

        const img = document.createElement("img");
        img.className = "cc-cam-photo"; img.alt = "";

        const black = document.createElement("div");
        black.className = "cc-cam-black";

        const label = document.createElement("div");
        label.className = "cc-cam-label";
        label.textContent = cid;

        const badge = document.createElement("div");
        badge.className = "cc-cam-badge";

        cell.append(video, img, black, label, badge);

        return {
            id: cid,
            videos: block.videos || [],
            photos: block.photos || [],
            els: { cell, video, img, black, badge },
            activeUrl: null, activePhoto: null, needSeek: null, stall: false, stallSince: 0,
            photoCache: new Map(), photoInflight: 0, photoWaitSince: 0,
        };
    }

    mountGrid() {
        const grid = $("ccGrid");
        if (!grid) return;
        this.cams.forEach((c) => {
            try { c.els.video.pause(); c.els.video.removeAttribute("src"); c.els.video.load(); } catch (e) { /* ignore */ }
            for (const e of c.photoCache.values()) this.abortPhoto(e);
        });
        grid.innerHTML = "";
        this.cams = [];

        const rect = grid.getBoundingClientRect();
        const R = (rect.width && rect.height) ? rect.width / rect.height : 16 / 9;
        const layout = computeLayout(
            this.selected.map((id) => ({ id, orient: this.camOrient.get(id) || "landscape" })),
            R
        );

        for (const col of layout) {
            const colEl = document.createElement("div");
            colEl.className = "cc-col";
            colEl.style.flexGrow = String(col.k);
            for (const cid of col.ids) {
                const cam = this.buildCell(cid);
                colEl.appendChild(cam.els.cell);
                this.cams.push(cam);
            }
            grid.appendChild(colEl);
        }

        grid.dataset.count = String(this.selected.length);
        this.syncAll(true);
    }

    showLayer(c, which) {
        c.els.video.style.visibility = which === "video" ? "visible" : "hidden";
        c.els.img.style.display = which === "photo" ? "block" : "none";
        c.els.black.style.display = which === "black" ? "flex" : "none";
        c.els.badge.textContent =
            which === "video" ? "відео" : which === "photo" ? "фото" : "немає";
        c.els.badge.dataset.kind = which;
    }

    // ── попереднє завантаження фото ──────────────────────────────────
    // Окремі Image() не пов'язані з <img> на екрані, тож зміна кадру їх не скасовує;
    // на екран фото потрапляє вже з кешу пам'яті браузера, без нового запиту.
    // Слот паралельності звільняється рівно один раз: по load/error, по таймауту
    // або при скасуванні. На promise decode() не покладаємось — після img.src = ""
    // він може так і не завершитись, і слот губився б назавжди.
    loadPhoto(c, url, idx) {
        const img = new Image();
        const entry = { img, idx, state: "loading", skip: false, timer: 0, settle: null };
        entry.settle = (state) => {
            if (entry.state !== "loading") return;
            entry.state = state;
            clearTimeout(entry.timer);
            img.onload = img.onerror = null;
            c.photoInflight--;
        };
        img.onload = () => {
            img.decode().catch(() => { }).then(() => entry.settle(img.naturalWidth ? "ready" : "error"));
        };
        img.onerror = () => entry.settle("error");
        entry.timer = setTimeout(() => this.abortPhoto(entry), PHOTO_LOAD_TIMEOUT_MS);
        c.photoCache.set(url, entry);
        c.photoInflight++;
        img.src = url;
        return entry;
    }

    abortPhoto(entry) {
        if (entry.state !== "loading") return;
        if (entry.img.complete && entry.img.naturalWidth) { entry.settle("ready"); return; }
        entry.settle("error");
        entry.img.src = "";
    }

    // Тримає завантаженими фото від start на PHOTO_AHEAD_SEC реального часу вперед,
    // найближчі — першими. Поточне фото стартує поза лімітом паралельності.
    prefetchPhotos(c, start, utc) {
        const photos = c.photos;
        const endUtc = utc + this.rate() * PHOTO_AHEAD_SEC;
        let end = start, readyAhead = 0, contiguous = true;
        for (; end < photos.length && end - start < PHOTO_AHEAD_MAX; end++) {
            if (end - start >= PHOTO_AHEAD_MIN && photos[end].utc > endUtc) break;
            const url = photos[end].url;
            let e = c.photoCache.get(url);
            if (!e && (end === start || c.photoInflight < PHOTO_PARALLEL)) e = this.loadPhoto(c, url, end);
            if (contiguous && photoSettled(e)) readyAhead++;
            else contiguous = false;
        }
        if (c.photoCache.size > (end - start) + PHOTO_KEEP_BEHIND + PHOTO_PARALLEL) {
            this.evictPhotos(c, start - PHOTO_KEEP_BEHIND, end);
        }
        return { readyAhead, size: end - start };
    }

    evictPhotos(c, lo, hi) {
        for (const [url, e] of c.photoCache) {
            if (e.idx >= lo && e.idx < hi) continue;
            this.abortPhoto(e);
            c.photoCache.delete(url);
        }
    }

    // Поки поточне фото не готове — тримаємо майстер-годинник (як буферизація відео).
    // Після зупинки рушаємо, лише коли готова половина вікна наперед, інакше
    // плеєр смикався б «стоп-старт» на кожному кадрі.
    holdForPhoto(c, entry, buf) {
        if (!this.playing) { c.photoWaitSince = 0; c.stall = false; return; }
        const now = performance.now();
        if (!photoSettled(entry) && !c.photoWaitSince) c.photoWaitSince = now;
        if (c.photoWaitSince) {
            if (now - c.photoWaitSince > PHOTO_WAIT_MAX_MS) {
                entry.skip = true;
                c.photoWaitSince = 0;
            } else if (photoSettled(entry) && buf.readyAhead >= Math.ceil(buf.size / 2)) {
                c.photoWaitSince = 0;
            }
        }
        c.stall = c.photoWaitSince > 0;
    }

    showPhotoAt(c, utc) {
        if (!c.els.video.paused) c.els.video.pause();
        c.activeUrl = null;
        c.stallSince = 0;
        const idx = photoIndex(c.photos, utc);
        const photo = idx >= 0 && utc - c.photos[idx].utc <= PHOTO_MAX_GAP ? c.photos[idx] : null;
        const buf = this.prefetchPhotos(c, photo ? idx : idx + 1, utc);
        if (!photo) {
            this.showLayer(c, "black");
            c.activePhoto = null;
            c.photoWaitSince = 0;
            c.stall = false;
            return false;
        }

        const entry = c.photoCache.get(photo.url);
        this.holdForPhoto(c, entry, buf);

        if (entry.state === "error") {
            this.showLayer(c, "black");
            c.els.badge.textContent = "фото недоступне";
            c.activePhoto = null;
            return true;
        }
        this.showLayer(c, "photo");
        if (entry.state === "ready" && c.activePhoto !== photo.url) {
            c.activePhoto = photo.url;
            c.els.img.src = photo.url;
        }
        if (entry.state === "loading") c.els.badge.textContent = "фото · завантаження…";
        return true;
    }

    showVideoAt(c, utc, force) {
        const chunk = findChunk(c.videos, utc);
        if (!chunk) return false;

        c.photoWaitSince = 0;
        this.showLayer(c, "video");
        const v = c.els.video;
        if (c.activeUrl !== chunk.url) {
            c.activeUrl = chunk.url;
            c.activePhoto = null;
            v.src = chunk.url;
            c.needSeek = utc - chunk.chunk_start_utc;
        }
        const desired = utc - chunk.chunk_start_utc;
        if (v.readyState >= 1) {
            if (c.needSeek != null) {
                try { v.currentTime = c.needSeek; } catch (e) { /* ignore */ }
                c.needSeek = null;
            } else if (force || Math.abs(v.currentTime - desired) > DRIFT_SEC) {
                try { v.currentTime = desired; } catch (e) { /* ignore */ }
            }
        }
        if (this.playing && !this.holding) {
            v.playbackRate = this.rate();
            if (v.paused) v.play().catch(() => { });
        } else if (!v.paused) {
            v.pause();
        }
        const atEnd = v.ended || (v.duration > 0 && v.currentTime >= v.duration - END_EPS);
        const buffering = this.playing && v.readyState < 3 && !atEnd;
        if (buffering) {
            if (!c.stallSince) c.stallSince = performance.now();
            c.stall = (performance.now() - c.stallSince) < STALL_TIMEOUT_MS;
        } else {
            c.stallSince = 0;
            c.stall = false;
        }
        return true;
    }

    syncCam(c, force) {
        const utc = this.winStart + this.master;
        const preferPhoto = this.opts.sourcePriority === "photo";

        if (preferPhoto) {
            if (this.showPhotoAt(c, utc)) return;
            this.showVideoAt(c, utc, force);
            return;
        }
        if (this.showVideoAt(c, utc, force)) return;
        this.showPhotoAt(c, utc);
    }

    syncAll(force) {
        for (const c of this.cams) this.syncCam(c, force);
    }

    loop(ts) {
        const now = performance.now();
        const dt = this.lastTs ? (now - this.lastTs) / 1000 : 0;
        this.lastTs = now;

        if (this.playing && !this.holding) {
            this.master = clamp(this.master + dt * this.rate(), 0, this.durSec);
        }

        let anyStall = false;
        for (const c of this.cams) { this.syncCam(c, false); if (c.stall) anyStall = true; }
        this.holding = this.playing && anyStall;

        if (this.playing && this.master >= this.durSec) this.pause();

        this.updateTimeUI();
        this.drawPlayhead();
        this.raf = requestAnimationFrame((t) => this.loop(t));
    }

    updateTimeUI() {
        const cur = $("ccCur");
        if (cur) cur.textContent = clock(this.winStart + this.master);
        const seek = $("ccSeek");
        if (seek && document.activeElement !== seek) {
            seek.value = String(Math.floor(this.master));
        }
        this._updateRateUI();
    }

    _updatePlayIcon() {
        const icon = $("ccPlayIcon");
        if (!icon) return;
        if (this.playing) {
            icon.src = "../../static/images/pause.svg";
            icon.classList.add("is-playing");
        } else {
            icon.src = "../../static/images/play-video.svg";
            icon.classList.remove("is-playing");
        }
    }

    play() {
        if (this.master >= this.durSec) this.master = 0;
        this.playing = true;
        this._updatePlayIcon();
    }

    pause() {
        this.playing = false;
        this.holding = false;
        this._updatePlayIcon();
        for (const c of this.cams) if (!c.els.video.paused) c.els.video.pause();
    }

    toggle() { this.playing ? this.pause() : this.play(); }

    seekTo(sec) {
        this.master = clamp(sec, 0, this.durSec);
        this.syncAll(true);
        this.updateTimeUI();
        this.drawPlayhead();
    }

    seekBy(sec) { this.seekTo(this.master + sec); }

    setRate(idx) {
        // Не даємо піднятися вище стелі поточного режиму (на відео — 16×),
        // інакше з'являлися б «фантомні» щаблі, які потім треба прощовкувати.
        this.rateIdx = clamp(idx, 0, this.maxRateIdx());
        this._updateRateUI();
        for (const c of this.cams) c.els.video.playbackRate = this.rate();
    }

    // Крок швидкості рахуємо від ЕФЕКТИВНОГО щабля, а не від збереженого,
    // тож на відео стрілка вниз одразу дає 8× (без холостих натискань).
    stepRate(dir) {
        const effIdx = Math.min(this.rateIdx, this.maxRateIdx());
        this.setRate(effIdx + dir);
    }

    // Показуємо ефективну швидкість (з урахуванням стелі відео).
    _updateRateUI() {
        const el = $("ccRate");
        if (el) el.textContent = this.rate() + "×";
    }

    // ── графік ───────────────────────────────────────────────────────
    graphHeight() {
        return this.SPEED_H + this.selected.length * (this.LANE_H + this.LANE_GAP) + 8;
    }

    resizeCanvas() {
        if (!this.canvas) return;
        this.dpr = window.devicePixelRatio || 1;
        this.cssW = this.canvas.clientWidth || this.canvas.parentElement.clientWidth || 800;
        this.cssH = this.graphHeight();
        this.canvas.style.height = this.cssH + "px";
        this.canvas.width = Math.round(this.cssW * this.dpr);
        this.canvas.height = Math.round(this.cssH * this.dpr);
        this.buildGraphStatic();
        this.drawPlayhead();
    }

    xOf(utc) { return ((utc - this.viewStart) / (this.viewEnd - this.viewStart)) * this.cssW; }

    themeColors() {
        const dark = document.documentElement.getAttribute("data-theme") !== "light";
        return {
            grid: dark ? "#2a303b" : "#e2e6ee",
            axis: dark ? "#8b93a2" : "#6b7280",
            speed: "#3b82f6",
            video: dark ? "#14532d" : "#bbf7d0",
            videoLine: "#22c55e",
            photo: dark ? "#7c2d12" : "#fed7aa",
            bg: dark ? "#0f1216" : "#ffffff",
            text: dark ? "#c7cdd8" : "#374151",
        };
    }

    buildGraphStatic() {
        if (!this.cssW || !this.canvas) return;
        const col = this.themeColors();
        const off = document.createElement("canvas");
        off.width = this.canvas.width; off.height = this.canvas.height;
        const c = off.getContext("2d");
        c.scale(this.dpr, this.dpr);
        c.clearRect(0, 0, this.cssW, this.cssH);

        c.strokeStyle = col.grid; c.fillStyle = col.axis;
        c.lineWidth = 1; c.font = "10px system-ui, sans-serif";
        const viewSpan = this.viewEnd - this.viewStart;
        const stepH = viewSpan > 4 * 3600 ? 3600 : viewSpan > 900 ? 600 : 60;
        const firstTick = Math.ceil(this.viewStart / stepH) * stepH;
        for (let t = firstTick; t <= this.viewEnd; t += stepH) {
            const x = this.xOf(t);
            c.beginPath(); c.moveTo(x, 0); c.lineTo(x, this.cssH); c.stroke();
            const d = new Date((t + LOCAL_OFFSET) * 1000);
            const hh = String(d.getUTCHours()).padStart(2, "0");
            const mm = String(d.getUTCMinutes()).padStart(2, "0");
            c.fillText(`${hh}:${mm}`, x + 2, 10);
        }

        const speed = (this.media && this.media.speed) || [];
        if (speed.length) {
            let vmax = 1;
            for (const p of speed) if (p.v > vmax) vmax = p.v;
            c.strokeStyle = col.speed; c.lineWidth = 1.5;
            c.beginPath();
            let started = false;
            speed.forEach((p) => {
                if (p.t < this.viewStart || p.t > this.viewEnd) return;
                const x = this.xOf(p.t);
                const y = this.SPEED_H - (p.v / vmax) * (this.SPEED_H - 14) - 2;
                if (!started) { c.moveTo(x, y); started = true; }
                else c.lineTo(x, y);
            });
            c.stroke();
            c.fillStyle = col.axis;
        } else {
            c.fillStyle = col.axis;
            c.fillText("немає треку швидкості", 4, this.SPEED_H / 2);
        }

        this.selected.forEach((id, idx) => {
            const y = this.SPEED_H + idx * (this.LANE_H + this.LANE_GAP);
            const block = ((this.media && this.media.day_media) || {})[id] || { videos: [], photos: [] };
            c.fillStyle = col.grid;
            c.globalAlpha = 0.25; c.fillRect(0, y, this.cssW, this.LANE_H); c.globalAlpha = 1;
            c.fillStyle = col.video;
            for (const v of block.videos) {
                const x1 = this.xOf(v.chunk_start_utc), x2 = this.xOf(v.chunk_stop_utc);
                c.fillRect(x1, y + 2, Math.max(1, x2 - x1), this.LANE_H - 4);
            }
            if (block.photos.length) {
                const colHit = new Uint8Array(Math.max(1, Math.ceil(this.cssW)));
                for (const p of block.photos) {
                    const px = Math.floor(this.xOf(p.utc));
                    if (px >= 0 && px < colHit.length) colHit[px] = 1;
                }
                c.fillStyle = col.photo;
                for (let px = 0; px < colHit.length; px++) {
                    if (colHit[px]) c.fillRect(px, y + this.LANE_H - 6, 1, 4);
                }
            }
            c.fillStyle = col.text;
            c.fillText(id, 4, y + 13);
        });

        this.staticLayer = off;
    }

    drawPlayhead() {
        if (!this.staticLayer || !this.ctx) return;
        this.ctx.setTransform(1, 0, 0, 1, 0, 0);
        this.ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);
        this.ctx.drawImage(this.staticLayer, 0, 0);
        this.ctx.save();
        this.ctx.scale(this.dpr, this.dpr);
        const x = this.xOf(this.winStart + this.master);
        this.ctx.strokeStyle = "#ef4444"; this.ctx.lineWidth = 2;
        this.ctx.beginPath(); this.ctx.moveTo(x, 0); this.ctx.lineTo(x, this.cssH); this.ctx.stroke();
        this.ctx.restore();
    }

    onCanvasClick(ev) {
        const rect = this.canvas.getBoundingClientRect();
        const x = ev.clientX - rect.left;
        const utc = this.viewStart + (x / this.cssW) * (this.viewEnd - this.viewStart);
        this.seekTo(utc - this.winStart);
    }

    onCanvasWheel(ev) {
        ev.preventDefault();
        const rect = this.canvas.getBoundingClientRect();
        const x = clamp(ev.clientX - rect.left, 0, this.cssW || 1);
        const span = this.viewEnd - this.viewStart;
        const utcAt = this.viewStart + (x / (this.cssW || 1)) * span;
        const factor = ev.deltaY < 0 ? 1 / ZOOM_STEP : ZOOM_STEP;
        const newSpan = clamp(span * factor, MIN_VIEW_SEC, this.durSec);
        const frac = (utcAt - this.viewStart) / span;
        let ns = utcAt - frac * newSpan;
        let ne = ns + newSpan;
        if (ns < this.winStart) { ns = this.winStart; ne = ns + newSpan; }
        if (ne > this.winEnd) { ne = this.winEnd; ns = ne - newSpan; }
        if (ns < this.winStart) ns = this.winStart;
        this.viewStart = ns; this.viewEnd = ne;
        this.buildGraphStatic();
        this.drawPlayhead();
    }

    // ── time_spent + автозбереження ──────────────────────────────────
    accrueVisibleTime() {
        if (document.visibilityState !== "visible") return;
        const now = performance.now();
        this.pendingTimeSec += (now - this.visibleSince) / 1000;
        this.visibleSince = now;
    }

    takePendingTimeSec() {
        this.accrueVisibleTime();
        const sec = Math.max(0, Math.floor(this.pendingTimeSec));
        this.pendingTimeSec -= sec;
        return sec;
    }

    autosavePayload() {
        const pending = this.takePendingTimeSec();
        if (typeof this.opts.buildPayload === "function") {
            return this.opts.buildPayload(pending);
        }
        return { add_time_sec: pending };
    }

    async flushAutosave(isUnload = false) {
        if (!this.opts.autosaveUrl) return;
        const payload = this.autosavePayload();

        if (isUnload && navigator.sendBeacon) {
            navigator.sendBeacon(
                this.opts.autosaveUrl,
                new Blob([JSON.stringify(payload)], { type: "application/json" })
            );
            return;
        }
        if (this.autosaveBusy) return;
        this.autosaveBusy = true;
        try {
            const resp = await fetch(this.opts.autosaveUrl, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            });
            if (!resp.ok) return;
            const state = $("ccSaveState");
            if (state && !state.textContent.includes("Збережено")) {
                state.textContent = "Автозбережено " + new Date().toLocaleTimeString();
            }
        } catch (e) { /* наступна спроба через 10 с */ }
        finally { this.autosaveBusy = false; }
    }

    startAutosave() {
        if (this.autosaveTimer) return;
        this.visibleSince = performance.now();
        document.addEventListener("visibilitychange", () => {
            if (document.visibilityState === "hidden") {
                this.accrueVisibleTime();
                this.flushAutosave(true);
            } else {
                this.visibleSince = performance.now();
            }
        });
        this.autosaveTimer = setInterval(() => this.flushAutosave(false), AUTOSAVE_MS);
        window.addEventListener("pagehide", () => this.flushAutosave(true));
    }

    bindControls() {
        $("ccPlay")?.addEventListener("click", () => this.toggle());
        $("ccBack")?.addEventListener("click", () => this.seekBy(-1));
        $("ccFwd")?.addEventListener("click", () => this.seekBy(1));
        $("ccRateUp")?.addEventListener("click", () => this.stepRate(+1));
        $("ccRateDown")?.addEventListener("click", () => this.stepRate(-1));
        $("ccSeek")?.addEventListener("input", (e) => this.seekTo(parseInt(e.target.value, 10) || 0));
        $("ccSeek")?.addEventListener("keydown", (e) => {
            if (e.code !== "Space" || e.repeat) return;
            e.preventDefault();
            this.toggle();
        });
        $("ccShowGraph")?.addEventListener("change", (e) => {
            const graphs = $("ccGraphs");
            if (!graphs) return;
            graphs.style.display = e.target.checked ? "" : "none";
            if (e.target.checked) this.resizeCanvas();
        });
        if (this.canvas) {
            this.canvas.addEventListener("click", (ev) => this.onCanvasClick(ev));
            this.canvas.addEventListener("wheel", (ev) => this.onCanvasWheel(ev), { passive: false });
        }
        window.addEventListener("resize", () => this.resizeCanvas());
        window.addEventListener("keydown", (e) => {
            if (["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement.tagName)) return;
            if (e.code === "Space") { e.preventDefault(); this.toggle(); }
            else if (e.code === "ArrowLeft") { e.preventDefault(); this.seekBy(-1); }
            else if (e.code === "ArrowRight") { e.preventDefault(); this.seekBy(1); }
            else if (e.code === "ArrowUp") { e.preventDefault(); this.stepRate(+1); }
            else if (e.code === "ArrowDown") { e.preventDefault(); this.stepRate(-1); }
        });
        new MutationObserver(() => this.buildGraphStatic()).observe(
            document.documentElement, { attributes: true, attributeFilter: ["data-theme"] }
        );
        this.setRate(this.rateIdx);
    }
}
