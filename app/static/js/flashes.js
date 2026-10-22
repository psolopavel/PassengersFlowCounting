/** Автоприховування flash-повідомлень через 7 с. */
(function () {
    const HIDE_AFTER_MS = 7000;
    const FADE_MS = 400;

    function dismissFlash(el) {
        el.classList.add("is-hiding");
        setTimeout(() => {
            const box = el.closest(".flashes");
            el.remove();
            if (box && !box.querySelector(".flash")) box.remove();
        }, FADE_MS);
    }

    function initFlashes() {
        document.querySelectorAll(".flash:not([data-flash-dismiss])").forEach((el) => {
            el.dataset.flashDismiss = "1";
            setTimeout(() => dismissFlash(el), HIDE_AFTER_MS);
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", initFlashes);
    } else {
        initFlashes();
    }
})();
