(function () {
    "use strict";

    const MONTH_NAMES = [
        "Січень", "Лютий", "Березень", "Квітень", "Травень", "Червень",
        "Липень", "Серпень", "Вересень", "Жовтень", "Листопад", "Грудень"
    ];
    const COLORS = [
        "#38bdf8", "#f472b6", "#a78bfa", "#34d399",
        "#fbbf24", "#fb923c", "#f87171", "#2dd4bf"
    ];

    const calendarState = {};
    let previewYear, previewMonth;
    let modalSingleDays = [];
    let modalExceptionDays = [];
    let selectedCarByCounter = {};

    // ── Утилиты ──

    function daysInMonth(y, m) {
        return new Date(y, m, 0).getDate();
    }

    function firstWeekday(y, m) {
        let d = new Date(y, m - 1, 1).getDay();
        return d === 0 ? 7 : d;
    }

    function pad(n) { return String(n).padStart(2, "0"); }
    function isoDate(y, m, d) { return `${y}-${pad(m)}-${pad(d)}`; }

    function recurringMode(recurring) {
        return (recurring && recurring.mode) || "fixed";
    }

    function computeLocalDates(recurring, singleDays, exceptionDays, year, month) {
        const days = daysInMonth(year, month);
        const first = new Date(year, month - 1, 1);
        const last = new Date(year, month - 1, days);

        const recurringDates = new Set();
        if (recurring && recurringMode(recurring) !== "counter_choice") {
            if (recurring.type === "weekly") {
                const weekdays = new Set(recurring.days || []);
                const startDate = recurring.start ? new Date(recurring.start) : null;
                for (let d = new Date(first); d <= last; d.setDate(d.getDate() + 1)) {
                    if (startDate && d < startDate) continue;
                    let wd = d.getDay();
                    wd = wd === 0 ? 7 : wd;
                    if (weekdays.has(wd)) recurringDates.add(isoDate(year, month, d.getDate()));
                }
            } else if (recurring.type === "every_n_weeks") {
                const every = recurring.every || 1;
                const dayOfWeek = recurring.day || 1;
                const startDate = recurring.start ? new Date(recurring.start) : first;
                for (let d = new Date(first); d <= last; d.setDate(d.getDate() + 1)) {
                    let wd = d.getDay();
                    wd = wd === 0 ? 7 : wd;
                    if (wd === dayOfWeek) {
                        const deltaDays = Math.floor((d - startDate) / 86400000);
                        const weekNum = Math.floor(deltaDays / 7);
                        if (deltaDays >= 0 && weekNum % every === 0) {
                            recurringDates.add(isoDate(year, month, d.getDate()));
                        }
                    }
                }
            }
        }

        const singleSet = new Set();
        (singleDays || []).forEach(ds => {
            if (ds.startsWith(`${year}-${pad(month)}-`)) singleSet.add(ds);
        });

        const exceptionSet = new Set();
        (exceptionDays || []).forEach(ds => {
            if (ds.startsWith(`${year}-${pad(month)}-`)) exceptionSet.add(ds);
        });

        const allDates = new Set([...recurringDates, ...singleSet]);
        const result = {};
        allDates.forEach(d => {
            result[d] = {
                source: singleSet.has(d) && !recurringDates.has(d) ? "single" : "recurring",
                excluded: exceptionSet.has(d),
            };
        });
        exceptionSet.forEach(d => {
            if (!result[d]) result[d] = { source: "exception_only", excluded: true };
        });

        return result;
    }

    // ── Основной календарь считальщика ──

    function initCalendars() {
        const now = new Date();
        document.querySelectorAll(".sched-card").forEach(card => {
            const cid = +card.dataset.counterId;
            calendarState[cid] = { year: now.getFullYear(), month: now.getMonth() + 1 };
            renderCalendar(cid);
        });
    }

    function renderCalendar(counterId) {
        const st = calendarState[counterId];
        const year = st.year, month = st.month;
        const titleEl = document.querySelector(`[data-month-title="${counterId}"]`);
        if (titleEl) titleEl.textContent = `${MONTH_NAMES[month - 1]} ${year}`;

        const grid = document.querySelector(`[data-calendar="${counterId}"]`);
        if (!grid) return;

        const headers = grid.querySelectorAll(".sched-cal-head");
        grid.innerHTML = "";
        headers.forEach(h => grid.appendChild(h));

        const schedules = SCHEDULE_DATA[counterId] || [];
        const selectedSched = selectedCarByCounter[counterId] || null;

        const dateMap = {};
        schedules.forEach((s, idx) => {
            if (selectedSched && s.id !== selectedSched) return;
            const color = COLORS[idx % COLORS.length];
            const dates = computeLocalDates(s.recurring, s.single_days, s.exception_days, year, month);
            for (const [d, info] of Object.entries(dates)) {
                if (info.excluded) continue;
                if (!dateMap[d]) dateMap[d] = [];
                dateMap[d].push({ color, carName: s.car_name, schedId: s.id });
            }
        });

        const fw = firstWeekday(year, month);
        const dim = daysInMonth(year, month);

        for (let i = 1; i < fw; i++) {
            const empty = document.createElement("div");
            empty.className = "sched-cal-empty";
            grid.appendChild(empty);
        }

        for (let d = 1; d <= dim; d++) {
            const key = isoDate(year, month, d);
            const cell = document.createElement("div");
            cell.className = "sched-cal-day";
            cell.textContent = d;
            cell.dataset.date = key;

            const entries = dateMap[key];
            if (entries && entries.length > 0) {
                cell.classList.add("has-schedule");
                if (entries.length === 1) {
                    cell.style.backgroundColor = entries[0].color + "33";
                    cell.style.borderColor = entries[0].color;
                } else {
                    const grad = entries.map((e, i) => {
                        const pct1 = (i / entries.length * 100).toFixed(0);
                        const pct2 = ((i + 1) / entries.length * 100).toFixed(0);
                        return `${e.color}33 ${pct1}%, ${e.color}33 ${pct2}%`;
                    }).join(", ");
                    cell.style.background = `linear-gradient(135deg, ${grad})`;
                    cell.style.borderColor = entries[0].color;
                }
                cell.onclick = () => showDayDetail(counterId, key, entries);
            }

            const today = new Date();
            if (year === today.getFullYear() && month === today.getMonth() + 1 && d === today.getDate()) {
                cell.classList.add("is-today");
            }

            grid.appendChild(cell);
        }
    }

    function showDayDetail(counterId, dateStr, entries) {
        const panel = document.querySelector(`[data-day-detail="${counterId}"]`);
        if (!panel) return;
        panel.classList.remove("hidden");
        panel.querySelector("[data-detail-date]").textContent = dateStr;
        const list = panel.querySelector("[data-detail-list]");
        list.innerHTML = "";
        entries.forEach(e => {
            const li = document.createElement("li");
            li.innerHTML = `<span class="sched-car-color" style="background:${e.color}"></span> ${e.carName}`;
            list.appendChild(li);
        });
    }

    window.changeMonth = function (counterId, delta) {
        const st = calendarState[counterId];
        st.month += delta;
        if (st.month < 1) { st.month = 12; st.year--; }
        if (st.month > 12) { st.month = 1; st.year++; }
        renderCalendar(counterId);
    };

    window.selectCar = function (el, counterId, schedId) {
        const list = el.closest(".sched-cars-list");
        list.querySelectorAll(".sched-car-item").forEach(i => i.classList.remove("selected"));

        if (selectedCarByCounter[counterId] === schedId) {
            selectedCarByCounter[counterId] = null;
        } else {
            selectedCarByCounter[counterId] = schedId;
            el.classList.add("selected");
        }
        renderCalendar(counterId);
    };

    // ── Модальное окно ──

    function resetCarPicker() {
        const hidden = document.getElementById("modalCarId");
        const search = document.getElementById("modalCarSearch");
        const list = document.getElementById("modalCarOptions");
        if (!hidden || !search || !list) return;
        hidden.value = "";
        search.value = "";
        search.classList.remove("invalid");
        list.querySelectorAll(".searchable-select-option").forEach((item) => {
            item.classList.remove("selected", "hidden");
            item.removeAttribute("aria-selected");
        });
        list.classList.add("hidden");
        search.setAttribute("aria-expanded", "false");
    }

    function setCarPicker(carId) {
        const hidden = document.getElementById("modalCarId");
        const search = document.getElementById("modalCarSearch");
        const list = document.getElementById("modalCarOptions");
        if (!hidden || !search || !list) return;
        const item = list.querySelector(`.searchable-select-option[data-value="${carId}"]`);
        if (!item) {
            hidden.value = String(carId);
            return;
        }
        hidden.value = String(carId);
        search.value = item.textContent.trim();
        list.querySelectorAll(".searchable-select-option").forEach((i) => {
            i.classList.toggle("selected", i === item);
            i.setAttribute("aria-selected", i === item ? "true" : "false");
        });
    }

    window.openModal = function (counterId, scheduleId) {
        const modal = document.getElementById("schedModal");
        modal.classList.remove("hidden");
        document.getElementById("modalCounterId").value = counterId;
        document.getElementById("modalScheduleId").value = scheduleId || "";
        document.getElementById("modalComment").value = "";
        modalSingleDays = [];
        modalExceptionDays = [];

        const isEdit = !!scheduleId;
        document.getElementById("modalTitle").textContent = isEdit ? "Змінити розклад" : "Додати авто";
        document.getElementById("fieldCar").style.display = isEdit ? "none" : "";
        resetCarPicker();

        document.querySelectorAll('[name="weekday"]').forEach(cb => cb.checked = false);
        document.querySelector('[name="recurType"][value="weekly"]').checked = true;
        document.querySelector('[name="weeklyMode"][value="fixed"]').checked = true;
        document.querySelector('[name="everyMode"][value="fixed"]').checked = true;
        document.getElementById("weeklyDaysCount").value = "1";
        onRecurTypeChange();
        onWeeklyModeChange();
        onEveryModeChange();

        const now = new Date();
        previewYear = now.getFullYear();
        previewMonth = now.getMonth() + 1;
        document.getElementById("weeklyStart").value = isoDate(now.getFullYear(), now.getMonth() + 1, now.getDate());
        document.getElementById("everyStart").value = isoDate(now.getFullYear(), now.getMonth() + 1, now.getDate());

        if (isEdit) {
            const scheds = SCHEDULE_DATA[counterId] || [];
            const sched = scheds.find(s => s.id === scheduleId);
            if (sched) {
                populateModalFromSchedule(sched);
            }
        }

        renderTagLists();
        previewDates();
    };

    function populateModalFromSchedule(sched) {
        setCarPicker(sched.car_id);
        document.getElementById("modalComment").value = sched.comment || "";
        modalSingleDays = [...(sched.single_days || [])];
        modalExceptionDays = [...(sched.exception_days || [])];

        const rec = sched.recurring;
        if (!rec) {
            document.querySelector('[name="recurType"][value="none"]').checked = true;
        } else if (rec.type === "weekly") {
            document.querySelector('[name="recurType"][value="weekly"]').checked = true;
            const mode = recurringMode(rec);
            document.querySelector(`[name="weeklyMode"][value="${mode}"]`).checked = true;
            if (mode === "counter_choice") {
                document.getElementById("weeklyDaysCount").value = String(rec.days_count || 1);
            } else {
                (rec.days || []).forEach(d => {
                    const cb = document.querySelector(`[name="weekday"][value="${d}"]`);
                    if (cb) cb.checked = true;
                });
            }
            if (rec.start) document.getElementById("weeklyStart").value = rec.start;
        } else if (rec.type === "every_n_weeks") {
            document.querySelector('[name="recurType"][value="every_n_weeks"]').checked = true;
            const mode = recurringMode(rec);
            document.querySelector(`[name="everyMode"][value="${mode}"]`).checked = true;
            if (mode === "counter_choice") {
                document.getElementById("everyNChoice").value = String(rec.every || 2);
            } else {
                document.getElementById("everyN").value = rec.every || 2;
                document.getElementById("everyDay").value = rec.day || 1;
            }
            if (rec.start) document.getElementById("everyStart").value = rec.start;
        }
        onRecurTypeChange();
        onWeeklyModeChange();
        onEveryModeChange();
    }

    window.closeModal = function () {
        document.getElementById("schedModal").classList.add("hidden");
    };

    window.editSchedule = function (scheduleId) {
        const card = document.querySelector(`[data-schedule-id="${scheduleId}"]`);
        if (!card) return;
        const counterId = +card.closest(".sched-card").dataset.counterId;
        openModal(counterId, scheduleId);
    };

    window.onRecurTypeChange = function () {
        const val = document.querySelector('[name="recurType"]:checked').value;
        document.getElementById("weeklyBlock").classList.toggle("hidden", val !== "weekly");
        document.getElementById("everyNBlock").classList.toggle("hidden", val !== "every_n_weeks");
        previewDates();
    };

    window.onWeeklyModeChange = function () {
        const mode = document.querySelector('[name="weeklyMode"]:checked').value;
        document.getElementById("weeklyFixedBlock").classList.toggle("hidden", mode !== "fixed");
        document.getElementById("weeklyChoiceBlock").classList.toggle("hidden", mode !== "counter_choice");
        previewDates();
    };

    window.onEveryModeChange = function () {
        const mode = document.querySelector('[name="everyMode"]:checked').value;
        document.getElementById("everyFixedBlock").classList.toggle("hidden", mode !== "fixed");
        document.getElementById("everyChoiceBlock").classList.toggle("hidden", mode !== "counter_choice");
        previewDates();
    };

    // ── Дополнительные/исключённые дни ──

    window.addSingleDay = function () {
        const input = document.getElementById("singleDayInput");
        const val = input.value;
        if (val && !modalSingleDays.includes(val)) {
            modalSingleDays.push(val);
            modalSingleDays.sort();
            renderTagLists();
            previewDates();
        }
        input.value = "";
    };

    window.addExceptionDay = function () {
        const input = document.getElementById("exceptionDayInput");
        const val = input.value;
        if (val && !modalExceptionDays.includes(val)) {
            modalExceptionDays.push(val);
            modalExceptionDays.sort();
            renderTagLists();
            previewDates();
        }
        input.value = "";
    };

    function removeSingleDay(d) {
        modalSingleDays = modalSingleDays.filter(x => x !== d);
        renderTagLists();
        previewDates();
    }

    function removeExceptionDay(d) {
        modalExceptionDays = modalExceptionDays.filter(x => x !== d);
        renderTagLists();
        previewDates();
    }

    function renderTagLists() {
        renderTags("singleDaysList", modalSingleDays, removeSingleDay);
        renderTags("exceptionDaysList", modalExceptionDays, removeExceptionDay);
    }

    function renderTags(containerId, list, removeFn) {
        const el = document.getElementById(containerId);
        el.innerHTML = "";
        list.forEach(d => {
            const tag = document.createElement("span");
            tag.className = "sched-tag";
            tag.innerHTML = `${d} <button type="button" onclick="this.remove()">✕</button>`;
            tag.querySelector("button").onclick = (e) => { e.preventDefault(); removeFn(d); };
            el.appendChild(tag);
        });
    }

    // ── Предпросмотр календаря ──

    function getRecurringFromModal() {
        const type = document.querySelector('[name="recurType"]:checked').value;
        if (type === "none") return null;
        if (type === "weekly") {
            const mode = document.querySelector('[name="weeklyMode"]:checked').value;
            if (mode === "counter_choice") {
                const daysCount = +document.getElementById("weeklyDaysCount").value;
                if (!daysCount || daysCount < 1) return null;
                return {
                    type: "weekly",
                    mode: "counter_choice",
                    days_count: daysCount,
                    start: document.getElementById("weeklyStart").value,
                };
            }
            const days = [];
            document.querySelectorAll('[name="weekday"]:checked').forEach(cb => days.push(+cb.value));
            if (days.length === 0) return null;
            return {
                type: "weekly",
                mode: "fixed",
                days,
                start: document.getElementById("weeklyStart").value,
            };
        }
        if (type === "every_n_weeks") {
            const mode = document.querySelector('[name="everyMode"]:checked').value;
            if (mode === "counter_choice") {
                return {
                    type: "every_n_weeks",
                    mode: "counter_choice",
                    every: +document.getElementById("everyNChoice").value,
                    start: document.getElementById("everyStart").value,
                };
            }
            return {
                type: "every_n_weeks",
                mode: "fixed",
                every: +document.getElementById("everyN").value,
                day: +document.getElementById("everyDay").value,
                start: document.getElementById("everyStart").value,
            };
        }
        return null;
    }

    function updatePreviewHint(recurring) {
        const hint = document.getElementById("previewChoiceHint");
        if (!hint) return;
        if (recurring && recurringMode(recurring) === "counter_choice") {
            if (recurring.type === "weekly") {
                hint.textContent =
                    `На розсуд рахувальника: ${recurring.days_count} дн. на тиждень`;
            } else {
                hint.textContent =
                    `На розсуд рахувальника: 1 день кожні ${recurring.every} тижн.`;
            }
            hint.classList.remove("hidden");
        } else {
            hint.classList.add("hidden");
        }
    }

    window.previewDates = function () {
        const recurring = getRecurringFromModal();
        updatePreviewHint(recurring);
        const dates = computeLocalDates(recurring, modalSingleDays, modalExceptionDays, previewYear, previewMonth);
        renderPreviewCalendar(dates);
    };

    function renderPreviewCalendar(dates) {
        document.getElementById("previewMonthTitle").textContent =
            `${MONTH_NAMES[previewMonth - 1]} ${previewYear}`;

        const grid = document.getElementById("previewCalendar");
        const headers = grid.querySelectorAll(".sched-cal-head");
        grid.innerHTML = "";
        headers.forEach(h => grid.appendChild(h));

        const fw = firstWeekday(previewYear, previewMonth);
        const dim = daysInMonth(previewYear, previewMonth);

        for (let i = 1; i < fw; i++) {
            const empty = document.createElement("div");
            empty.className = "sched-cal-empty";
            grid.appendChild(empty);
        }

        for (let d = 1; d <= dim; d++) {
            const key = isoDate(previewYear, previewMonth, d);
            const cell = document.createElement("div");
            cell.className = "sched-cal-day";
            cell.textContent = d;

            const info = dates[key];
            if (info) {
                if (info.excluded) {
                    cell.classList.add("is-excluded");
                } else if (info.source === "single") {
                    cell.classList.add("is-single");
                } else {
                    cell.classList.add("is-recurring");
                }
            }

            const today = new Date();
            if (previewYear === today.getFullYear() && previewMonth === today.getMonth() + 1 && d === today.getDate()) {
                cell.classList.add("is-today");
            }

            grid.appendChild(cell);
        }
    }

    window.changePreviewMonth = function (delta) {
        previewMonth += delta;
        if (previewMonth < 1) { previewMonth = 12; previewYear--; }
        if (previewMonth > 12) { previewMonth = 1; previewYear++; }
        previewDates();
    };

    // ── Сохранение ──

    window.saveSchedule = function () {
        const scheduleId = document.getElementById("modalScheduleId").value;
        const counterId = +document.getElementById("modalCounterId").value;
        const carId = +document.getElementById("modalCarId").value;
        const comment = document.getElementById("modalComment").value.trim();
        const recurring = getRecurringFromModal();

        const isEdit = !!scheduleId;

        if (!isEdit && !carId) {
            const search = document.getElementById("modalCarSearch");
            if (search) {
                search.classList.add("invalid");
                search.focus();
            }
            alert("Оберіть машину");
            return;
        }

        if (!recurring && modalSingleDays.length === 0) {
            alert("Заповніть правила повторення або додайте окремі дні підрахунку");
            return;
        }

        const body = {
            recurring,
            single_days: modalSingleDays.length ? modalSingleDays : null,
            exception_days: modalExceptionDays.length ? modalExceptionDays : null,
            comment: comment || null,
        };

        let url, method;
        if (isEdit) {
            url = `/admin/schedules/api/${scheduleId}/update`;
        } else {
            url = "/admin/schedules/api/create";
            body.counter_id = counterId;
            body.car_id = carId;
            body.type = (typeof ACTIVE_TYPE !== "undefined") ? ACTIVE_TYPE : "whole_day";
        }

        document.getElementById("modalSaveBtn").disabled = true;

        fetch(url, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
        })
            .then(async (r) => {
                let data = {};
                const text = await r.text();
                if (text) {
                    try {
                        data = JSON.parse(text);
                    } catch (e) {
                        if (!r.ok) {
                            throw new Error(`Помилка сервера (${r.status})`);
                        }
                    }
                }
                return { ok: r.ok, data };
            })
            .then(({ ok, data }) => {
                document.getElementById("modalSaveBtn").disabled = false;
                if (!ok) {
                    alert(data.error || data.detail || "Помилка збереження");
                    return;
                }
                closeModal();
                location.reload();
            })
            .catch((err) => {
                document.getElementById("modalSaveBtn").disabled = false;
                alert(err.message || "Помилка мережі");
            });
    };

    window.deleteSchedule = function (scheduleId, carName) {
        if (!confirm(`Видалити прив'язку до «${carName}»?`)) return;

        fetch(`/admin/schedules/api/${scheduleId}/delete`, { method: "POST" })
            .then(r => r.json())
            .then(data => {
                if (data.ok) location.reload();
                else alert(data.error || "Помилка");
            })
            .catch(() => alert("Помилка мережі"));
    };

    // ── Инициализация ──
    initCalendars();

})();
