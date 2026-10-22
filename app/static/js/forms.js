/** Общие UI-компоненты форм: звёзды, фото, список камер. */

function initStarRatings() {
    document.querySelectorAll(".star-rating").forEach((wrap) => {
        const input = wrap.querySelector('input[type="hidden"]');
        const stars = wrap.querySelectorAll(".star-btn");
        const clearBtn = wrap.querySelector(".star-clear");
        if (!input || !stars.length) return;

        const setValue = (val, animate = false) => {
            input.value = val || "";
            stars.forEach((btn) => {
                const active = val && Number(btn.dataset.value) <= val;
                btn.classList.toggle("active", active);
                if (animate && active) {
                    btn.classList.remove("pop");
                    void btn.offsetWidth;
                    btn.classList.add("pop");
                }
            });
        };

        setValue(input.value ? Number(input.value) : 0);

        stars.forEach((btn) => {
            btn.addEventListener("click", () => {
                const val = Number(btn.dataset.value);
                setValue(val === Number(input.value) ? 0 : val, true);
            });
            btn.addEventListener("mouseenter", () => {
                const hover = Number(btn.dataset.value);
                stars.forEach((s) => s.classList.toggle("hover", Number(s.dataset.value) <= hover));
            });
        });

        wrap.querySelector(".stars")?.addEventListener("mouseleave", () => {
            stars.forEach((s) => s.classList.remove("hover"));
        });

        clearBtn?.addEventListener("click", () => setValue(0));
    });
}

function initPhotoUploads() {
    document.querySelectorAll(".photo-upload[data-preview]").forEach((wrap) => {
        const input = wrap.querySelector('input[type="file"]');
        const filename = wrap.querySelector(".photo-filename");
        const previewWrap = wrap.querySelector(".photo-preview-wrap");
        const previewImg = wrap.querySelector(".photo-preview-img");
        if (!input) return;

        input.addEventListener("change", () => {
            const file = input.files?.[0];
            if (!file) {
                if (filename) filename.textContent = "Файл не обрано";
                return;
            }
            if (filename) filename.textContent = file.name;
            const reader = new FileReader();
            reader.onload = (e) => {
                if (previewImg) previewImg.src = e.target.result;
                previewWrap?.classList.remove("hidden");
            };
            reader.readAsDataURL(file);
        });
    });
}

