# OfficeFlow — Vercel-ready Flask Employee Task Management

A Python/Flask + SQLAlchemy + Jinja employee task management application.

## Production architecture

- **Frontend/templates:** Flask + Jinja + CSS/JS (static assets served from `/public` via Vercel's CDN)
- **Backend:** Flask (deployed as a single Vercel Function using Vercel's zero-config Flask support)
- **Production database:** PostgreSQL (Neon, Supabase, Vercel Postgres, or any other Postgres provider)
- **Authentication:** Flask-Login
- **CSRF protection:** Flask-WTF
- **First admin:** created automatically from environment variables on first boot

> Vercel is serverless. Do not rely on a local SQLite file for production data —
> the filesystem is not persistent between invocations.

This project uses Vercel's **zero-configuration Flask support** (no `/api`
folder, no `vercel.json` rewrites needed). Vercel detects `app.py` at the
project root and runs it directly as a Vercel Function.

---

# How the multi-manager system works

| Role | How the account is created | What they can do |
|---|---|---|
| **Admin** (you) | From `ADMIN_*` env variables on first boot | See all managers, block/unblock a manager, resend/force verification, platform stats. Cannot see managers' task details. |
| **Manager** | Self sign-up at `/register` + **email verification link** | Add their own employees, create/assign/transfer tasks, reports, print. Sees **only their own team**. |
| **Employee** | Added by a manager. Gets an **invite email** to set their own password (this verifies the email) | Manage own tasks, reports, notifications. |

- One deployment serves every manager; data is separated by `manager_id`, so a manager can never open another manager's employees or tasks (they get 404).
- Sign-in is by **email or Employee ID** (names are not unique across managers).
- Emails are sent by SMTP: manager verification (3 days), employee invite (7 days), password reset (1 hour). Links are signed, expire, and invite/reset links are single-use. A 60-second cooldown limits resend spam.
- **Approval step:** with *Admin approval* ON (default), a new manager must verify their email and then wait for the admin to approve. Admins get an email when a verified manager is waiting; the manager gets an email when approved/rejected. The sidebar shows a badge with the number of pending requests.
- **Admin controls** (Admin → Managers → open a manager): approve/reject, block/unblock, view the team (read-only, task details stay private), edit name/company/email/phone, set a new password or email a reset link, and delete the manager together with all employees, tasks and history (type the manager's email to confirm).
- **Admin → Settings:** live switches for *Manager sign-ups* (open/closed) and *Admin approval*, plus a "send test email" button. These are stored in the database, so no redeploy is needed.
- Blocking a manager from the Admin panel also blocks all of their employees.
- Locally (no SMTP set) emails are **printed in the terminal** so you can test without a mail account.
- `/health` also reports whether email is configured.

> **Upgrading an existing database:** new columns are added automatically and old accounts are marked verified. Old employees that were created before this version have no manager, so they will not appear under any manager. For a clean start, use a fresh database.

---

# A. Deploy to Vercel — exact steps

## 1. Prepare the project

The important deployment files are:

```text
app.py
requirements.txt
vercel.json
public/static/...
```

Do **not** upload `.env`, `.venv`, `venv`, or `instance/` to GitHub — they're
already excluded via `.gitignore`.

## 2. Create a PostgreSQL database

Create a PostgreSQL database with a provider such as Neon, Supabase, or
Vercel Postgres. Copy the connection string — it looks like:

```text
postgresql://USER:PASSWORD@HOST/DATABASE?sslmode=require
```

or:

```text
postgresql+psycopg://USER:PASSWORD@HOST/DATABASE?sslmode=require
```

Both formats are accepted — the app normalizes either one to use the
`psycopg` (v3) driver automatically.

> **Tip:** if your provider offers a "pooled"/"pgbouncer" connection string
> (Neon and Supabase both do), prefer that one for `DATABASE_URL`. Serverless
> functions open a lot of short-lived connections, and a pooled connection
> string handles that far better than a direct one.

## 3. Create a GitHub repository

```powershell
git init
git add .
git commit -m "Initial deployment"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/officeflow.git
git push -u origin main
```

## 4. Import into Vercel

**Add New → Project → Import Git Repository**, then select your repository.

Leave the Build Command, Output Directory, and Install Command on their
defaults — Vercel detects the Flask app automatically from `app.py` and
`requirements.txt`.

## 5. Add Environment Variables

In **Vercel → Project → Settings → Environment Variables**, add:

| Variable | Example / notes |
|---|---|
| `SECRET_KEY` | Generate with `python -c "import secrets; print(secrets.token_hex(32))"` |
| `DATABASE_URL` | Your real PostgreSQL connection string — never invent this value |
| `ADMIN_NAME` | `Administrator` |
| `ADMIN_EMAIL` | Your real admin login email, e.g. `admin@yourcompany.com` |
| `ADMIN_EMPLOYEE_ID` | `ADM-1000` |
| `ADMIN_PASSWORD` | A strong password, at least 6 characters — keep this out of GitHub |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD` / `SMTP_FROM` | Your SMTP account (see `.env.example`). **Required** for sign-up emails |
| `APP_BASE_URL` | Optional, e.g. `https://yourdomain.com` |
| `FLASK_DEBUG` | `0` |

For the first deployment, applying these to **Production and Preview** is fine.

## 6. Deploy

Click **Deploy** and wait for the deployment to become **Ready**, then open
the Vercel URL.

If any required variable above is missing, the app now shows an in-app
**"Setup required"** page listing exactly what's missing, instead of a bare
Vercel error page.

## 7. Test the health endpoint

Open:

```text
https://YOUR-VERCEL-DOMAIN/health
```

Expected response:

```json
{ "status": "ok", "database": "ok" }
```

If it returns `"database": "unavailable"` or `"unconfigured"`, double-check
`DATABASE_URL`.

## 8. Log in

```text
Email: ADMIN_EMAIL
Password: ADMIN_PASSWORD
```

The app automatically creates the first admin if that email doesn't already
exist. Changing `ADMIN_PASSWORD` in Vercel later does **not** change the
password of an already-created admin — change it from the app's password
page instead.

---

# B. About seed_admin.py

`seed_admin.py` is a **local-only** interactive helper for SQLite development.
Don't run it on Vercel — the app creates the first admin automatically from
`ADMIN_*` environment variables instead.

```powershell
python seed_admin.py   # local Windows/macOS/Linux development only
```

---

# C. Local development setup

```powershell
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS/Linux

pip install -r requirements.txt
copy .env.example .env          # Windows: copy, macOS/Linux: cp
```

Leave `DATABASE_URL` empty in `.env` to use local SQLite automatically.

```powershell
python seed_admin.py
python app.py
```

Open `http://127.0.0.1:5000`.

---

# D. Updating the deployed app

```powershell
git add .
git commit -m "Update application"
git push
```

Vercel automatically creates a new deployment from the pushed commit.

---

# E. Important production rules

**Never commit:** `.env`, `.venv/`, `venv/`, `instance/`, `*.db` — already
covered by `.gitignore`.

**Never put in GitHub:** `SECRET_KEY`, `DATABASE_URL`, `ADMIN_PASSWORD` — keep
them in Vercel Environment Variables only.

**PostgreSQL is required for production.** SQLite is for local development
only; Vercel's filesystem is not a persistent application database.

---

# F. Common Vercel errors and fixes

## In-app "Setup required" page

The app itself will tell you exactly which environment variable is missing.
Add it in **Project → Settings → Environment Variables**, then redeploy.

## `/health` returns `"database": "unavailable"`

`DATABASE_URL` is set but the app can't connect. Check for:
- a typo in the connection string
- `?sslmode=require` missing (most hosted providers require it)
- the database provider's IP allowlist blocking Vercel (most managed
  Postgres providers allow all IPs by default — check yours does too)

## `FUNCTION_INVOCATION_FAILED`

Open **Vercel → Deployments → your deployment → Logs** and look for the
first Python traceback. As of this version, most startup failures (missing
env vars, unreachable database) are caught and shown as the in-app setup
page instead — so a traceback here usually points to something else, like a
missing package.

## `ModuleNotFoundError`

Make sure the package is listed in `requirements.txt`, then push and
redeploy.

---

# G. Deployment checklist

- [ ] GitHub repository contains the latest project
- [ ] `app.py`, `requirements.txt`, `vercel.json` are present at the project root
- [ ] PostgreSQL database created (pooled connection string if available)
- [ ] `DATABASE_URL` added in Vercel
- [ ] `SECRET_KEY` added in Vercel
- [ ] `ADMIN_NAME`, `ADMIN_EMAIL`, `ADMIN_EMPLOYEE_ID`, `ADMIN_PASSWORD` added in Vercel
- [ ] `SMTP_*` variables added (manager verification will not work without them)
- [ ] `FLASK_DEBUG=0`
- [ ] `.env` is not in GitHub
- [ ] No need to run `seed_admin.py` on Vercel

Then click **Deploy**.
