/* ============================================================
   GitPulse - main UI behaviour
   Sidebar toggle, tab navigation, scan button loading state.
   ============================================================ */
(function () {
    "use strict";

    // ---------- Sidebar toggle (mobile) ----------
    const sidebar = document.getElementById("sidebar");
    const sidebarToggle = document.getElementById("sidebarToggle");

    if (sidebarToggle && sidebar) {
        sidebarToggle.addEventListener("click", function () {
            sidebar.classList.toggle("open");
        });
        document.addEventListener("click", function (event) {
            if (
                window.innerWidth <= 992 &&
                sidebar.classList.contains("open") &&
                !sidebar.contains(event.target) &&
                event.target !== sidebarToggle
            ) {
                sidebar.classList.remove("open");
            }
        });
    }

    // ---------- Tab navigation ----------
    const navLinks = document.querySelectorAll(".sidebar-nav .nav-link");
    const panes = document.querySelectorAll(".tab-pane");
    const currentRoute = document.body.dataset.route || "";

    function activateTab(targetId) {
        panes.forEach(function (pane) {
            pane.classList.toggle("active", pane.id === targetId);
        });
        navLinks.forEach(function (link) {
            const isTab = link.dataset.tab === targetId;
            const isPage = link.dataset.page === targetId;
            link.classList.toggle("active", isTab || isPage);
        });
    }

    // Determine which sidebar item should be highlighted for the current page.
    function activeTargetForRoute() {
        if (currentRoute === "dashboard") {
            const hashTab = (window.location.hash || "").slice(1);
            if (hashTab && document.getElementById(hashTab)) {
                return hashTab;
            }
            return "tab-overview";
        }
        if (currentRoute === "team_members") return "page-team-members";
        if (currentRoute === "member_profile") return "page-team-members";
        if (currentRoute === "reports") return "page-reports";
        if (currentRoute === "code_review") return "page-code-review";
        if (currentRoute === "notifications") return "page-notifications";
        if (currentRoute === "settings_page") return "page-settings";
        return null;
    }

    const initialTarget = activeTargetForRoute();
    if (initialTarget) activateTab(initialTarget);

    navLinks.forEach(function (link) {
        link.addEventListener("click", function (event) {
            if (sidebar && window.innerWidth <= 992) sidebar.classList.remove("open");
            const tab = link.dataset.tab;
            if (tab && currentRoute === "dashboard") {
                // Dashboard tabs switch in place (no full page reload).
                event.preventDefault();
                activateTab(tab);
                if (window.history && window.history.replaceState) {
                    window.history.replaceState(null, "", "#" + tab);
                }
            }
            // Tab links clicked from other pages and standalone page links
            // fall through to their normal href navigation.
        });
    });

    // ---------- Scan button loading state ----------
    const scanBtn = document.getElementById("scanBtn");
    if (scanBtn) {
        scanBtn.addEventListener("click", function () {
            scanBtn.disabled = true;
            scanBtn.textContent = "Scanning…";
            // Give the UI a moment to paint before the (slow) network scan.
            setTimeout(function () {
                scanBtn.closest("form").submit();
            }, 50);
        });
    }

    // ---------- Sidebar height ----------
    // The menu scrolls independently inside the sidebar, so the sidebar must
    // always match the visible viewport. Modern browsers use 100dvh via CSS;
    // fall back to window.innerHeight for older ones (handles mobile browser
    // chrome so the bottom menu items are never cut off).
    function fitSidebarHeight() {
        const sidebarEl = document.getElementById("sidebar");
        if (!sidebarEl) return;
        const supportsDvh =
            window.CSS && window.CSS.supports && window.CSS.supports("height", "100dvh");
        if (!supportsDvh) {
            sidebarEl.style.height = window.innerHeight + "px";
        }
    }
    fitSidebarHeight();
    window.addEventListener("resize", fitSidebarHeight);
    window.addEventListener("orientationchange", fitSidebarHeight);

    // ---------- Auto-dismiss alerts ----------
    window.setTimeout(function () {
        document.querySelectorAll(".alert").forEach(function (alert) {
            // Bootstrap's own dismiss method if present.
            var instance = window.bootstrap && window.bootstrap.Alert
                ? window.bootstrap.Alert.getOrCreateInstance(alert)
                : null;
            if (instance) instance.close();
        });
    }, 6000);

    // ---------- Repository selector ----------
    const repoSelect = document.getElementById("repoSelect");

    function selectRepo(repo) {
        if (!repo || repoSelect.disabled) return;
        repoSelect.disabled = true;
        fetch("/api/github/select-repo", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ repo: repo }),
        })
            .then(function (resp) {
                return resp.json().then(function (payload) {
                    return { ok: resp.ok, payload: payload };
                });
            })
            .then(function (res) {
                if (res.ok) {
                    // Reload so every chart/stat/table rebuilds for the repo.
                    window.location.reload();
                } else {
                    repoSelect.disabled = false;
                    alert("Could not select repository: " + (res.payload.error || "unknown error"));
                }
            })
            .catch(function () {
                repoSelect.disabled = false;
                alert("Network error while selecting repository.");
            });
    }

    if (repoSelect) {
        // Load the repositories accessible to the authenticated account.
        fetch("/api/github/repos")
            .then(function (resp) {
                return resp.json().catch(function () { return null; });
            })
            .then(function (payload) {
                if (!payload || payload.error || !Array.isArray(payload.repos)) {
                    return;
                }
                var current = repoSelect.dataset.current || "";
                var repos = payload.repos;
                repoSelect.innerHTML = "";

                if (repos.length === 0) {
                    var none = document.createElement("option");
                    none.value = "";
                    none.textContent = "No repositories accessible";
                    repoSelect.appendChild(none);
                    return;
                }

                repos.forEach(function (r) {
                    var opt = document.createElement("option");
                    opt.value = r.full_name;
                    opt.textContent = r.full_name;
                    repoSelect.appendChild(opt);
                });

                repoSelect.value = current;
                // First visit: pick the most recently updated repo automatically.
                if (!current && repos.length > 0) {
                    selectRepo(repos[0].full_name);
                }
            })
            .catch(function () {
                // Leave the server-rendered option as-is.
            });

        repoSelect.addEventListener("change", function () {
            selectRepo(repoSelect.value);
        });
    }

    // ---------- AI helpers ----------
    function postJSON(url, data) {
        return fetch(url, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(data),
        }).then(function (resp) {
            return resp.json().then(function (payload) {
                return { ok: resp.ok, payload: payload };
            });
        });
    }

    function showResult(title, payload) {
        var problem = payload.problem || payload.summary || payload.error || "No result.";
        var detail = payload.explanation || payload.root_cause || "";
        var suggestions = (payload.suggestions || []).map(function (s) { return "• " + s; }).join("\n");
        alert(title + "\n\n" + problem + (detail ? "\n\n" + detail : "") + (suggestions ? "\n\n" + suggestions : ""));
    }

    // Analyze a pull request or an issue. Uses event delegation so the
    // buttons keep working after the issues table is re-rendered by the
    // client-side Issues view.
    document.addEventListener("click", function (event) {
        var btn = event.target.closest(".ai-pr-btn, .ai-issue-btn");
        if (!btn) return;
        var isPr = btn.classList.contains("ai-pr-btn");
        var number = isPr ? btn.dataset.pr : btn.dataset.issue;
        if (!number) return;
        var endpoint = isPr ? "/api/ai/analyze-pr" : "/api/ai/analyze-issue";
        var label = isPr ? "PR" : "Issue";
        btn.disabled = true;
        btn.textContent = "Analyzing…";
        postJSON(endpoint, { number: number }).then(function (res) {
            btn.disabled = false;
            btn.textContent = "Analyze";
            if (res.ok) {
                showResult("AI Analysis · " + label + " #" + number, res.payload);
                if (typeof window.__refreshIssues === "function") {
                    window.__refreshIssues();
                }
            } else {
                alert("Analysis failed: " + (res.payload.error || "unknown error"));
            }
        }).catch(function () {
            btn.disabled = false;
            btn.textContent = "Analyze";
            alert("Network error while analyzing " + label.toLowerCase() + ".");
        });
    });
})();
