# GitPulse — GitHub Team Intelligence Platform

GitPulse is a **Flask** web application that helps engineering managers monitor
GitHub team activity, analyze developer productivity, detect insecure code
patterns, and generate AI-powered coaching suggestions — all behind a modern
dark glassmorphism dashboard.

![Version](https://img.shields.io/badge/version-1.0.0-blue)
![Python](https://img.shields.io/badge/python-3.12-green)
![License](https://img.shields.io/badge/license-MIT-lightgrey)

---

## Features

| Feature | Description |
|---|---|
| 🔐 **GitHub OAuth login** | Standard OAuth flow via Authlib. |
| 🔑 **PAT login** | Paste a Personal Access Token instead of using OAuth. |
| 🚪 **Access control** | Restrict login to an `ALLOWED_GITHUB_USERS` allow-list. |
| 📂 **Dynamic repository selection** | Each logged-in user picks any repository they can access; the choice is stored per session and drives every page. |
| 📊 **Team dashboard** | Members, commits, PRs, issues, last activity, activity score. |
| ✨ **AI coaching** | Per-developer suggestions from Claude with rule-based fallback. |
| 🛡️ **Security scanner** | Regex-based static scan (secrets, injection, bad patterns). |
| 🧊 **Glassmorphism UI** | Dark theme, responsive, animated, zero inline CSS. |
| 🧾 **Structured logging** | Separate logs for auth, GitHub API, scanner and app errors. |

---

## Tech Stack

- **Backend:** Python 3.12+, Flask 3, Requests
- **Auth:** Authlib (GitHub OAuth), personal access tokens
- **Frontend:** HTML5, CSS3, Bootstrap 5, JavaScript, Chart.js
- **AI:** Anthropic Claude API (optional) + rule-based fallback
- **Security:** Regex-based static code scanner
- **Deploy:** Render, Railway, Docker (Dockerfile + docker-compose + gunicorn)

---

## Folder Structure

```
github_team_monitor/
│
├── app.py                     # Flask entry point (app factory, routes, errors)
├── requirements.txt           # Python dependencies
├── README.md                  # This file
├── .env.example               # Template for environment variables
├── .gitignore                 # Files to exclude from version control
├── .dockerignore              # Files to exclude from the Docker image
├── Dockerfile                 # Multi-stage production image
├── docker-compose.yml         # One-command local deployment
├── gunicorn.conf.py           # Production WSGI server configuration
│
├── config/
│   ├── settings.py            # Centralized environment configuration
│   └── logging_setup.py       # Rotating log files + console logging
│
├── utils/
│   ├── github_api.py          # GitHub REST API wrapper + activity scoring
│   ├── ai_analyzer.py         # Claude + rule-based coaching engine
│   ├── code_scanner.py        # Regex static scanner (10 rules)
│   └── auth.py                # OAuth setup, session guards, rate limiting
│
├── templates/
│   ├── base.html              # Layout: sidebar, topbar, flash, error pages
│   ├── login.html             # OAuth + PAT login form
│   ├── dashboard.html         # Four-tab dashboard
│   └── unauthorized.html      # 403 page for non-allow-listed users
│
├── static/
│   ├── css/style.css          # Dark glassmorphism theme
│   ├── js/main.js             # Tabs, sidebar, scan button, alerts
│   ├── js/charts.js           # Chart.js renderers
│   └── images/                # Default avatar + screenshots
│
└── logs/                      # Generated at runtime (git-ignored)
    ├── app.log
    ├── auth.log
    ├── github.log
    └── scanner.log
```

---

## Getting Started

### 1. Prerequisites

- Python **3.12+**
- A GitHub account
- (Optional) A GitHub OAuth App + (optional) Anthropic API key

### 2. Clone and set up the environment

```bash
git clone <your-repo-url> github_team_monitor
cd github_team_monitor

# Create and activate a virtual environment
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 3. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env` and fill in your values (see table below).

### 4. Run the app

```bash
python app.py
```

Open http://localhost:5000

---

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `SECRET_KEY` | ✅ (prod) | `dev-insecure-secret` | Flask session signing key. Generate: `python -c "import secrets; print(secrets.token_hex(32))"` |
| `FLASK_ENV` | no | `development` | `development` or `production`. |
| `FLASK_DEBUG` | no | `0` | Set `1` only for local development. |
| `BASE_URL` | no | `http://localhost:5000` | Public URL of the app (OAuth redirects). |
| `GITHUB_CLIENT_ID` | for OAuth | — | From https://github.com/settings/developers |
| `GITHUB_CLIENT_SECRET` | for OAuth | — | OAuth App secret. |
| `GITHUB_REDIRECT_URI` | for OAuth | `{BASE_URL}/auth/callback` | Must match the callback URL in the OAuth app. |
| `ALLOWED_GITHUB_USERS` | ✅ (prod) | empty (allow all) | Comma-separated usernames, e.g. `alice,bob,charlie`. |
| `GITHUB_OWNER` | no | — | Optional bootstrap default; users pick their own repo after login. |
| `GITHUB_REPO` | no | — | Optional bootstrap default; users pick their own repo after login. |
| `ANTHROPIC_API_KEY` | no | — | If set, AI coaching uses Claude; otherwise rule-based. |
| `ANTHROPIC_MODEL` | no | `claude-3-5-haiku-20241022` | Claude model identifier. |
| `LOG_LEVEL` | no | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`. |
| `LOG_DIR` | no | `logs` | Where log files are written. |
| `RATE_LIMIT_MAX_ATTEMPTS` | no | `5` | Max PAT login attempts per IP per window. |
| `RATE_LIMIT_WINDOW_SECONDS` | no | `300` | Rate-limit window length. |

### Token requirements

The token (OAuth or PAT) needs the `repo` scope to read private repositories.
For public repos, a fine-grained token with **read-only Contents** access is enough.

---

## Project Workflow

```
Login (OAuth / PAT)
   │
   ├─ ALLOWED_GITHUB_USERS check ──┐
   │                               ├──→ 403 unauthorized.html
   │         not allowed           │
   └─ allowed ─────────────────────┘
   │
   ▼
Token stored encrypted in the signed session cookie
   │
   ▼
Repository selection (top bar) ──► /api/github/repos (this account's repos)
   │                                 └─ POST /api/github/select-repo → stored in session
   ▼
Dashboard (/dashboard) ──loads──► GitHubAPI.build_team_report()
                                       ├─ collaborators ──────────────┐
                                       ├─ team members  ──────────────┼──► member metrics + activity score
                                       ├─ repo owner    ──────────────┘
                                       ├─ commits 90d   (all-time total)
                                       ├─ open PRs
                                       ├─ open issues
                                       └─ languages

   │
   ▼
AI Suggestions ──► generate_suggestions(members)
                      ├─ ANTHROPIC_API_KEY set? ──► Claude JSON coaching
                      └─ else ─────────────────────► rule-based coaching
   │
   ▼
Security Scanner ──► POST /dashboard/scan
                      ├─ target=repo  ──► scan_github_repo() (git trees API)
                      └─ target=local ──► scan_path() (local source tree)
```

---

## Scanner Rules

| Rule ID | Severity | Languages | Description |
|---|---|---|---|
| `HARDCODED_SECRET` | CRITICAL | all | Password / API key / token in source. |
| `SQL_INJECTION` | HIGH | all | String-built SQL queries. |
| `EVAL_USAGE` | HIGH | py/js/ts | `eval()` of runtime data. |
| `SHELL_INJECTION` | CRITICAL | all | `os.system`, `shell=True`, `exec`. |
| `BARE_EXCEPT` | MEDIUM | py | Bare `except:` clauses. |
| `MUTABLE_DEFAULT` | MEDIUM | py | `def f(x=[])` shared state. |
| `DANGEROUSLYSETHTML` | HIGH | js/ts | `innerHTML` / `dangerouslySetInnerHTML`. |
| `CONSOLE_LOG` | LOW | js/ts | Debug logging in production code. |
| `PRINT_DEBUG` | LOW | py/java/go/rb | Print-debug statements. |
| `TODO_FIXME` | LOW | all | Unfinished-work markers. |

---

## Activity Score

A transparent 0–100 score computed per developer:

| Component | Max points |
|---|---|
| Commits (capped at 50) | 50 |
| Open PRs (capped at 5) | 25 |
| Open issues (capped at 3) | 15 |
| Recency (active within 7 days) | 10 |
| **Total** | **100** |

---

## Deployment

### Docker

```bash
cp .env.example .env      # fill in real values
docker compose up --build
```

### Render / Railway

1. Push this repository to GitHub.
2. Create a new **Web Service** (Render) or service (Railway).
3. Set the **build command**:
   ```bash
   pip install -r requirements.txt
   ```
4. Set the **start command**:
   ```bash
   gunicorn -c gunicorn.conf.py app:app
   ```
5. Add every variable from `.env.example` in the platform's environment panel.
6. Point `BASE_URL` / `GITHUB_REDIRECT_URI` at your deployed URL and register
   that callback URL in the GitHub OAuth App settings.

### Manual (gunicorn)

```bash
gunicorn -c gunicorn.conf.py app:app
```

---

## Logging

Logs are written as rotating files (5 MB × 3 backups) under `LOG_DIR` and to
stdout (so `docker logs` works):

| Logger | File | Content |
|---|---|---|
| `auth` | `auth.log` | Logins, OAuth callbacks, access denials, rate limits. |
| `github` | `github.log` | API calls, rate-limit backoffs, token failures. |
| `scanner` | `scanner.log` | Scan summaries and per-file skips. |
| `app` | `app.log` | Application errors and AI analyzer activity. |

---

## Security Notes

- Secrets are **never** committed; all configuration lives in `.env`.
- The GitHub token is stored only in the signed, HttpOnly session cookie.
- Production sessions force `Secure` cookies (HTTPS required).
- PAT login is protected by per-IP rate limiting.
- The scanner is a *heuristic* — use real SAST tools (Semgrep, CodeQL, Bandit)
  for mission-critical assurance.

---

## Screenshots

> _Placeholder — add captures here._

| Login | Dashboard |
|---|---|
| `static/images/screenshot-login.png` | `static/images/screenshot-dashboard.png` |
| AI Suggestions | Security Scanner |
| `static/images/screenshot-ai.png` | `static/images/screenshot-scanner.png` |

---

## Roadmap

- [ ] Multi-repository aggregation
- [ ] Historical trends (last 6 months)
- [ ] Per-developer commit graphs
- [ ] Slack / email weekly digest
- [ ] Real SAST integration (Semgrep)

---

## License

MIT

Team Members:

1.Manoj 
2.Wilfrit
3.Surya
4.Yogesh


