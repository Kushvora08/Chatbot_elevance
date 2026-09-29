# Publish the Aster browser preview

## Just one file

The root **index.html** is the complete browser preview. HTML, CSS, JavaScript,
icons, the welcome image and six public synthetic documents are embedded.
Double-click it to open in a modern browser. No Python, npm, API key or internet
connection is needed after the file is downloaded. JavaScript must be enabled.

To publish **only this file** in a new GitHub repository:

1. Create a public repository and upload `index.html` at its root, not in a folder.
2. Open **Settings > Pages**.
3. Under **Build and deployment**, select **Deploy from a branch**.
4. Select **main** and **/ (root)**, then **Save**.
5. Wait for the Pages deployment, then use **Visit site** on that screen.

The address normally looks like `https://YOUR-USERNAME.github.io/YOUR-REPOSITORY/`.
The page works at a repository subpath. Upload a replacement `index.html` to update it.

## If uploading the whole project

Use **Settings > Pages > Source > GitHub Actions** instead. The included
**Publish browser demo to GitHub Pages** workflow stages only `index.html`, not
your Python code, test fixtures, account database or private documents. Run it
from **Actions > Publish browser demo to GitHub Pages > Run workflow** after
enabling Pages. Later changes to `index.html` on `main` or `master` deploy automatically.

Never upload `.env`, `.hosted-data`, `.kb_state`, virtual environments, real customer
files or `outputs`. Keep the included `.gitignore`. On a public repository, files
committed to GitHub are public regardless of the Pages artifact.

## What works in the browser preview

- Conversations, language selection, region/product context and source dialogs.
- Sample refund policies: US 30 days, EU 45 days, historical US 14 days in 2025.
- Questions about capacity, the included cable and the sample error ERR-204.
- Tamil, Telugu and Bengali acknowledgement/clarification rules, including some
  transliterated phrases. Source excerpts stay in English; this is not a translation model.
- Plain-text TXT/CSV/Markdown extraction and mismatch checks. Maximum 200 KB/file.
- Duplicate text-file detection, basic unsafe-content rejection and 15-minute
  file retention checked while the page is open or when it resumes.
- Multiple isolated conversations with up to 40 turns in tab memory. After 30
  minutes of inactivity, the conversation is inactive; context is retained for
  up to 24 hours in that tab. Reload or closing the page clears all demo data.
- Local ticket drafts, duplicate-draft checks, masked JSON downloads, sample
  knowledge CSV download and a synthetic invoice download.

Try `What is the refund policy?`, `What was the refund policy on 2025-06-01?`,
`How many connected devices does Atlas Hub support?`, or `Error ERR-204`.
Change Region to European Union to test the alternate policy; choose India to
verify that missing policy evidence is not invented. Use Demo activity for downloads.

Use **fictional data only**. Rule-based masking and injection checks are demo
safeguards, not a security boundary or an exhaustive sensitive-data detector.
Downloaded files are outside retention cleanup and remain until you delete them.

## What needs the Python backend

GitHub Pages serves static files; it does not run Python. This preview does not
provide real accounts/access control, live order lookups, agent delivery, image/PDF
OCR, persistent data, scheduled updates, quality-gated activation, rollback,
background queues, production sentiment/SLA processing or production monitoring.
The demo activity counts are only in-tab interactions, not service health metrics.
Never use this preview as a real customer support endpoint.

For the authenticated application, follow [DEPLOYMENT.md](DEPLOYMENT.md).
The HTML demo deliberately makes no backend requests. Once the Python application
is deployed, share its HTTPS address as the full app; it serves the working frontend
itself. No credentials belong in `index.html`.

## Developer checks

After editing the shared frontend or `github_pages` sources, rebuild the file:

```text
python scripts/build_pages.py
python scripts/build_pages.py --check
python -m unittest tests.test_pages_build -v
node --test tests/pages-demo.test.cjs
```

The build script uses only Python's standard library and the project's own modules.
Do not manually edit the generated root `index.html`; changes will be overwritten.

Official references: [Creating a GitHub Pages site](https://docs.github.com/en/pages/getting-started-with-github-pages/creating-a-github-pages-site)
and [Custom Pages workflows](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages).
