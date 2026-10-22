/** Страница тарифов машины: раскрытие маршрутов и редактор периодов. */

function toggleRoute(head) {
    const body = head.nextElementSibling;
    if (!body) return;
    const open = body.classList.toggle("hidden");
    head.classList.toggle("open", !open);
}

function initRouteToggles() {
    document.querySelectorAll(".tariff-route-head[data-toggle]").forEach((head) => {
        head.addEventListener("click", () => toggleRoute(head));
    });
}

function wireRemove(row) {
    row.querySelector(".period-remove")?.addEventListener("click", () => row.remove());
}

function initPeriodEditors() {
    document.querySelectorAll(".period-form").forEach((form) => {
        const list = form.querySelector("[data-periods]");
        const template = form.querySelector(".period-template");
        const addBtn = form.querySelector(".period-add");
        if (!list) return;

        list.querySelectorAll(".period-row").forEach(wireRemove);

        addBtn?.addEventListener("click", () => {
            if (!template) return;
            const row = template.content.firstElementChild.cloneNode(true);
            list.appendChild(row);
            wireRemove(row);
            row.querySelector('input[type="date"]')?.focus();
        });
    });
}

function openHashRoute() {
    if (!window.location.hash) return;
    const target = document.querySelector(window.location.hash);
    if (!target || !target.classList.contains("tariff-route")) return;
    const head = target.querySelector(".tariff-route-head");
    const body = target.querySelector(".tariff-route-body");
    if (body?.classList.contains("hidden")) toggleRoute(head);
    target.scrollIntoView({ behavior: "smooth", block: "center" });
}

document.addEventListener("DOMContentLoaded", () => {
    initRouteToggles();
    initPeriodEditors();
    openHashRoute();
});
