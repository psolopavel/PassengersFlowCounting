async function checkHealth() {
    const el = document.getElementById("health");
    try {
        const res = await fetch("/api/health");
        const data = await res.json();
        el.textContent = JSON.stringify(data, null, 2);
    } catch (err) {
        el.textContent = "Помилка запиту: " + err;
    }
}

checkHealth();
