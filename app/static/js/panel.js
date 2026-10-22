/** Логика панели рахувальника: тема, модалка коментаря, фильтр, last_seen. */

// ---------- Тема (світла/темна), запоминаем в localStorage ----------
function initTheme() {
    const toggle = document.getElementById("themeToggle");
    if (!toggle) return;
    const apply = (theme) => {
        document.documentElement.setAttribute("data-theme", theme);
        try { localStorage.setItem("panelTheme", theme); } catch (e) { }
    };
    toggle.addEventListener("click", () => {
        const current = document.documentElement.getAttribute("data-theme") || "dark";
        apply(current === "dark" ? "light" : "dark");
    });
}

// ---------- Относительное время последней координаты ----------
function formatRelative(ts) {
    const now = Math.floor(Date.now() / 1000);
    let diff = now - ts;
    const future = diff < 0;
    diff = Math.abs(diff);

    const units = [
        [31536000, "р."],
        [2592000, "міс."],
        [86400, "дн."],
        [3600, "год."],
        [60, "хв."],
        [1, "с"],
    ];
    let text = "щойно";
    for (const [sec, label] of units) {
        if (diff >= sec) {
            text = Math.floor(diff / sec) + " " + label;
            break;
        }
    }
    if (future) return "через " + text;
    return text === "щойно" ? text : text + " тому";
}

function initLastSeen() {
    const STALE_SEC = 24 * 3600; // >24 год — вважаємо неактивною
    const now = Math.floor(Date.now() / 1000);
    document.querySelectorAll(".last-seen").forEach((el) => {
        const raw = el.dataset.ts;
        if (!raw) return;
        const ts = parseInt(raw, 10);
        if (!ts || Number.isNaN(ts)) {
            if (!el.querySelector(".last-seen-hint")) {
                el.textContent = "";
                const hint = document.createElement("span");
                hint.className = "muted last-seen-hint";
                hint.textContent = "Невірний формат last_seen: " + raw;
                el.appendChild(hint);
            }
            return;
        }
        el.textContent = formatRelative(ts);
        el.title = new Date(ts * 1000).toLocaleString("uk-UA");
        if (now - ts > STALE_SEC) el.classList.add("stale");
    });
}

// ---------- Фильтр «лише не підраховані» ----------
function initPendingFilter() {
    const cb = document.getElementById("onlyPending");
    const table = document.getElementById("carsTable");
    if (!cb || !table) return;
    const hint = document.getElementById("allDoneHint");

    const apply = () => {
        table.classList.toggle("hide-done", cb.checked);
        if (hint) {
            const visible = [...table.querySelectorAll('tbody tr')]
                .some((tr) => tr.offsetParent !== null);
            hint.classList.toggle("hidden", visible || !cb.checked);
        }
    };
    cb.addEventListener("change", apply);
    apply();
}

// ---------- Модалка коментаря ----------
function initCommentModal() {
    const modal = document.getElementById("commentModal");
    if (!modal) return;
    const textarea = document.getElementById("carCommentInput");
    const title = document.getElementById("commentModalTitle");
    const saveBtn = document.getElementById("carCommentSave");
    const statusEl = document.getElementById("carCommentStatus");
    let activeBtn = null;

    const updateBtnState = (btn, comment) => {
        const text = comment || "";
        btn.dataset.comment = text;
        const has = Boolean(text.trim());
        btn.classList.toggle("comment-btn-empty", !has);
        btn.title = has ? "Коментар про машину" : "Додати коментар";
    };

    const close = () => {
        modal.classList.add("hidden");
        activeBtn = null;
        if (statusEl) {
            statusEl.textContent = "";
            statusEl.classList.add("hidden");
        }
    };

    const open = (btn) => {
        activeBtn = btn;
        const car = btn.dataset.car || "";
        if (textarea) textarea.value = btn.dataset.comment || "";
        if (title) title.textContent = car ? ("Коментар · " + car) : "Коментар";
        if (statusEl) {
            statusEl.textContent = "";
            statusEl.classList.add("hidden");
        }
        modal.classList.remove("hidden");
        if (textarea) textarea.focus();
    };

    document.querySelectorAll(".comment-btn").forEach((btn) => {
        btn.addEventListener("click", () => open(btn));
    });

    if (saveBtn) {
        saveBtn.addEventListener("click", async () => {
            if (!activeBtn) return;
            const carId = activeBtn.dataset.carId;
            const comment = (textarea ? textarea.value : "").trim();
            const counterId = new URL(window.location.href).searchParams.get("counter_id");

            saveBtn.disabled = true;
            try {
                const res = await fetch(`/panel/car/${carId}/comment`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        comment,
                        counter_id: counterId ? Number(counterId) : null,
                    }),
                });
                const data = await res.json();
                if (!res.ok || !data.ok) {
                    throw new Error(data.error || "save_failed");
                }
                updateBtnState(activeBtn, data.comment || "");
                close();
            } catch (e) {
                if (statusEl) {
                    statusEl.textContent = e.message === "access_denied"
                        ? "Немає доступу до цієї машини"
                        : "Не вдалося зберегти коментар";
                    statusEl.classList.remove("hidden");
                }
            } finally {
                saveBtn.disabled = false;
            }
        });
    }

    modal.querySelectorAll("[data-close-modal]").forEach((el) => el.addEventListener("click", close));
    modal.addEventListener("click", (e) => { if (e.target === modal) close(); });
    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && !modal.classList.contains("hidden")) close();
    });
}

