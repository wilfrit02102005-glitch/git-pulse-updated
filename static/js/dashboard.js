/* ============================================================
   GitPulse - dashboard page behaviour
   Clickable stat cards, activity/commit/PR/issue filters,
   detail modals, AJAX refresh and repository health analysis.
   ============================================================ */
(function () {
    "use strict";

    // ---------- Modal ----------
    var modal = document.getElementById("gitpulseModal");
    var modalTitle = document.getElementById("gpModalTitle");
    var modalBody = document.getElementById("gpModalBody");
    var modalLink = document.getElementById("gpModalLink");

    function openModal(title, url) {
        if (!modal) return;
        modalTitle.textContent = title;
        modalLink.href = url || "#";
        modal.classList.add("open");
        modal.setAttribute("aria-hidden", "false");
        document.body.classList.add("modal-open-gp");
    }

    function closeModal() {
        if (!modal) return;
        modal.classList.remove("open");
        modal.setAttribute("aria-hidden", "true");
        document.body.classList.remove("modal-open-gp");
    }

    if (modal) {
        modal.addEventListener("click", function (event) {
            if (event.target.closest("[data-gp-close]")) closeModal();
        });
        document.addEventListener("keydown", function (event) {
            if (event.key === "Escape") closeModal();
        });
    }

    function esc(value) {
        return String(value == null ? "" : value)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }

    function renderError(msg) {
        return '<p class="muted mb-0">' + esc(msg) + "</p>";
    }

    // ---------- Tab navigation (mirrors main.js for stat cards) ----------
    function activateTab(targetId) {
        document.querySelectorAll(".tab-pane").forEach(function (pane) {
            pane.classList.toggle("active", pane.id === targetId);
        });
        document.querySelectorAll(".sidebar-nav .nav-link").forEach(function (link) {
            link.classList.toggle("active", link.dataset.tab === targetId);
        });
    }

    document.querySelectorAll(".stat-card-link").forEach(function (card) {
        card.addEventListener("click", function (event) {
            var goto = card.dataset.goto;
            if (!goto) return; // Real page navigation (member cards link to /team-members).
            event.preventDefault();
            activateTab(goto);
        });
    });

    // ---------- Refresh (AJAX) ----------
    var refreshBtn = document.getElementById("refreshBtn");
    if (refreshBtn) {
        refreshBtn.addEventListener("click", function () {
            refreshBtn.disabled = true;
            refreshBtn.textContent = "Refreshing…";
            fetch("/api/refresh", { method: "POST" })
                .then(function (resp) {
                    return resp.json().then(function (payload) {
                        return { ok: resp.ok, payload: payload };
                    });
                })
                .then(function (res) {
                    if (!res.ok) {
                        throw new Error((res.payload && res.payload.error) || "Refresh failed");
                    }
                    refreshBtn.textContent = "✓ Refreshed";
                    setTimeout(function () {
                        window.location.reload();
                    }, 400);
                })
                .catch(function (err) {
                    refreshBtn.disabled = false;
                    refreshBtn.textContent = "↻ Refresh";
                    alert("Could not refresh: " + err.message);
                });
        });
    }

    // ---------- Activity filters (client-side) ----------
    var feed = document.getElementById("activityFeed");
    var categorySel = document.getElementById("activityCategory");
    var authorSel = document.getElementById("activityAuthor");
    var searchInput = document.getElementById("activitySearch");
    var activityCount = document.getElementById("activityCount");
    var applyBtn = document.getElementById("activityApply");

    function applyActivityFilters() {
        if (!feed) return;
        var cat = (categorySel && categorySel.value) || "";
        var author = (authorSel && authorSel.value) || "";
        var query = (searchInput && searchInput.value.trim().toLowerCase()) || "";
        var visible = 0;
        feed.querySelectorAll(".feed-item").forEach(function (item) {
            var show = true;
            if (cat && item.dataset.category !== cat) show = false;
            if (show && author && item.dataset.actor !== author) show = false;
            if (show && query && item.dataset.search.indexOf(query) === -1) show = false;
            item.style.display = show ? "" : "none";
            if (show) visible += 1;
        });
        if (activityCount) activityCount.textContent = String(visible);
    }

    if (applyBtn) applyBtn.addEventListener("click", applyActivityFilters);
    if (categorySel) categorySel.addEventListener("change", applyActivityFilters);
    if (authorSel) authorSel.addEventListener("change", applyActivityFilters);
    if (searchInput) {
        searchInput.addEventListener("input", function () {
            window.clearTimeout(searchInput._timer);
            searchInput._timer = window.setTimeout(applyActivityFilters, 250);
        });
    }

    // ---------- Commits: filter + detail modal ----------
    var commitSearch = document.getElementById("commitSearch");
    var commitAuthor = document.getElementById("commitAuthor");
    var commitsBody = document.getElementById("commitsBody");
    var commitCount = document.getElementById("commitCount");

    function applyCommitFilters() {
        if (!commitsBody) return;
        var query = (commitSearch && commitSearch.value.trim().toLowerCase()) || "";
        var author = (commitAuthor && commitAuthor.value) || "";
        var visible = 0;
        commitsBody.querySelectorAll(".commit-row").forEach(function (row) {
            var show = true;
            if (author && row.dataset.author !== author) show = false;
            if (show && query && row.dataset.search.indexOf(query) === -1) show = false;
            row.style.display = show ? "" : "none";
            if (show) visible += 1;
        });
        if (commitCount) commitCount.textContent = String(visible);
    }

    if (commitSearch) commitSearch.addEventListener("input", applyCommitFilters);
    if (commitAuthor) commitAuthor.addEventListener("change", applyCommitFilters);

    if (commitsBody) {
        commitsBody.addEventListener("click", function (event) {
            if (event.target.closest("a, button")) return;
            var row = event.target.closest(".commit-row");
            if (!row) return;
            fetchCommitDetail(row.dataset.commitSha);
        });
    }

    function fetchCommitDetail(sha) {
        openModal("Commit " + sha, "https://github.com/" + window.GITPULSE_REPO + "/commit/" + sha);
        modalBody.innerHTML = '<p class="muted">Loading commit details…</p>';
        fetch("/api/commit/" + encodeURIComponent(sha))
            .then(function (resp) { return resp.json(); })
            .then(function (data) {
                if (data.error) {
                    modalBody.innerHTML = renderError(data.error);
                    return;
                }
                modalTitle.textContent = esc(data.short_sha || sha) + " · " + esc(data.message);
                var rows = (data.files || []).map(function (f) {
                    return "<tr><td class='file-cell'><code>" + esc(f.filename) + "</code></td>" +
                        "<td class='text-center'><span class='badge badge-glass'>" + esc(f.status) + "</span></td>" +
                        "<td class='text-center text-success'>+" + esc(f.additions) + "</td>" +
                        "<td class='text-center text-danger'>-" + esc(f.deletions) + "</td></tr>";
                }).join("");
                modalBody.innerHTML =
                    "<div class='gp-detail-meta'>" +
                    "<span><strong>Author</strong> @" + esc(data.author_login || data.author || "unknown") + "</span>" +
                    "<span><strong>Date</strong> " + esc((data.date || "").replace("T", " ").slice(0, 19)) + "</span>" +
                    "<span><strong>Changes</strong> +" + esc(data.stats.additions) + " / -" + esc(data.stats.deletions) + "</span>" +
                    "</div>" +
                    "<p class='muted'>" + esc(data.full_message || data.message || "") + "</p>" +
                    "<h3 class='h6 mt-3'>Files changed (" + esc(data.files.length) + ")</h3>" +
                    "<div class='table-responsive'><table class='table table-pulse table-sm align-middle'>" +
                    "<thead><tr><th>File</th><th class='text-center'>Status</th><th class='text-center'>+</th><th class='text-center'>-</th></tr></thead>" +
                    "<tbody>" + rows + "</tbody></table></div>";
            })
            .catch(function () {
                modalBody.innerHTML = renderError("Network error while loading commit details.");
            });
    }

    // ---------- Pull requests: filter + detail modal ----------
    var prSearch = document.getElementById("prSearch");
    var prState = document.getElementById("prState");
    var prAuthor = document.getElementById("prAuthor");
    var prsBody = document.getElementById("prsBody");
    var prCount = document.getElementById("prCount");

    function applyPrFilters() {
        if (!prsBody) return;
        var query = (prSearch && prSearch.value.trim().toLowerCase()) || "";
        var state = (prState && prState.value) || "";
        var author = (prAuthor && prAuthor.value) || "";
        var visible = 0;
        prsBody.querySelectorAll(".pr-row").forEach(function (row) {
            var show = true;
            if (state && row.dataset.state !== state) show = false;
            if (author && row.dataset.author !== author) show = false;
            if (show && query && row.dataset.search.indexOf(query) === -1) show = false;
            row.style.display = show ? "" : "none";
            if (show) visible += 1;
        });
        if (prCount) prCount.textContent = String(visible);
    }

    if (prSearch) prSearch.addEventListener("input", applyPrFilters);
    if (prState) prState.addEventListener("change", applyPrFilters);
    if (prAuthor) prAuthor.addEventListener("change", applyPrFilters);

    if (prsBody) {
        prsBody.addEventListener("click", function (event) {
            if (event.target.closest("a, button")) return;
            var row = event.target.closest(".pr-row");
            if (!row) return;
            fetchPrDetail(row.dataset.pr);
        });
    }

    function fetchPrDetail(number) {
        openModal("Pull Request #" + number, "https://github.com/" + window.GITPULSE_REPO + "/pull/" + number);
        modalBody.innerHTML = '<p class="muted">Loading pull request details…</p>';
        fetch("/api/pull-request/" + encodeURIComponent(number))
            .then(function (resp) { return resp.json(); })
            .then(function (data) {
                if (data.error) {
                    modalBody.innerHTML = renderError(data.error);
                    return;
                }
                var stateBadge = data.merged
                    ? "<span class='badge badge-status st-merged'>Merged</span>"
                    : data.state === "open"
                        ? "<span class='badge badge-status st-open'>Open</span>"
                        : "<span class='badge badge-status st-closed'>Closed</span>";
                modalTitle.textContent = "PR #" + number + " · " + data.title;
                var commits = (data.commits || []).map(function (c) {
                    return "<li><code>" + esc(c.sha) + "</code> " + esc(c.message) + "</li>";
                }).join("");
                var reviews = (data.reviews || []).map(function (r) {
                    return "<li><strong>@" + esc(r.author) + "</strong> (" + esc(r.state) + ") · " + esc((r.submitted_at || "").slice(0, 10)) + "</li>";
                }).join("");
                modalBody.innerHTML =
                    "<div class='gp-detail-meta'>" + stateBadge +
                    "<span><strong>Author</strong> @" + esc(data.author) + "</span>" +
                    "<span><strong>Created</strong> " + esc((data.created_at || "").slice(0, 10)) + "</span>" +
                    "<span><strong>Commits</strong> " + esc(data.commits_count) + "</span>" +
                    "<span><strong>Changes</strong> +" + esc(data.additions) + " / -" + esc(data.deletions) + " (" + esc(data.changed_files) + " files)</span>" +
                    "</div>" +
                    (data.base ? "<p class='muted'>" + esc(data.base) + " ← " + esc(data.head) + "</p>" : "") +
                    (data.body ? "<p class='find-desc'>" + esc(data.body) + "</p>" : "") +
                    (commits ? "<h3 class='h6 mt-3'>Commits</h3><ul class='gp-list'>" + commits + "</ul>" : "") +
                    (reviews ? "<h3 class='h6 mt-3'>Reviews</h3><ul class='gp-list'>" + reviews + "</ul>" : "") +
                    (data.labels && data.labels.length
                        ? "<div class='mt-3'>" + data.labels.map(function (l) { return "<span class='badge badge-glass'>" + esc(l) + "</span> "; }).join("") + "</div>"
                        : "");
            })
            .catch(function () {
                modalBody.innerHTML = renderError("Network error while loading pull request details.");
            });
    }

    // ---------- Issues: fetch, filter, paginate + inline detail ----------
    var issueSearch = document.getElementById("issueSearch");
    var issueState = document.getElementById("issueState");
    var issueAuthor = document.getElementById("issueAuthor");
    var issuesBody = document.getElementById("issuesBody");
    var issueCount = document.getElementById("issueCount");
    var issueFilteredCount = document.getElementById("issueFilteredCount");
    var issuesPagination = document.getElementById("issuesPagination");
    var issuesPrev = document.getElementById("issuesPrev");
    var issuesNext = document.getElementById("issuesNext");
    var issuesPageInfo = document.getElementById("issuesPageInfo");
    var issuesListView = document.getElementById("issuesListView");
    var issueDetailView = document.getElementById("issueDetailView");
    var issueDetailBody = document.getElementById("issueDetailBody");
    var issueOpenGithub = document.getElementById("issueOpenGithub");
    var issueBackBtn = document.getElementById("issueBackBtn");

    var issuesState = { data: [], page: 1, perPage: 25, loading: false, error: false };

    function issueLabelStyle(color) {
        var hex = String(color || "6e7781").replace(/^#/, "");
        if (!/^[0-9a-fA-F]{6}$/.test(hex)) hex = "6e7781";
        var r = parseInt(hex.slice(0, 2), 16);
        var g = parseInt(hex.slice(2, 4), 16);
        var b = parseInt(hex.slice(4, 6), 16);
        var luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255;
        var fg = luminance > 0.6 ? "#1c2333" : "#f2f5fa";
        return "background:rgba(" + r + "," + g + "," + b + ",0.28);color:" + fg + ";border-color:rgba(" + r + "," + g + "," + b + ",0.55);";
    }

    function issueDate(value) {
        return (value || "").slice(0, 10) || "—";
    }

    function issueSeverityClass(severity) {
        var s = String(severity || "").toLowerCase();
        if (s === "critical") return "sev-critical";
        if (s === "high") return "sev-high";
        if (s === "low") return "sev-low";
        return "sev-medium";
    }

    function issueRowHtml(i) {
        var stateBadge = i.state === "open"
            ? "<span class='badge badge-status st-open'>Open</span>"
            : "<span class='badge badge-status st-closed'>Closed</span>";
        var labels = (i.labels || []).map(function (l) {
            return "<span class='issue-label' style='" + issueLabelStyle(l.color) + "'>" + esc(l.name) + "</span>";
        }).join(" ");
        var assignees = (i.assignees && i.assignees.length)
            ? i.assignees.map(function (a) { return "@" + esc(a); }).join(", ")
            : "<span class='muted'>Unassigned</span>";
        var aiCell;
        if (i.ai && i.ai.analyzed) {
            aiCell = "<span class='badge badge-glass badge-sev " + issueSeverityClass(i.ai.severity) + "'>" + esc(i.ai.severity || "Analyzed") + "</span>";
        } else {
            aiCell = "<button class='btn btn-pulse btn-sm ai-issue-btn' data-issue='" + i.number + "'>Analyze</button>";
        }
        return "<tr class='hover-row issue-row' data-issue='" + i.number + "' title='Click to view issue details'>" +
            "<td><a href='" + esc(i.html_url || "#") + "' target='_blank' rel='noopener'>#" + i.number + "</a></td>" +
            "<td class='issue-title'>" + esc(i.title) + "</td>" +
            "<td><strong>@" + esc(i.author || "unknown") + "</strong></td>" +
            "<td>" + (labels || "<span class='muted'>—</span>") + "</td>" +
            "<td>" + stateBadge + "</td>" +
            "<td>" + assignees + "</td>" +
            "<td class='muted'>" + issueDate(i.created_at) + "</td>" +
            "<td class='text-center'>" + aiCell + "</td>" +
            "</tr>";
    }

    function getFilteredIssues() {
        var query = (issueSearch && issueSearch.value.trim().toLowerCase()) || "";
        var state = (issueState && issueState.value) || "";
        var author = (issueAuthor && issueAuthor.value) || "";
        return issuesState.data.filter(function (i) {
            if (state && i.state !== state) return false;
            if (author && i.author !== author) return false;
            if (query) {
                var hay = ((i.title || "") + " " + (i.body || "") + " #" + i.number).toLowerCase();
                if (hay.indexOf(query) === -1) return false;
            }
            return true;
        });
    }

    function renderIssues() {
        if (!issuesBody) return;
        if (issuesState.loading) {
            issuesBody.innerHTML = "<tr><td colspan='8' class='text-center py-4'><div class='loading-spinner'></div><div class='muted small mt-2'>Loading issues…</div></td></tr>";
        } else if (issuesState.error) {
            issuesBody.innerHTML = "<tr><td colspan='8' class='text-center py-4'><p class='muted mb-2'>Unable to load issues from GitHub.</p><button type='button' class='btn btn-pulse btn-sm' id='issuesRetryBtn'>Retry</button></td></tr>";
        } else {
            var filtered = getFilteredIssues();
            var total = filtered.length;
            var pages = Math.max(1, Math.ceil(total / issuesState.perPage));
            if (issuesState.page > pages) issuesState.page = pages;
            var start = (issuesState.page - 1) * issuesState.perPage;
            var slice = filtered.slice(start, start + issuesState.perPage);
            if (total === 0) {
                var msg = issuesState.data.length === 0
                    ? "No issues have been created in this repository yet."
                    : "No issues match your current filters.";
                issuesBody.innerHTML = "<tr><td colspan='8' class='muted text-center py-4'>" + esc(msg) + "</td></tr>";
            } else {
                issuesBody.innerHTML = slice.map(issueRowHtml).join("");
            }
            if (issuesPagination) issuesPagination.hidden = total === 0 || total <= issuesState.perPage;
            if (issuesPageInfo) issuesPageInfo.textContent = "Page " + issuesState.page + " of " + pages;
            if (issuesPrev) issuesPrev.disabled = issuesState.page <= 1;
            if (issuesNext) issuesNext.disabled = issuesState.page >= pages;
        }
        if (issueCount) issueCount.textContent = String(issuesState.data.length);
        if (issueFilteredCount) {
            var totalAll = issuesState.data.length;
            var filteredNow = getFilteredIssues().length;
            issueFilteredCount.textContent = totalAll === 0
                ? ""
                : (filteredNow === totalAll
                    ? totalAll + " issue" + (totalAll === 1 ? "" : "s")
                    : "Showing " + filteredNow + " of " + totalAll + " issues");
        }
    }

    function populateIssueAuthors() {
        if (!issueAuthor) return;
        var authors = [];
        var seen = {};
        issuesState.data.forEach(function (i) {
            var a = i.author || "";
            if (a && !seen[a]) { seen[a] = true; authors.push(a); }
        });
        authors.sort(function (x, y) { return x.toLowerCase() < y.toLowerCase() ? -1 : 1; });
        issueAuthor.innerHTML = "<option value=''>All authors</option>" + authors.map(function (a) {
            return "<option value='" + esc(a) + "'>@" + esc(a) + "</option>";
        }).join("");
    }

    function loadIssues() {
        if (!issuesBody) return;
        issuesState.loading = true;
        issuesState.error = false;
        renderIssues();
        fetch("/api/issues/list")
            .then(function (resp) {
                return resp.json().catch(function () { return null; });
            })
            .then(function (payload) {
                issuesState.loading = false;
                if (!payload || payload.error || !Array.isArray(payload.issues)) {
                    issuesState.error = true;
                    renderIssues();
                    return;
                }
                issuesState.data = payload.issues;
                issuesState.page = 1;
                populateIssueAuthors();
                renderIssues();
            })
            .catch(function () {
                issuesState.loading = false;
                issuesState.error = true;
                renderIssues();
            });
    }

    window.__refreshIssues = loadIssues;

    if (issueSearch) {
        issueSearch.addEventListener("input", function () {
            window.clearTimeout(issueSearch._timer);
            issueSearch._timer = window.setTimeout(function () {
                issuesState.page = 1;
                renderIssues();
            }, 250);
        });
    }
    if (issueState) issueState.addEventListener("change", function () { issuesState.page = 1; renderIssues(); });
    if (issueAuthor) issueAuthor.addEventListener("change", function () { issuesState.page = 1; renderIssues(); });
    if (issuesPrev) issuesPrev.addEventListener("click", function () { issuesState.page -= 1; renderIssues(); });
    if (issuesNext) issuesNext.addEventListener("click", function () { issuesState.page += 1; renderIssues(); });

    if (issuesBody) {
        issuesBody.addEventListener("click", function (event) {
            if (event.target.closest("#issuesRetryBtn")) {
                loadIssues();
                return;
            }
            if (event.target.closest("a, button")) return;
            var row = event.target.closest(".issue-row");
            if (!row) return;
            openIssueDetail(Number(row.dataset.issue));
        });
    }

    if (issueBackBtn) {
        issueBackBtn.addEventListener("click", function () {
            if (issueDetailView) issueDetailView.hidden = true;
            if (issuesListView) issuesListView.hidden = false;
            renderIssues();
        });
    }

    function openIssueDetail(number) {
        if (!issueDetailView || !issuesListView || !issueDetailBody) return;
        issuesListView.hidden = true;
        issueDetailView.hidden = false;
        issueDetailBody.innerHTML = "<div class='text-center py-4'><div class='loading-spinner'></div><div class='muted small mt-2'>Loading issue details…</div></div>";
        fetch("/api/issue/" + encodeURIComponent(number))
            .then(function (resp) {
                return resp.json().catch(function () { return null; });
            })
            .then(function (data) {
                if (!data || data.error) {
                    issueDetailBody.innerHTML = renderError((data && data.error) || "Unable to load issue details from GitHub.");
                    return;
                }
                if (issueOpenGithub) issueOpenGithub.href = data.url || ("https://github.com/" + (window.GITPULSE_REPO || "") + "/issues/" + number);
                renderIssueDetail(data);
            })
            .catch(function () {
                issueDetailBody.innerHTML = renderError("Network error while loading issue details.");
            });
    }

    function renderIssueDetail(data) {
        var stateBadge = data.state === "open"
            ? "<span class='badge badge-status st-open'>Open</span>"
            : "<span class='badge badge-status st-closed'>Closed</span>";
        var labels = (data.labels || []).map(function (l) {
            var name = typeof l === "string" ? l : (l.name || "");
            var color = typeof l === "string" ? "6e7781" : (l.color || "6e7781");
            return "<span class='issue-label' style='" + issueLabelStyle(color) + "'>" + esc(name) + "</span>";
        }).join(" ");
        var assignees = (data.assignees && data.assignees.length)
            ? data.assignees.map(function (a) { return "@" + esc(a); }).join(", ")
            : "<span class='muted'>Unassigned</span>";
        var events = (data.timeline_events || []).map(function (e) {
            return "<li><strong>" + esc(e.event) + "</strong> by @" + esc(e.actor) + " · " + esc((e.date || "").slice(0, 10)) + "</li>";
        }).join("");
        var aiBlock = "";
        if (data.ai) {
            var sev = data.ai.severity || "";
            aiBlock = "<div class='glass card-pulse p-3 mt-3'>" +
                "<div class='d-flex align-items-center gap-2 flex-wrap mb-2'>" +
                "<strong>AI Analysis</strong>" +
                "<span class='badge badge-glass badge-sev " + issueSeverityClass(sev) + "'>" + esc(sev || "Analyzed") + "</span>" +
                "<span class='muted small'>engine: " + esc(data.ai.engine || "rule-based") + "</span>" +
                "</div>" +
                (data.ai.summary ? "<p class='mb-2'><strong>Summary:</strong> " + esc(data.ai.summary) + "</p>" : "") +
                (data.ai.root_cause ? "<p class='mb-2'><strong>Root cause:</strong> " + esc(data.ai.root_cause) + "</p>" : "") +
                (data.ai.solution ? "<p class='mb-2'><strong>Suggested solution:</strong> " + esc(data.ai.solution) + "</p>" : "") +
                ((data.ai.related_files || []).length ? "<p class='mb-0 muted small'><strong>Related files:</strong> " + data.ai.related_files.map(function (f) { return "<code>" + esc(f) + "</code>"; }).join(" ") + "</p>" : "") +
                "</div>";
        } else {
            aiBlock = "<div class='mt-3'><p class='muted small mb-2'>This issue has not been analyzed yet.</p><button class='btn btn-pulse btn-sm ai-issue-btn' data-issue='" + data.number + "'>Run AI Analysis</button></div>";
        }
        issueDetailBody.innerHTML =
            "<h2 class='h4 mb-2'>" + esc(data.title || "") + " " + stateBadge + "</h2>" +
            "<div class='gp-detail-meta'>" +
            "<span><strong>Author</strong> @" + esc(data.author || "unknown") + "</span>" +
            "<span><strong>Created</strong> " + esc((data.created_at || "").slice(0, 10)) + "</span>" +
            "<span><strong>Updated</strong> " + esc((data.updated_at || "").slice(0, 10)) + "</span>" +
            "<span><strong>Comments</strong> " + esc(data.comments_count) + "</span>" +
            "<span><strong>Assigned</strong> " + assignees + "</span>" +
            "</div>" +
            (labels ? "<div class='mt-3'>" + labels + "</div>" : "") +
            (data.body ? "<div class='issue-detail-body mt-3'>" + esc(data.body) + "</div>" : "<p class='muted mt-3 mb-0'>No description provided.</p>") +
            aiBlock +
            (events ? "<h3 class='h6 mt-3'>Timeline</h3><ul class='gp-list'>" + events + "</ul>" : "");
    }

    loadIssues();

    // ---------- Repository health analysis ----------
    var analyzeRepoBtn = document.getElementById("analyzeRepoBtn");
    var repoAnalysisStatus = document.getElementById("repoAnalysisStatus");
    var repoAnalysisResult = document.getElementById("repoAnalysisResult");

    if (analyzeRepoBtn) {
        analyzeRepoBtn.addEventListener("click", function () {
            analyzeRepoBtn.disabled = true;
            analyzeRepoBtn.textContent = "Analyzing…";
            if (repoAnalysisStatus) repoAnalysisStatus.textContent = "Running rule-based analysis…";
            fetch("/api/ai/analyze-repo", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: "{}",
            })
                .then(function (resp) { return resp.json(); })
                .then(function (data) {
                    analyzeRepoBtn.disabled = false;
                    analyzeRepoBtn.textContent = "Analyze Repository";
                    if (data.error) {
                        if (repoAnalysisStatus) repoAnalysisStatus.textContent = "";
                        repoAnalysisResult.innerHTML = renderError(data.error);
                        return;
                    }
                    if (repoAnalysisStatus) repoAnalysisStatus.textContent = "";
                    var score = Number(data.health_score) || 0;
                    var scoreColor = score >= 70 ? "st-open" : (score >= 45 ? "st-merged" : "st-closed");
                    var findings = (data.findings || []).map(function (f) {
                        return "<div class='suggestion-card prio-" + esc(f.severity) + " mb-2'>" +
                            "<div class='sugg-top'>" +
                            "<span class='badge badge-prio'>" + esc(f.severity.toUpperCase()) + "</span>" +
                            "<span class='muted small'>" + esc(f.category) + (f.affected ? " · " + esc(f.affected) : "") + "</span>" +
                            "</div>" +
                            "<h3 class='h6 mb-1'>" + esc(f.title) + "</h3>" +
                            "<p class='sugg-detail muted mb-0'>" + esc(f.explanation) + "</p>" +
                            "<p class='sugg-detail muted mb-0'><strong>Recommendation:</strong> " + esc(f.recommendation) + "</p>" +
                            "</div>";
                    }).join("");
                    var narrative = data.ai_narrative
                        ? "<p class='muted'>" + esc(data.ai_narrative.narrative) + "</p>" +
                          (data.ai_narrative.priorities || []).map(function (p) { return "<li>" + esc(p) + "</li>"; }).join("")
                        : "";
                    repoAnalysisResult.innerHTML =
                        "<div class='glass card-pulse p-3 mb-3'>" +
                        "<div class='d-flex align-items-center gap-3 flex-wrap'>" +
                        "<div class='repo-health-score badge-status " + scoreColor + "'>" + score + "</div>" +
                        "<div><strong>Health Score</strong><div class='muted'>" + esc(data.summary || "") + "</div></div>" +
                        "</div>" +
                        (narrative ? "<div class='mt-2'><ul class='gp-list'>" + narrative + "</ul></div>" : "") +
                        "</div>" +
                        "<h3 class='h6'>Findings (" + esc((data.findings || []).length) + ")</h3>" +
                        (findings || '<p class="muted mb-0">No findings - repository looks healthy.</p>');
                })
                .catch(function () {
                    analyzeRepoBtn.disabled = false;
                    analyzeRepoBtn.textContent = "Analyze Repository";
                    if (repoAnalysisStatus) repoAnalysisStatus.textContent = "";
                    repoAnalysisResult.innerHTML = renderError("Network error while analyzing repository.");
                });
        });
    }

    // ---------- AI Fix form confirmation ----------
    var aiFixForm = document.getElementById("aiFixForm");
    if (aiFixForm) {
        aiFixForm.addEventListener("submit", function (event) {
            var path = aiFixForm.querySelector('[name="path"]');
            var label = aiFixForm.querySelector('[name="issue_label"]');
            var message = "This will commit an AI-generated fix to a new ai-fix/ branch and open a pull request on GitHub. It will NOT merge or touch the default branch.\n\nContinue?";
            if (!window.confirm(message)) {
                event.preventDefault();
                return;
            }
            var btn = document.getElementById("aiFixBtn");
            if (btn) {
                btn.disabled = true;
                btn.textContent = "Creating…";
            }
            void path; void label;
        });
    }

    // ---------- Auto-refresh (every 30s) ----------
    var lastUpdated = document.getElementById("lastUpdatedTime");
    var REFRESH_INTERVAL_MS = 30000;

    function pad2(n) {
        return n < 10 ? "0" + n : String(n);
    }

    function updateLastUpdated() {
        if (!lastUpdated) return;
        var now = new Date();
        lastUpdated.textContent =
            pad2(now.getHours()) + ":" + pad2(now.getMinutes()) + ":" + pad2(now.getSeconds());
    }

    function setText(id, value) {
        var el = document.getElementById(id);
        if (el) el.textContent = String(value == null ? "" : value);
    }

    function setCanvasData(canvasId, key, value) {
        var canvas = document.getElementById(canvasId);
        if (canvas) canvas.dataset[key] = JSON.stringify(value);
    }

    function refreshOverview(overview, languages) {
        if (!overview) return;
        setText("statMembers", overview.members);
        setText("statActiveMembers", overview.active_members || 0);
        setText("statInactiveMembers", overview.inactive_members || 0);
        setText("statCommits", overview.total_commits || 0);
        setText("statOpenPrs", overview.open_prs || 0);
        setText("statMergedPrs", overview.merged_prs || 0);
        setText("statOpenIssues", overview.open_issues || 0);
        setText("statTotalPrs", overview.total_prs || 0);
        setText("statContributors", overview.contributors_count || 0);
        setText("statLines", "+" + (overview.total_additions || 0) + " / \u2212" + (overview.total_deletions || 0));
        setCanvasData("statusChart", "active", overview.active_members || 0);
        setCanvasData("statusChart", "inactive", overview.inactive_members || 0);
        if (languages) {
            setCanvasData("languagesChart", "labels", Object.keys(languages));
            setCanvasData("languagesChart", "values", Object.keys(languages).map(function (k) { return languages[k]; }));
        }
    }

    function refreshMembers(members) {
        if (!members) return;
        setCanvasData("commitsChart", "labels", members.map(function (m) { return m.username; }));
        setCanvasData("commitsChart", "values", members.map(function (m) { return m.commits || 0; }));
        setCanvasData("scoreChart", "labels", members.map(function (m) { return m.username; }));
        setCanvasData("scoreChart", "values", members.map(function (m) { return m.activity_score || 0; }));

        document.querySelectorAll("#tab-members tbody tr[data-username]").forEach(function (row) {
            var match = null;
            for (var i = 0; i < members.length; i += 1) {
                if (members[i].username === row.dataset.username) { match = members[i]; break; }
            }
            if (!match) return;
            var cells = row.querySelectorAll("td");
            if (cells.length < 11) return;
            cells[3].textContent = match.commits || 0;
            cells[4].textContent = match.commits_all_time != null ? match.commits_all_time : (match.commits || 0);
            cells[5].textContent = match.prs_created != null ? match.prs_created : (match.pr_count || 0);
            cells[6].textContent = match.prs_merged || 0;
            cells[7].textContent = match.prs_reviewed || 0;
            cells[8].textContent = match.issues_created != null ? match.issues_created : (match.issue_count || 0);
            var status = (match.activity_status || "INACTIVE").toLowerCase().replace(/ /g, "-");
            var badge = cells[9].querySelector(".badge-status");
            if (badge) {
                badge.className = "badge badge-status st-" + status;
                badge.textContent = match.activity_status || "INACTIVE";
            }
            var small = cells[9].querySelector("small");
            if (small) small.textContent = match.last_active_text || "No activity";
            var fill = cells[10].querySelector(".score-fill");
            var num = cells[10].querySelector(".score-num");
            if (fill) fill.style.width = (match.activity_score || 0) + "%";
            if (num) num.textContent = match.activity_score || 0;
        });
    }

    function refreshDashboard() {
        Promise.all([
            fetch("/api/overview").then(function (r) { return r.json(); }),
            fetch("/api/team/members").then(function (r) { return r.json(); }),
            fetch("/api/commits").then(function (r) { return r.json(); }),
            fetch("/api/pull-requests").then(function (r) { return r.json(); }),
            fetch("/api/issues").then(function (r) { return r.json(); }),
        ]).then(function (results) {
            var overview = results[0];
            var members = results[1];
            var commits = results[2];
            var prs = results[3];
            var issues = results[4];
            if (overview.error) throw new Error(overview.error);
            refreshOverview(overview.overview || {}, overview.languages);
            refreshMembers(members.members || []);
            setText("commitCount", (commits.pushes || []).length);
            setText("prCount", (prs.pull_requests || []).length);
            setText("issueCount", (issues.issues || []).length);
            if (window.GitPulseCharts) window.GitPulseCharts.renderAll();
            updateLastUpdated();
        }).catch(function () {
            // Graceful failure: keep the last good data, just note the miss.
            if (lastUpdated) lastUpdated.textContent = "update failed";
        });
    }

    if (document.getElementById("refreshBtn")) {
        updateLastUpdated();
        window.setInterval(refreshDashboard, REFRESH_INTERVAL_MS);
    }
})();
