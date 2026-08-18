/* ============================================================
   GitPulse - Chart.js renderers
   Reads data from data-* attributes on the canvas elements so the
   templates stay clean and free of inline script data.

   Charts are registered under window.GitPulseCharts so dashboard.js
   can re-render them in place after an AJAX refresh (the renderer
   re-reads the data-* attributes, so callers just need to update
   those attributes first).
   ============================================================ */
(function () {
    "use strict";

    var chartDefaults = {
        color: "#9aa7bd",
        font: { family: "Inter", size: 12 },
        grid: { color: "rgba(255,255,255,0.06)" },
    };

    Chart.defaults.color = chartDefaults.color;
    Chart.defaults.font.family = chartDefaults.font.family;

    var PALETTE = [
        "#7c5cff", "#00d4ff", "#2ecc71", "#f39c12", "#ff5252",
        "#4aa8ff", "#9b6cff", "#2ecc9c", "#e67e22", "#f1c40f",
    ];

    function readJson(canvas, key, fallback) {
        try {
            return JSON.parse(canvas.dataset[key] || "[]");
        } catch (err) {
            console.error("GitPulse: bad " + key + " chart data", err);
            return fallback;
        }
    }

    function toggleEmpty(emptyId, show) {
        var empty = document.getElementById(emptyId);
        if (empty) empty.classList.toggle("show", show);
    }

    // Each renderer: (canvas, emptyId) => void.
    // Re-runs on every refresh; it must destroy/recreate or Chart.js will
    // stack instances. We destroy the previous instance up front.
    var renderers = {};

    function register(id, emptyId, renderer) {
        renderers[id] = function () {
            var canvas = document.getElementById(id);
            if (!canvas) return;
            var existing = Chart.getChart(canvas);
            if (existing) existing.destroy();
            toggleEmpty(emptyId || "", false);
            renderer(canvas);
        };
    }

    // ---------- Languages doughnut ----------
    register("languagesChart", "langEmpty", function (canvas) {
        var labels = readJson(canvas, "labels", []);
        var values = readJson(canvas, "values", []);
        if (values.length === 0) {
            toggleEmpty("langEmpty", true);
            return;
        }
        new Chart(canvas, {
            type: "doughnut",
            data: {
                labels: labels,
                datasets: [{
                    data: values,
                    backgroundColor: labels.map(function (_, i) {
                        return PALETTE[i % PALETTE.length];
                    }),
                    borderWidth: 0,
                    hoverOffset: 8,
                }],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                cutout: "62%",
                plugins: {
                    legend: {
                        position: "bottom",
                        labels: { padding: 14, usePointStyle: true, pointStyle: "circle" },
                    },
                },
            },
        });
    });

    // ---------- Commits per member bar ----------
    register("commitsChart", null, function (canvas) {
        var labels = readJson(canvas, "labels", []);
        var values = readJson(canvas, "values", []);
        if (values.length === 0) return;
        new Chart(canvas, {
            type: "bar",
            data: {
                labels: labels,
                datasets: [{
                    label: "Commits",
                    data: values,
                    backgroundColor: "rgba(124, 92, 255, 0.55)",
                    borderColor: "#7c5cff",
                    borderWidth: 1,
                    borderRadius: 8,
                }],
            },
            options: {
                indexAxis: "y",
                responsive: true,
                maintainAspectRatio: false,
                scales: {
                    x: { beginAtZero: true, grid: chartDefaults.grid, ticks: { precision: 0 } },
                    y: { grid: { display: false } },
                },
                plugins: {
                    legend: { display: false },
                },
            },
        });
    });

    // ---------- Activity score per member bar ----------
    register("scoreChart", null, function (canvas) {
        var labels = readJson(canvas, "labels", []);
        var values = readJson(canvas, "values", []);
        if (values.length === 0) return;
        new Chart(canvas, {
            type: "bar",
            data: {
                labels: labels,
                datasets: [{
                    label: "Activity score",
                    data: values,
                    backgroundColor: "rgba(0, 212, 255, 0.55)",
                    borderColor: "#00d4ff",
                    borderWidth: 1,
                    borderRadius: 8,
                }],
            },
            options: {
                indexAxis: "y",
                responsive: true,
                maintainAspectRatio: false,
                scales: {
                    x: { min: 0, max: 100, grid: chartDefaults.grid, ticks: { precision: 0 } },
                    y: { grid: { display: false } },
                },
                plugins: {
                    legend: { display: false },
                },
            },
        });
    });

    // ---------- Member status doughnut (Active / Inactive) ----------
    register("statusChart", "statusEmpty", function (canvas) {
        var active = parseInt(canvas.dataset.active || "0", 10);
        var inactive = parseInt(canvas.dataset.inactive || "0", 10);
        var values = [active, inactive];
        if (active + inactive === 0) {
            toggleEmpty("statusEmpty", true);
            return;
        }
        new Chart(canvas, {
            type: "doughnut",
            data: {
                labels: ["Active", "Inactive"],
                datasets: [{
                    data: values,
                    backgroundColor: ["#2ecc71", "#ff5252"],
                    borderWidth: 0,
                    hoverOffset: 8,
                }],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                cutout: "60%",
                plugins: {
                    legend: {
                        position: "bottom",
                        labels: { padding: 12, usePointStyle: true, pointStyle: "circle" },
                    },
                },
            },
        });
    });

    // ---------- Team Reports: commits per member bar ----------
    register("reportsCommitsChart", "reportsCommitsEmpty", function (canvas) {
        var labels = readJson(canvas, "labels", []);
        var values = readJson(canvas, "values", []);
        if (values.length === 0) {
            toggleEmpty("reportsCommitsEmpty", true);
            return;
        }
        new Chart(canvas, {
            type: "bar",
            data: {
                labels: labels,
                datasets: [{
                    label: "Commits",
                    data: values,
                    backgroundColor: "rgba(124, 92, 255, 0.55)",
                    borderColor: "#7c5cff",
                    borderWidth: 1,
                    borderRadius: 8,
                }],
            },
            options: {
                indexAxis: "y",
                responsive: true,
                maintainAspectRatio: false,
                scales: {
                    x: { beginAtZero: true, grid: chartDefaults.grid, ticks: { precision: 0 } },
                    y: { grid: { display: false } },
                },
                plugins: { legend: { display: false } },
            },
        });
    });

    // ---------- Team Reports: activity score per member bar ----------
    register("reportsScoreChart", "reportsScoreEmpty", function (canvas) {
        var labels = readJson(canvas, "labels", []);
        var values = readJson(canvas, "values", []);
        if (values.length === 0) {
            toggleEmpty("reportsScoreEmpty", true);
            return;
        }
        new Chart(canvas, {
            type: "bar",
            data: {
                labels: labels,
                datasets: [{
                    label: "Activity score",
                    data: values,
                    backgroundColor: "rgba(0, 212, 255, 0.55)",
                    borderColor: "#00d4ff",
                    borderWidth: 1,
                    borderRadius: 8,
                }],
            },
            options: {
                indexAxis: "y",
                responsive: true,
                maintainAspectRatio: false,
                scales: {
                    x: { min: 0, max: 100, grid: chartDefaults.grid, ticks: { precision: 0 } },
                    y: { grid: { display: false } },
                },
                plugins: { legend: { display: false } },
            },
        });
    });

    // ---------- Team Reports: weekly activity line ----------
    register("reportsWeeklyChart", "reportsWeeklyEmpty", function (canvas) {
        var labels = readJson(canvas, "labels", []);
        var values = readJson(canvas, "values", []);
        if (values.length === 0) {
            toggleEmpty("reportsWeeklyEmpty", true);
            return;
        }
        new Chart(canvas, {
            type: "line",
            data: {
                labels: labels,
                datasets: [{
                    label: "Events",
                    data: values,
                    borderColor: "#2ecc71",
                    backgroundColor: "rgba(46, 204, 113, 0.15)",
                    fill: true,
                    tension: 0.3,
                    pointBackgroundColor: "#2ecc71",
                    borderWidth: 2,
                }],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                scales: {
                    x: { grid: { display: false } },
                    y: { beginAtZero: true, grid: chartDefaults.grid, ticks: { precision: 0 } },
                },
                plugins: { legend: { display: false } },
            },
        });
    });

    // ---------- Member page language doughnut ----------
    register("langMemberChart", "langMemberEmpty", function (canvas) {
        var labels = readJson(canvas, "labels", []);
        var values = readJson(canvas, "values", []);
        if (values.length === 0) {
            toggleEmpty("langMemberEmpty", true);
            return;
        }
        new Chart(canvas, {
            type: "doughnut",
            data: {
                labels: labels,
                datasets: [{
                    data: values,
                    backgroundColor: labels.map(function (_, i) {
                        return PALETTE[i % PALETTE.length];
                    }),
                    borderWidth: 0,
                    hoverOffset: 8,
                }],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                cutout: "62%",
                plugins: {
                    legend: {
                        position: "bottom",
                        labels: { padding: 14, usePointStyle: true, pointStyle: "circle" },
                    },
                },
            },
        });
    });

    window.GitPulseCharts = {
        renderAll: function () {
            Object.keys(renderers).forEach(function (id) {
                if (document.getElementById(id)) renderers[id]();
            });
        },
    };

    window.GitPulseCharts.renderAll();
})();