// ---------- Вибір типу маршруту (місто / міжмісто) ----------
function routeKindStorageKey(carId, day, counterId) {
    return `routeKind:${counterId || "self"}:${carId}:${day}`;
}

function syncRouteKindInputs(carId, kind) {
    document.querySelectorAll(`.route-kind-input[data-car-id="${carId}"]`).forEach((inp) => {
        inp.value = kind;
    });
}

function applyRouteKindToggle(btn, kind) {
    const defaultKind = btn.dataset.defaultKind;
    const defaultLabel = btn.dataset.defaultLabel;
    const alternateKind = btn.dataset.alternateKind;
    const alternateLabel = btn.dataset.alternateLabel;
    const isChosen = kind !== defaultKind;
    const label = kind === defaultKind ? defaultLabel : alternateLabel;

    btn.dataset.kind = kind;
    btn.dataset.chosen = isChosen ? "1" : "0";
    btn.classList.remove("pill-city", "pill-intercity");
    btn.classList.add(`pill-${kind}`);
    btn.textContent = label + (isChosen ? " (вибраний)" : "");
}

function initRouteKindToggle() {
    document.querySelectorAll(".route-kind-toggle").forEach((btn) => {
        const carId = btn.dataset.carId;
        const day = btn.dataset.day;
        const counterId = btn.dataset.counterId || "";
        const key = routeKindStorageKey(carId, day, counterId);
        const stored = sessionStorage.getItem(key);
        if (stored === btn.dataset.defaultKind || stored === btn.dataset.alternateKind) {
            applyRouteKindToggle(btn, stored);
            syncRouteKindInputs(carId, stored);
        }

        btn.addEventListener("click", () => {
            const current = btn.dataset.kind;
            const next = current === btn.dataset.defaultKind
                ? btn.dataset.alternateKind
                : btn.dataset.defaultKind;
            sessionStorage.setItem(key, next);
            applyRouteKindToggle(btn, next);
            syncRouteKindInputs(carId, next);
        });
    });

    // Підтвердження пересбору: якщо тип маршруту змінено — попереджаємо, що
    // введені числа НЕ збережуться (структура даних міста/міжміста різна).
    document.querySelectorAll(".reset-form").forEach((form) => {
        form.addEventListener("submit", (e) => {
            const name = form.dataset.carName || "";
            const currentKind = form.dataset.currentKind;
            const input = form.querySelector(".route-kind-input");
            const chosen = input ? input.value : currentKind;
            const kindChanged = input && chosen !== currentKind;
            let msg;
            if (kindChanged) {
                msg = `Перерахувати ${name} з новим типом маршруту?\n\n` +
                    `Дані буде зібрано заново, введені числа НЕ збережуться ` +
                    `(тип підрахунку змінюється).`;
            } else {
                msg = `Перерахувати ${name}?\n\nДані буде перезібрано, ` +
                    `введені числа збережуться там, де відрізок не змінився.`;
            }
            if (!window.confirm(msg)) {
                e.preventDefault();
            }
        });
    });

    const startForm = document.getElementById("countStartForm");
    if (!startForm || startForm.dataset.routeKindChose !== "1") return;

    const carId = startForm.dataset.carId;
    const day = startForm.dataset.day;
    const counterId = startForm.dataset.counterId || "";
    const key = routeKindStorageKey(carId, day, counterId);
    const stored = sessionStorage.getItem(key);
    const input = document.getElementById("countStartRouteKind");
    if (input && (stored === "city" || stored === "intercity")) {
        input.value = stored;
    }
}

