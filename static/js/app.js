/**
 * EventHub — Minimal client-side JS
 * Handles: Bootstrap toast init, file-drop UX, subtle animations.
 */

document.addEventListener("DOMContentLoaded", () => {
    // -----------------------------------------------------------------------
    // 1. Bootstrap Toasts — auto-show
    // -----------------------------------------------------------------------
    document.querySelectorAll(".toast.show").forEach((toastEl) => {
        const toast = new bootstrap.Toast(toastEl);
        toast.show();
    });

    // -----------------------------------------------------------------------
    // 2. File Drop Zone UX
    // -----------------------------------------------------------------------
    const dropZone = document.getElementById("fileDropZone");
    const fileInput = document.getElementById("fileInput");
    const dropContent = document.getElementById("fileDropContent");

    if (dropZone && fileInput) {
        // Click to open file picker
        dropZone.addEventListener("click", () => fileInput.click());

        // Prevent default drag behaviors
        ["dragenter", "dragover", "dragleave", "drop"].forEach((evt) => {
            dropZone.addEventListener(evt, (e) => {
                e.preventDefault();
                e.stopPropagation();
            });
        });

        // Highlight on drag
        ["dragenter", "dragover"].forEach((evt) => {
            dropZone.addEventListener(evt, () => dropZone.classList.add("eh-file-drop--active"));
        });
        ["dragleave", "drop"].forEach((evt) => {
            dropZone.addEventListener(evt, () => dropZone.classList.remove("eh-file-drop--active"));
        });

        // Handle drop
        dropZone.addEventListener("drop", (e) => {
            const files = e.dataTransfer.files;
            if (files.length > 0) {
                fileInput.files = files;
                showFileName(files[0].name);
            }
        });

        // Handle file input change
        fileInput.addEventListener("change", () => {
            if (fileInput.files.length > 0) {
                showFileName(fileInput.files[0].name);
            }
        });

        function showFileName(name) {
            if (dropContent) {
                dropContent.innerHTML = `
                    <i class="bi bi-file-earmark-check-fill eh-file-icon text-accent"></i>
                    <p class="eh-file-text fw-semibold">${name}</p>
                    <p class="eh-file-hint">Click or drop to replace</p>
                `;
            }
        }
    }

    // -----------------------------------------------------------------------
    // 3. Animate KPI cards on scroll
    // -----------------------------------------------------------------------
    const kpiCards = document.querySelectorAll(".eh-kpi-card");
    if (kpiCards.length > 0) {
        const observer = new IntersectionObserver(
            (entries) => {
                entries.forEach((entry) => {
                    if (entry.isIntersecting) {
                        entry.target.classList.add("eh-kpi-visible");
                        observer.unobserve(entry.target);
                    }
                });
            },
            { threshold: 0.2 }
        );
        kpiCards.forEach((card) => observer.observe(card));
    }

    // -----------------------------------------------------------------------
    // 4. Animate event cards on scroll
    // -----------------------------------------------------------------------
    const eventCards = document.querySelectorAll(".eh-event-card");
    if (eventCards.length > 0) {
        const cardObserver = new IntersectionObserver(
            (entries) => {
                entries.forEach((entry, index) => {
                    if (entry.isIntersecting) {
                        setTimeout(() => {
                            entry.target.classList.add("eh-card-visible");
                        }, index * 80);
                        cardObserver.unobserve(entry.target);
                    }
                });
            },
            { threshold: 0.1 }
        );
        eventCards.forEach((card) => cardObserver.observe(card));
    }
});