function createCameraRow(id = "", orientation = "landscape") {
    const row = document.createElement("div");
    row.className = "camera-row";
    const esc = (s) => s.replace(/"/g, "&quot;");
    row.innerHTML = `
        <input type="text" name="camera_id" value="${esc(id)}" placeholder="ID камери" required maxlength="32">
        <select name="camera_orientation" title="Орієнтація">
            <option value="landscape"${orientation === "landscape" ? " selected" : ""}>Горизонтальна</option>
            <option value="portrait"${orientation === "portrait" ? " selected" : ""}>Вертикальна</option>
        </select>
        <button type="button" class="btn btn-sm btn-danger camera-remove" title="Видалити">✕</button>
    `;
    row.querySelector(".camera-remove").addEventListener("click", () => row.remove());
    return row;
}

function initCameraLists() {
    const list = document.getElementById("camera-list");
    const addBtn = document.getElementById("camera-add");
    if (!list || !addBtn) return;

    list.querySelectorAll(".camera-remove").forEach((btn) => {
        btn.addEventListener("click", () => btn.closest(".camera-row")?.remove());
    });

    addBtn.addEventListener("click", () => {
        const row = createCameraRow("");
        list.appendChild(row);
        row.querySelector("input")?.focus();
    });
}

function initSearchableSelects() {
    document.querySelectorAll(".searchable-select").forEach((wrap) => {
        const hidden = wrap.querySelector('input[type="hidden"]');
        const search = wrap.querySelector(".searchable-select-search");
        const list = wrap.querySelector(".searchable-select-list");
        if (!hidden || !search || !list) return;

        const items = [...list.querySelectorAll(".searchable-select-option")];
        const required = wrap.dataset.required === "true";
        const form = wrap.closest("form");

        const optionLabel = (item) => item.textContent.trim();

        const clearSelection = () => {
            hidden.value = "";
            items.forEach((item) => {
                item.classList.remove("selected");
                item.removeAttribute("aria-selected");
            });
        };

        const selectItem = (item) => {
            hidden.value = item.dataset.value;
            search.value = optionLabel(item);
            items.forEach((i) => {
                i.classList.toggle("selected", i === item);
                i.setAttribute("aria-selected", i === item ? "true" : "false");
            });
            list.classList.add("hidden");
            search.setAttribute("aria-expanded", "false");
            search.classList.remove("invalid");
        };

        const showList = () => {
            list.classList.remove("hidden");
            search.setAttribute("aria-expanded", "true");
        };

        const filterItems = (query) => {
            const q = query.trim().toLowerCase();
            let visible = 0;
            items.forEach((item) => {
                const haystack = item.dataset.search || optionLabel(item).toLowerCase();
                const match = !q || haystack.includes(q);
                item.classList.toggle("hidden", !match);
                if (match) visible += 1;
            });
            list.classList.toggle("empty", visible === 0);
            showList();
        };

        const preselected = items.find((item) => item.dataset.selected === "true");
        if (preselected) {
            selectItem(preselected);
        }

        search.addEventListener("input", () => {
            clearSelection();
            filterItems(search.value);
        });

        search.addEventListener("focus", () => filterItems(search.value));

        search.addEventListener("keydown", (event) => {
            const visible = items.filter((item) => !item.classList.contains("hidden"));
            if (event.key === "Escape") {
                list.classList.add("hidden");
                search.setAttribute("aria-expanded", "false");
                return;
            }
            if (event.key === "ArrowDown") {
                event.preventDefault();
                if (list.classList.contains("hidden")) filterItems(search.value);
                const current = visible.findIndex((item) => item.classList.contains("selected"));
                const next = visible[Math.min(current + 1, visible.length - 1)] || visible[0];
                if (next) {
                    items.forEach((i) => i.classList.remove("selected"));
                    next.classList.add("selected");
                    next.scrollIntoView({ block: "nearest" });
                }
                return;
            }
            if (event.key === "ArrowUp") {
                event.preventDefault();
                const current = visible.findIndex((item) => item.classList.contains("selected"));
                const prev = visible[Math.max(current - 1, 0)] || visible[0];
                if (prev) {
                    items.forEach((i) => i.classList.remove("selected"));
                    prev.classList.add("selected");
                    prev.scrollIntoView({ block: "nearest" });
                }
                return;
            }
            if (event.key === "Enter") {
                event.preventDefault();
                const picked = visible.find((item) => item.classList.contains("selected")) || visible[0];
                if (picked) selectItem(picked);
            }
        });

        items.forEach((item) => {
            item.addEventListener("mousedown", (event) => {
                event.preventDefault();
                selectItem(item);
            });
        });

        document.addEventListener("click", (event) => {
            if (!wrap.contains(event.target)) {
                list.classList.add("hidden");
                search.setAttribute("aria-expanded", "false");
            }
        });

        form?.addEventListener("submit", (event) => {
            if (!required || hidden.value) return;
            event.preventDefault();
            search.classList.add("invalid");
            search.focus();
            filterItems(search.value);
        });
    });
}

function initCountSourcePriority() {
    const source = document.getElementById("count_source");
    const wrap = document.getElementById("source_priority_wrap");
    if (!source || !wrap) return;
    const toggle = () => {
        wrap.classList.toggle("hidden", source.value !== "photoAndVideo");
    };
    source.addEventListener("change", toggle);
    toggle();
}

function initIntercityMaxPeople() {
    const wrap = document.getElementById("max_people_wrap");
    const radios = document.querySelectorAll('input[name="route_kind"]');
    if (!wrap || !radios.length) return;
    const toggle = () => {
        const selected = document.querySelector('input[name="route_kind"]:checked');
        wrap.classList.toggle("hidden", !selected || selected.value !== "intercity");
    };
    radios.forEach((radio) => radio.addEventListener("change", toggle));
    toggle();
}

document.addEventListener("DOMContentLoaded", () => {
    initStarRatings();
    initPhotoUploads();
    initCameraLists();
    initSearchableSelects();
    initCountSourcePriority();
    initIntercityMaxPeople();
});
