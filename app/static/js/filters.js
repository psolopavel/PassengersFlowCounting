/** Клиентская фильтрация таблиц-списков (поиск + выпадающие фильтры). */

function initTableFilters() {
    document.querySelectorAll("[data-filterable]").forEach((root) => {
        const table = root.querySelector("table");
        if (!table) return;

        const rows = Array.from(table.querySelectorAll("tbody tr"));
        const controls = Array.from(root.querySelectorAll("[data-filter]"));
        const emptyMsg = root.querySelector("[data-filter-empty]");
        const resetBtn = root.querySelector("[data-filter-reset]");

        const rowMatches = (row) =>
            controls.every((ctrl) => {
                const key = ctrl.dataset.filter;
                const val = ctrl.value.trim().toLowerCase();
                if (!val) return true;

                const data = (row.dataset[key] || "").toLowerCase();
                if (ctrl.dataset.match === "includes") return data.includes(val);
                if (ctrl.dataset.match === "list") {
                    return data.split(",").filter(Boolean).includes(val);
                }
                return data === val;
            });

        const apply = () => {
            let visible = 0;
            rows.forEach((row) => {
                const show = rowMatches(row);
                row.classList.toggle("hidden", !show);
                if (show) visible += 1;
            });
            if (emptyMsg) emptyMsg.classList.toggle("hidden", visible !== 0);
        };

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
    });
}

document.addEventListener("DOMContentLoaded", initTableFilters);
