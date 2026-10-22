/** Сторінка /admin/statistics — пресети періоду, сортування та фільтри таблиці. */

function initStatsPeriodForm() {
    const form = document.getElementById("statsPeriodForm");
    const presetInput = document.getElementById("statsPresetInput");
    if (!form || !presetInput) return;

    form.querySelectorAll("[data-preset]").forEach((btn) => {
        btn.addEventListener("click", () => {
            presetInput.value = btn.dataset.preset || "";
            form.submit();
        });
    });

    form.querySelectorAll('input[type="date"]').forEach((input) => {
        input.addEventListener("change", () => {
            presetInput.value = "";
        });
    });
}

function initStatsTable() {
    const root = document.querySelector("[data-stats-table]");
    if (!root) return;

    const table = root.querySelector(".stats-table");
    const tbody = table?.querySelector("tbody");
    if (!tbody) return;

    const rows = Array.from(tbody.querySelectorAll("tr"));
    const controls = Array.from(root.querySelectorAll("[data-stats-filter]"));
    const emptyMsg = root.querySelector("[data-stats-filter-empty]");
    const resetBtn = root.querySelector("[data-stats-filter-reset]");
    const sortBtns = Array.from(table.querySelectorAll("[data-sort]"));

    let sortKey = "count_day";
    let sortDir = "desc";

    const compareValues = (a, b) => {
        if (a === b) return 0;
        if (a === "" || a == null) return 1;
        if (b === "" || b == null) return -1;
        if (sortKey === "time_spent") {
            return Number(a) - Number(b);
        }
        return String(a).localeCompare(String(b), "uk", { numeric: true });
    };

    const rowMatches = (row) =>
        controls.every((ctrl) => {
            const key = ctrl.dataset.statsFilter;
            const val = ctrl.value.trim().toLowerCase();
            if (!val) return true;

            if (key === "search") {
                const data = (row.dataset.search || "").toLowerCase();
                return data.includes(val);
            }
            if (key === "kind") return row.dataset.kind === val;
            if (key === "status") return row.dataset.status === val;
            return true;
        });

    const updateSortIcons = () => {
        sortBtns.forEach((btn) => {
            const icon = btn.querySelector(".stats-sort-icon");
            const active = btn.dataset.sort === sortKey;
            btn.classList.toggle("is-active", active);
            if (icon) {
                icon.textContent = active ? (sortDir === "asc" ? "↑" : "↓") : "";
            }
        });
    };

    const applySort = () => {
        const sorted = rows.slice().sort((left, right) => {
            const attr = sortKey.replace(/_/g, "-");
            const cmp = compareValues(left.dataset[attr], right.dataset[attr]);
            return sortDir === "asc" ? cmp : -cmp;
        });

        sorted.forEach((row) => tbody.appendChild(row));
    };

    const apply = () => {
        let visible = 0;
        rows.forEach((row) => {
            const show = rowMatches(row);
            row.classList.toggle("hidden", !show);
            if (show) visible += 1;
        });

        if (emptyMsg) {
            const hasRows = rows.length > 0;
            emptyMsg.classList.toggle("hidden", !hasRows || visible !== 0);
        }

        applySort();
        updateSortIcons();
    };

    sortBtns.forEach((btn) => {
        btn.addEventListener("click", () => {
            const key = btn.dataset.sort;
            if (sortKey === key) {
                sortDir = sortDir === "asc" ? "desc" : "asc";
            } else {
                sortKey = key;
                sortDir = btn.dataset.defaultDir || "asc";
            }
            apply();
        });
    });

    controls.forEach((ctrl) => {
        const evt = ctrl.tagName === "SELECT" ? "change" : "input";
        ctrl.addEventListener(evt, apply);
    });

    resetBtn?.addEventListener("click", () => {
        controls.forEach((ctrl) => {
            ctrl.value = "";
        });
        apply();
    });

    apply();
}

document.addEventListener("DOMContentLoaded", () => {
    initStatsPeriodForm();
    initStatsTable();
});
