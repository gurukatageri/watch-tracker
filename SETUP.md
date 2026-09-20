# Watch Launch Tracker v2: setup guide

Setup takes about 45 minutes on a computer. After that, everything works from your iPhone.

**What you'll get**

- A Telegram digest every morning at 08:00 UAE time with new releases under AED 100,000, each with:
  - collectability score
  - Hype index
  - resale outlook
  - where to buy in the UAE
- Urgent Telegram alerts (outside 23:00–07:00) when a strong release opens for pre-order or launches within 48 hours.
- A dashboard on your home screen, including your private collection and boutique contacts.
- Telegram commands to record purchases and contacts.

Sources are checked every hour in the background.

**If you already set up version 1:** skip to "Upgrading from version 1" at the end.

---

## Part A: Accounts and keys

Keep a private note open and paste each key into it as you go. You'll add them all to GitHub in Part C.

### 1. Telegram bot (required, free)

1. In Telegram, open **@BotFather** (blue check mark) and send `/newbot`.
2. Choose a name and a username ending in `bot`.
3. Copy the **token** → `TELEGRAM_BOT_TOKEN`.
4. Open your new bot and send it `hi`.
5. In a browser, open `https://api.telegram.org/botYOUR_TOKEN/getUpdates`.
6. Find `"chat":{"id":` followed by a number → `TELEGRAM_CHAT_ID`.

### 2. Claude API (required)

1. At **console.anthropic.com**, create an account and add prepaid credit under **Billing**. Set a monthly limit under **Limits** for peace of mind.
2. Under **API Keys**, create a key → `ANTHROPIC_API_KEY`.

The tracker uses a low-cost model for reading news. It uses a stronger model with web search only for researching promising releases, and that is capped at 8 per day in `config.json`.

### 3. WatchCharts (paid, recommended)

This powers the resale estimates and your collection values.

1. At **watchcharts.com**, create an account and subscribe to the **Professional** plan with API access (or pay-as-you-go API credits). New API customers can email them to request a free trial.
2. Go to **watchcharts.com/api/keys** and create a key → `WATCHCHARTS_API_KEY`.

Results are cached for 7 days to save credits.

### 4. SerpApi (paid, recommended)

This measures Google search interest.

1. Sign up at **serpapi.com**, choose a plan, and copy your key from the dashboard → `SERPAPI_KEY`.

The tracker makes at most 15 searches a day (`max_hype_checks_per_day`).

### 5. YouTube Data API (free)

This measures video count and views.

1. Go to **console.cloud.google.com** and create a project, e.g. *watch-tracker*.
2. Go to **APIs & Services → Library**, find **YouTube Data API v3**, and click **Enable**.
3. Go to **APIs & Services → Credentials → Create credentials → API key**. Copy it → `YOUTUBE_API_KEY`.
4. Optional: click the key and restrict it to *YouTube Data API v3* only.

The free daily quota comfortably covers this use.

### 6. Your private passphrase (required for collection features)

Invent a long passphrase, e.g. four random words: `falcon-copper-harbour-mint` → `PORTFOLIO_PASSPHRASE`.

This passphrase encrypts your collection and contacts. **If you lose it, the saved data cannot be recovered**, so keep it in your password manager.

### 7. Reddit (optional, skip for now)

Reddit now requires a manual application for API access, and approval is slow and uncertain. If you are approved later, add `REDDIT_CLIENT_ID` and `REDDIT_CLIENT_SECRET` and Reddit discussion is included in the Hype index automatically. Everything works without it.

---

## Part B: Put the project on GitHub

1. Create a free account at **github.com**.
2. Click **+** → **New repository**. Name it `watch-tracker`, set it to **Public**, and click **Create repository**.
3. Click **uploading an existing file**. Unzip the project and drag in everything **except the `github-workflow` folder**:
   - `watch_tracker.py`, `common.py`, `signals.py`, `private.py`
   - `config.json`, `requirements.txt`, `SETUP.md`
   - the `data` and `docs` folders

   Then click **Commit changes**.
4. Click **Add file → Create new file**.
5. Type the name `.github/workflows/tracker.yml`, paste the contents of `github-workflow/tracker.yml` from the zip, and click **Commit changes**.

## Part C: Add your secrets

Go to **Settings → Secrets and variables → Actions → New repository secret**. Add each one, using exactly these names:

| Name | Required? |
|---|---|
| `ANTHROPIC_API_KEY` | Yes |
| `TELEGRAM_BOT_TOKEN` | Yes |
| `TELEGRAM_CHAT_ID` | Yes |
| `PORTFOLIO_PASSPHRASE` | For collection and contacts |
| `WATCHCHARTS_API_KEY` | Recommended |
| `SERPAPI_KEY` | Recommended |
| `YOUTUBE_API_KEY` | Recommended |
| `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET` | Optional |

Any key you leave out simply switches that feature off. The tracker uses whatever is available.

## Part D: Dashboard and permissions

