# Daily Indian Bird Agent 🦜

One bird a day, by email + tracked in a Google Sheet. Starts with **South India** (Kerala, Tamil Nadu,
Karnataka, Andhra, Telangana, Puducherry, Lakshadweep), including migrants, then auto-expands to **all of India**
once ~15 South Indian species remain (force with `SCOPE=south|india`).

## How it stays reliable and non-repetitive
- **Species universe:** eBird (Cornell Lab) regional checklists + official taxonomy.
- **Content:** Wikipedia article (must contain the scientific name AND mention India, otherwise skipped) + Wikimedia Commons photos (credited) + IUCN category via GBIF.
- **No made-up facts:** Claude writes only from the fetched text (unknown = "not well documented"), then a second Claude pass fact-checks and removes unsupported claims. The count removed is shown in each email.
- **No repeats:** every sent/skipped species code is logged in the sheet and never picked again; same family is avoided for 14 days and same genus for 30.
- **You know the common birds:** `data/familiar_birds.txt` (edit it) is saved for last. Oct–Mar the picker leans toward migrant-rich families.
- **"Different varieties":** each email shows other Indian species from the same genus (or family) with photos.
- Books (Grimmett, Ripley Guide, Salim Ali, Kazmierczak) are listed as further reading; they can't be fetched online so facts are not taken from them.

## Setup (about 20 minutes, all free except a few paise of Claude API per day)
1. **eBird API key:** https://ebird.org/api/keygen
2. **Anthropic API key:** https://console.anthropic.com
3. **Gmail app password:** Google Account → Security → 2-Step Verification on → App passwords.
4. **Google Sheet:** create a blank sheet; copy its ID from the URL (`/d/<ID>/edit`).
   Google Cloud Console → new project → enable *Google Sheets API* → Credentials → Service account → Keys → JSON.
   Share the sheet (Editor) with the service account's `client_email`.
5. **GitHub:** create a private repo with these files. Settings → Secrets → Actions, add:
   `EBIRD_API_KEY, ANTHROPIC_API_KEY, GMAIL_USER, GMAIL_APP_PASSWORD, EMAIL_TO, SHEET_ID, GOOGLE_SERVICE_ACCOUNT_JSON` (paste the whole JSON).
6. **Test:** Actions → *Daily Bird* → Run workflow with `dry_run = 1`, then once with `0`. It then runs daily at 6:30 AM IST.

Local run: `pip install -r requirements.txt`, export the same variables, `python agent.py --dry-run` (writes `preview.html`).
Without `SHEET_ID` it logs to `data/log.csv` instead.

## Notes
- GitHub pauses scheduled workflows in repos inactive for 60 days; re-enable with one click if that happens.
- Runs are idempotent: it won't send twice in one day unless `FORCE=1`.
- Sheet columns: date, day #, species, family, migratory status, IUCN, range, male/female, breeding, nesting, facts, related species, links.
