# Publish Aster from GitHub

The HTML/CSS/JavaScript interface and Python backend are served together. GitHub
stores the code; Render runs the application. GitHub Pages alone cannot run this
Python backend. Nothing has been uploaded or provisioned automatically.

## 1. Upload the code

In VS Code, open this project folder, select **Source Control > Initialize
Repository**, review the files, commit, then choose **Publish to GitHub**. Start
with a private repository. Include `render.yaml`, `requirements-hosted.txt`,
`pyproject.toml`, `kb_pipeline/`, `tests/`, and `.github/workflows/tests.yml`.

The `.gitignore` excludes local databases, uploads, logs, virtual environments,
`.env` files, generated outputs, and the local testing configuration. Do not
upload `.kb_state`, `.hosted-data`, credentials, invoices, or customer transcripts.
Git ignore rules do not remove files previously committed, and do not apply to
GitHub's browser-based drag-and-drop uploads. Review the upload list carefully.
Only publish knowledge documents you have permission to share.

The repository includes synthetic Atlas Hub examples. They are not real product
policies. The hosted app seeds six safe examples from its packaged dataset;
it does not import your local databases or the adversarial test corpus.

## 2. Create the hosted service

1. Sign in to [Render](https://dashboard.render.com/) and choose **New > Blueprint**.
2. Connect your GitHub repository and select `render.yaml`.
3. Enter `ADMIN_USERNAME` and a unique `ADMIN_PASSWORD` of 14-128 characters.
4. Review and accept the estimated charges before creating the resources.
5. Wait for the build, then open the web service's HTTPS URL.

The blueprint uses a paid, always-on Python web service, a 1 GB persistent disk,
and a private paid Key Value service for rate limits. Do not substitute a sleeping
free service: background checks and durable local state need an always-on process
and persistent storage. See [Render disks](https://render.com/docs/disks) and
[Blueprint settings](https://render.com/docs/blueprint-spec).

The app uses Render's assigned URL automatically. For a custom domain, set
`APP_BASE_URL` to its exact HTTPS origin, without a path. Redirect users to that
one domain; other hostnames are rejected. HTTPS terminates at Render's proxy.
Only Render's proxy should reach the production Waitress listener. Outside
Render, configure your trusted reverse proxy explicitly before enabling forwarding.

## 3. First sign-in

1. Sign in with the administrator account you configured.
2. Open **Operations console** and create customer accounts. Share each password
   through a secure channel; do not put credentials in GitHub or chat transcripts.
3. Set the update schedule and maintenance window. All these times are **UTC**.
   The default 02:00-03:00 UTC window is 07:30-08:30 in India.
4. Select **Evaluate update**. Quality tests run before approval. Outside the
   maintenance window, the approved version waits; the background worker activates
   it when the window opens. Until an initial version is active, policy answers
   have no evidence and must decline to answer.
5. Ask `What is the refund policy?` with Atlas Hub / United States selected.
   The demo answer should cite the 30-day example policy.
6. After verifying access, remove `ADMIN_PASSWORD` from Render's environment
   settings. The hashed account is already on the disk; restarts do not reset it.

Customer accounts cannot open the operations console, read other customers'
conversations/files, change roles, or send simulated clock values. The shared
local demonstration tokens are not accepted by the hosted service.

## Updates, monitoring, and recovery

- GitHub Actions runs the full tests on pushes and pull requests. Render is
  configured to deploy code updates only after checks pass. This controls code
  deployment; the in-app maintenance window controls **knowledge activation**,
  not Render deployments. Plan code releases separately if they also need a window.
- Edit source content and its metadata in Operations. Save the matching quality
  tests, then evaluate. Only new/modified documents are processed; invalid and
  duplicate documents are quarantined. Inspect the update result before activation.
- Failed update attempts retry after 15, 30, then 60 minutes. Rejected quality
  changes need correction; they are not made active. Approved versions activate
  only inside the configured window.
- Background checks run while the web service is alive. A persisted five-minute
  activation watch restores the previous version on a failing health check;
  for a first activation, the bad version is deactivated. Checks resume on restart.
  A stopped service cannot run checks during its downtime.
- `/healthz` is a public readiness endpoint with no account or document details.
  It checks the account database, rate-limit service, and worker health. The
  protected console shows latency, failure rate, confidence, escalations, versions,
  retry state, and worker errors. No external alert/email provider is configured.
- Uploaded text files expire after 60 minutes by default and are cleaned up by
  the background worker. This retention is separate from knowledge snapshots and
  support records. Disk backups may retain older copies: set your backup retention
  and customer-data retention policy separately.
- All writable state lives under `/var/data`: `accounts.sqlite3`, `pipeline.json`,
  `quality-cases.json`, `knowledge_base/`, `state/`, and `uploads/`. A code deployment
  does not overwrite existing hosted knowledge or accounts.
- Keep **one instance and one Python process**. The JSON snapshots are not a
  distributed database. Do not add Gunicorn workers, replicas, or a second scheduler
  against this disk. The threaded web server serializes pipeline mutations.
- Arrange protected backups and a restore drill before accepting real customer
  data. Quiesce writes for a consistent whole-directory backup; do not copy live
  SQLite files independently. Never commit backups to GitHub. Preserve the secret
  key as well: rotating it revokes browser access and breaks existing chat resume
  credentials. Account passwords and knowledge remain stored on the disk.

For a forgotten password, an authorized hosting operator can use Render's shell:

```text
python -m kb_pipeline.manage_accounts YOUR_USERNAME
```

The password is entered privately at a prompt, not as a command-line argument.
Existing browser sessions for that account are revoked. Admins can also reset
customer passwords or disable accounts in Operations.

## Test locally

The original `start-chatbot.cmd` remains the dependency-free local testing
launcher. Never use that development server as your public hosting command.

To run the authenticated version in a VS Code PowerShell terminal, use Python
3.12 and install the hosted dependencies first:

```powershell
python -m venv .venv-hosted
.\.venv-hosted\Scripts\python.exe -m pip install -r requirements-hosted.txt
$env:APP_ENV = 'development'
$env:APP_BASE_URL = 'http://127.0.0.1:8766'
$env:APP_DATA_DIR = "$PWD/.hosted-data"
$env:PORT = '8766'
$env:KB_SEED_DEMO_DATA = '1'
$env:APP_SECRET_KEY = Read-Host 'Enter a random secret of at least 32 characters' -MaskInput
$env:ADMIN_USERNAME = Read-Host 'Initial admin username'
$env:ADMIN_PASSWORD = Read-Host 'Initial admin password (14+ characters)' -MaskInput
.\.venv-hosted\Scripts\python.exe -m kb_pipeline.hosted
```

`-MaskInput` requires PowerShell 7.1 or newer. On Windows PowerShell 5.1, use
`Read-Host -AsSecureString` and convert only into the process environment, or use
a newer PowerShell terminal. Keep your secret in a password manager and use the
same value on subsequent starts. Do not commit it. The `.env.example` file is
reference documentation only; this application does not automatically load `.env`.
Open <http://127.0.0.1:8766>. If that port is occupied, choose a different port in
both `PORT` and `APP_BASE_URL`. This preview uses in-memory rate limits; production
refuses to start without a private Redis-compatible service, HTTPS, and a persistent
data directory.

Run the complete test suite:

```powershell
.\.venv-hosted\Scripts\python.exe -m unittest discover -s tests -v
```

Tests cover account isolation, unauthorized access, CSRF, rate limits and outages,
session revocation, file ownership/expiry, persistence, maintenance, retry timing,
quality rejection, and post-activation rollback in addition to the local workflows.

## Before real customer use

This is a deployment-ready **controlled pilot**, not a claim that all original
production requirements are complete. Validate security and business rules against
your own data before opening it to customers.

- Hosted uploads intentionally accept only TXT, CSV, and Markdown, up to 2 MB,
  four files per analysis, with a 100 MB total upload-store cap. Images and PDFs
  remain disabled until sandboxed OCR, malware scanning, and a durable background
  processing/notification service are integrated and tested.
- Language, sentiment, injection detection, and retrieval remain rule-based. The
  bundled quality suite has four synthetic cases, not a statistically representative
  accuracy evaluation. Expand it with approved, real-domain cases and review the
  current document access levels, dates, and business policies.
- Replace the demo policies, verify agent availability, business hours, holidays,
  SLA conditions, languages, and thresholds for your organization. Configuration
  not exposed in Operations is in the private persistent `pipeline.json`; stop
  the service before editing it, then restart and test.
- Accounts are invitation-only, with hashed passwords and revocable 12-hour
  sign-ins. There is no SSO, MFA, email verification, password-reset email, or
  self-service registration. Integrate a managed identity provider if required.
- Ticket routing is internal state. Email, paging, agent-tool integrations and
  delivery acknowledgements are not wired to external services. An on-call queue
  entry by itself does not notify a real agent.
- Configure uptime alerts, log retention/rotation, backup retention, capacity
  monitoring, and incident response. A disk-backed single instance has downtime
  on redeploy and is not a high-availability architecture.