1. Go to **Settings → Pages**. Set **Source** to *Deploy from a branch*, choose `main` and `/docs`, and click **Save**.
2. Go to **Settings → Actions → General → Workflow permissions**, choose **Read and write permissions**, and click **Save**.

## Part E: Test

1. Open **Actions → Watch Launch Tracker → Run workflow** and choose mode **test**. Telegram shows a checklist of which services are connected.
2. If WatchCharts shows a warning, run mode **probe**. Open the run, then the *Run tracker* step, and send me what it printed. WatchCharts' documentation isn't publicly readable, so the connection is built to adapt, but the probe shows exactly what their API returns and lets us fine-tune it.
3. Run mode **digest** for your first full digest. The first one takes several minutes.

## Part F: iPhone

- **Dashboard:** open `https://YOUR-USERNAME.github.io/watch-tracker/` in Safari, then **Share → Add to Home Screen**.
- **Collection tab:** enter your passphrase once and tick *Remember on this device*.
- **Telegram:** keep notifications on for your bot. Mark the chat as a favourite so urgent alerts stand out.

---

## Telegram commands

Send these to your bot. They are processed within the hour, and the bot replies when done.

| Command | Example |
|---|---|
| Record a purchase | `/bought Tudor BB58 79030N for 17,900 AED on 12 Sep` |
| Record a sale | `/sold the Tudor BB58 for 21,000 AED` |
| Save a boutique contact | `/contact Tudor: Ahmed, Seddiqi Dubai Mall, +971 50 123 4567` |
| Collection value now | `/portfolio` |
| List contacts | `/contacts` |

Tips:

- Include the **reference number** when you can. It's how WatchCharts finds the exact market value.
- For `/bought`, enter the price you actually paid, including VAT.
- The Monday digest includes a collection summary. You can change the day with `portfolio_report_weekday` (0 = Monday).
- Contacts appear automatically in alerts for that brand.

## Reading the numbers

**Collectability (1–10)** is an AI judgment of how desirable and collectible the release is.

**Hype index (0–100)** combines:
- how many news outlets covered it
- YouTube videos and views
- Google search interest compared with the brand's own searches
- Reddit, if connected

It is tracked daily, so you'll see *rising* or *falling*. Hype measures attention, not resale value.

**Resale outlook** gives an expected range versus retail, e.g. *+12% to +32%*. It is built from:
- how comparable past editions trade today (WatchCharts figures preferred)
- adjustments for hype versus production size, and early market signals

Once the watch itself trades second-hand, the outlook switches to its actual market price.

**Confidence** is *low* when few comparables exist, which is common for microbrands and small independents.

**Break-even** is the premium needed to cover 5% VAT plus selling costs (default 10%, set in `selling_cost_pct`). The verdict compares the estimate with it.

These are estimates to support your judgment, not financial advice. The secondary market can change quickly.

## Main settings in `config.json`

| Setting | What it does |
|---|---|
| `max_price_aed` | Budget per watch |
| `min_score_for_digest` | Minimum collectability to appear in the digest |
| `urgent` | `min_score` 7 or `min_hype` 70; `within_hours` 48; quiet hours 23–07 |
| `research_min_score` / `max_research_per_day` | Which releases get full research, and how many per day |
| `max_hype_checks_per_day` | Caps SerpApi and YouTube usage |
| `purchase_tax_pct`, `selling_cost_pct` | Used for break-even and net profit figures |
| `interests` | Plain-English description of your taste, used in scoring |
| `feeds` | News sources |

## Privacy

The repository is public so the dashboard can be hosted for free. The release list and `config.json` are visible to anyone who finds the repository.

Your collection and contacts are stored **only** in encrypted form (`docs/private.enc.json`, AES-256). They can be read only with your passphrase, and your keys are never visible. Telegram messages are private to you.

## Troubleshooting

| Problem | Fix |
|---|---|
| Red X in Actions | Open the run → *Run tracker* step and read the error. |
| `secret is missing` | Check the secret name spelling. |
| `Telegram error 400 chat not found` | Wrong chat ID, or you haven't messaged the bot yet. |
| `Claude API error 401` | The key was copied wrongly; create a new one. |
| `Could not open private data` | `PORTFOLIO_PASSPHRASE` changed. Restore the original. |
| Dashboard says the passphrase doesn't match | Type the exact passphrase from your GitHub secret. |
| Bot doesn't reply to commands | Commands run hourly, so wait up to an hour. Check that the Actions runs are green. |
| Digest arrives a bit late | GitHub sometimes delays scheduled jobs by 5–30 minutes. |
| "Sources not reachable" in the digest | That website changed its feed. Remove or replace it in `feeds`. |

## Upgrading from version 1

1. In your repository, upload the new `watch_tracker.py`, `common.py`, `signals.py`, `private.py`, `requirements.txt`, `config.json` and `docs/index.html`, replacing the old ones. Your collected data is kept.
2. Open `.github/workflows/tracker.yml`, click the pencil, replace everything with the new `github-workflow/tracker.yml`, and click **Commit changes**.
3. Add the new secrets (Part C), then run mode **test**.
