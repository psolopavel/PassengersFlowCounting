// Presence / «онлайн»: доки вкладка відкрита й видима — шлемо heartbeat.
// Сервер зберігає last_seen_at; онлайн обчислюється як свіжість цього часу.
// Це переживає закриття вкладки, сон ноутбука, обрив мережі та падіння сервера —
// користувач сам «протухає» в офлайн, коли heartbeat перестає надходити.
(function () {
    "use strict";

    var HEARTBEAT_MS = 30000; // частіше за серверний поріг (90с) із запасом
    var timer = null;

    function ping() {
        // keepalive дозволяє запиту долетіти навіть під час вивантаження сторінки.
        fetch("/api/heartbeat", {
            method: "POST",
            credentials: "same-origin",
            keepalive: true,
        }).catch(function () { /* мережеві збої ігноруємо — просто «протухнемо» */ });
    }

    function start() {
        if (timer !== null) return;
        ping();
        timer = window.setInterval(ping, HEARTBEAT_MS);
    }

    function stop() {
        if (timer === null) return;
        window.clearInterval(timer);
        timer = null;
    }

    function onVisibilityChange() {
        if (document.visibilityState === "visible") {
            start();
        } else {
            // Вкладка прихована (інша вкладка/згорнуто) — не рахуємо активним.
            stop();
        }
    }

    function goOffline() {
        stop();
        // sendBeacon надійніший за fetch під час закриття вкладки.
        if (navigator.sendBeacon) {
            navigator.sendBeacon("/api/offline");
        }
    }

    document.addEventListener("visibilitychange", onVisibilityChange);
    window.addEventListener("pagehide", goOffline);

    // Стартуємо, лише якщо вкладка зараз видима.
    if (document.visibilityState === "visible") {
        start();
    }
})();