// ---------- Вибір дня (ДД.ММ.РРРР) ----------
function initDayPicker() {
    const input = document.getElementById("panelDayInput");
    if (!input) return;

    const parseDay = (raw) => {
        const m = String(raw).trim().match(/^(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})$/);
        if (!m) return null;
        const d = Number(m[1]);
        const mo = Number(m[2]);
        const y = Number(m[3]);
        const dt = new Date(y, mo - 1, d);
        if (dt.getFullYear() !== y || dt.getMonth() !== mo - 1 || dt.getDate() !== d) return null;
        return `${y}-${String(mo).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
    };

    const formatIsoToDisplay = (iso) => {
        const [y, m, d] = iso.split("-");
        return `${d}.${m}.${y}`;
    };

    const go = (iso) => {
        const url = new URL(window.location.href);
        url.searchParams.set("day", iso);
        url.searchParams.delete("page");
        window.location.href = url.toString();
    };

    const resetInvalid = () => input.classList.remove("is-invalid");

    input.addEventListener("keydown", (e) => {
        if (e.key !== "Enter") return;
        e.preventDefault();
        const iso = parseDay(input.value);
        if (!iso) {
            input.classList.add("is-invalid");
            return;
        }
        resetInvalid();
        if (iso !== input.dataset.dayIso) go(iso);
    });

    input.addEventListener("blur", () => {
        const iso = parseDay(input.value);
        if (!iso) {
            input.value = formatIsoToDisplay(input.dataset.dayIso);
            resetInvalid();
            return;
        }
        resetInvalid();
        if (iso !== input.dataset.dayIso) go(iso);
    });

    input.addEventListener("input", resetInvalid);
}

// ---------- Модалка причини пропуску (перегляд / редагування) ----------
function initSkipCommentModal() {
    const modal = document.getElementById("skipCommentModal");
    if (!modal) return;

    const textarea = document.getElementById("skipCommentEdit");
    const title = document.getElementById("skipCommentModalTitle");
    const hint = document.getElementById("skipCommentModalHint");
    const saveBtn = document.getElementById("skipCommentSave");
    const statusEl = document.getElementById("skipCommentEditStatus");
    let activeBtn = null;

    const updateBtnState = (btn, comment) => {
        const text = comment || "";
        btn.dataset.skipComment = text;
        btn.title = text.trim() ? "Причина пропуску" : "Додати причину пропуску";
    };

    const close = () => {
        modal.classList.add("hidden");
        activeBtn = null;
        if (statusEl) {
            statusEl.textContent = "";
            statusEl.classList.add("hidden");
        }
    };

    const open = (btn) => {
        activeBtn = btn;
        const car = btn.dataset.carName || "";
        const day = btn.dataset.dayDisplay || "";
        if (textarea) textarea.value = btn.dataset.skipComment || "";
        if (title) title.textContent = "Причина пропуску";
        if (hint) {
            hint.textContent = car && day
                ? `Пропущено: ${car} за ${day}`
                : "";
        }
        if (statusEl) {
            statusEl.textContent = "";
            statusEl.classList.add("hidden");
        }
        modal.classList.remove("hidden");
        if (textarea) textarea.focus();
    };

    document.querySelectorAll(".skip-comment-trigger").forEach((btn) => {
        btn.addEventListener("click", () => open(btn));
    });

    if (saveBtn) {
        saveBtn.addEventListener("click", async () => {
            if (!activeBtn) return;
            const carId = activeBtn.dataset.carId;
            const day = activeBtn.dataset.day;
            const comment = (textarea ? textarea.value : "").trim();
            const counterId = activeBtn.dataset.counterId
                || new URL(window.location.href).searchParams.get("counter_id");

            saveBtn.disabled = true;
            try {
                const res = await fetch(`/panel/count/${carId}/skip-comment`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        day,
                        comment,
                        counter_id: counterId ? Number(counterId) : null,
                    }),
                });
                const data = await res.json();
                if (!res.ok || !data.ok) {
                    throw new Error(data.error || "save_failed");
                }
                updateBtnState(activeBtn, data.comment || "");
                close();
            } catch (e) {
                if (statusEl) {
                    const messages = {
                        session_not_found: "Сесію не знайдено",
                        not_skipped: "Підрахунок більше не пропущено",
                    };
                    statusEl.textContent = messages[e.message] || "Не вдалося зберегти коментар";
                    statusEl.classList.remove("hidden");
                }
            } finally {
                saveBtn.disabled = false;
            }
        });
    }

    modal.querySelectorAll("[data-close-skip-comment-modal]").forEach((el) => {
        el.addEventListener("click", close);
    });
    modal.addEventListener("click", (e) => {
        if (e.target === modal) close();
    });
    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && !modal.classList.contains("hidden")) close();
    });
}

// ---------- Модалка пропуску підрахунку ----------
function initSkipModal() {
    const modal = document.getElementById("skipModal");
    const form = document.getElementById("skipModalForm");
    if (!modal || !form) return;

    const hint = document.getElementById("skipModalHint");
    const dayInput = document.getElementById("skipModalDay");
    const typeInput = document.getElementById("skipModalType");
    const counterInput = document.getElementById("skipModalCounterId");
    const routeKindInput = document.getElementById("skipModalRouteKind");
    const commentInput = document.getElementById("skipComment");

    const setOptionalField = (input, value, name) => {
        if (!input) return;
        const text = value || "";
        input.value = text;
        if (text) input.setAttribute("name", name);
        else input.removeAttribute("name");
    };

    const close = () => {
        modal.classList.add("hidden");
        form.reset();
        setOptionalField(counterInput, "", "counter_id");
        setOptionalField(routeKindInput, "", "route_kind");
        setOptionalField(typeInput, "", "type");
    };

    const open = (btn) => {
        form.action = btn.dataset.action || "";
        if (dayInput) dayInput.value = btn.dataset.day || "";
        setOptionalField(typeInput, btn.dataset.scheduleType || "whole_day", "type");
        setOptionalField(counterInput, btn.dataset.counterId, "counter_id");
        if (routeKindInput) {
            const carId = btn.dataset.carId;
            const storedInput = carId
                ? document.querySelector(`.route-kind-input[data-car-id="${carId}"]`)
                : null;
            const routeKind = storedInput ? storedInput.value : (btn.dataset.routeKind || "");
            setOptionalField(routeKindInput, routeKind, "route_kind");
        }
        if (commentInput) commentInput.value = "";
        if (hint) {
            const car = btn.dataset.carName || "";
            const day = btn.dataset.dayDisplay || "";
            hint.textContent = car && day
                ? `Пропустити підрахунок ${car} за ${day}?`
                : "Вкажіть причину пропуску або скасуйте дію.";
        }
        modal.classList.remove("hidden");
        if (commentInput) commentInput.focus();
    };

    document.querySelectorAll(".skip-trigger").forEach((btn) => {
        btn.addEventListener("click", (e) => {
            e.stopPropagation();
            document.querySelectorAll(".action-menu-wrap.is-open").forEach((w) => {
                w.classList.remove("is-open");
            });
            open(btn);
        });
    });

    modal.querySelectorAll("[data-close-skip-modal]").forEach((el) => {
        el.addEventListener("click", close);
    });
    modal.addEventListener("click", (e) => {
        if (e.target === modal) close();
    });
    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && !modal.classList.contains("hidden")) close();
    });
}

// ---------- Меню додаткових дій (пропустити тощо) ----------
function initActionMenus() {
    const closeAll = () => {
        document.querySelectorAll(".action-menu-wrap.is-open").forEach((w) => {
            w.classList.remove("is-open");
        });
    };

    document.querySelectorAll(".action-menu-wrap").forEach((wrap) => {
        const toggle = wrap.querySelector(".btn-action-more");
        if (!toggle) return;
        toggle.addEventListener("click", (e) => {
            e.stopPropagation();
            const open = wrap.classList.contains("is-open");
            closeAll();
            if (!open) wrap.classList.add("is-open");
        });
    });

    document.addEventListener("click", closeAll);
    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape") closeAll();
    });
}

// ---------- Оновлення списку машин ----------
function initReload() {
    const btn = document.getElementById("panelReload");
    if (!btn) return;
    btn.addEventListener("click", () => {
        btn.classList.add("is-spinning");
        window.location.reload();
    });
}

document.addEventListener("DOMContentLoaded", () => {
    initTheme();
    initLastSeen();
    initPendingFilter();
    initCommentModal();
    initDayPicker();
    initActionMenus();
    initSkipCommentModal();
    initSkipModal();
    initRouteKindToggle();
    initReload();
});
