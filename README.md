# Mealplanner

A self-hosted weekly meal planner for an Australian family with a toddler. Runs on a Raspberry Pi, edited from anywhere via GitHub, with the database backed up nightly to a Synology NAS. Uses the Anthropic API (Claude) as the planner brain.

## What it does

- Generates a week of meals against a hard AUD budget (per meal or per week).
- Filters by cooking time, ingredient count, cuisines, dislikes, allergies.
- Uses the pantry before adding to the shopping list.
- Knows your nearby supermarkets and groups the shopping list for one trip.
- Has a separate planner for a toddler that hits NHMRC nutrient targets, prioritises iron / omega-3 DHA / calcium / fibre, and lifts toddler-safe versions of family meals where possible. Date of birth is stored once and age is computed fresh — no need to update it monthly.
- **Appliance-aware**: tell the planner what's in your kitchen (oven, microwave, slow cooker, air fryer…) and it'll lean on what you have.
- **Batch cooking and freezer-friendly modes**: ask for meals that span 2-3 nights from one cook session, or that freeze cleanly.
- Recurring schedules: plans appear automatically on Sunday mornings.
- Feedback (thumbs / swap) feeds into future plans.
- **Export** any plan as a print-ready HTML page, plain-text recipes, or a plain-text shopping list.
- **On-screen shopping list with persistent checkboxes** — open the plan on your phone, tick items as you grab them, the state survives navigating away and coming back.

## Further reading

- [`docs/meal_planning_logic.md`](docs/meal_planning_logic.md) — how plans actually get generated, what the prompt contains, what the AI is and isn't doing, and how to tune it.
- [`docs/tailscale.md`](docs/tailscale.md) — make the planner reachable from anywhere, securely, without exposing the Pi to the public internet.

## The setup, in one diagram

```
You (Windows + VS Code)
        ↓ git push
GitHub repo (this one)
        ↓ git pull (manually or scheduled)
Raspberry Pi
   └── systemd-managed Flask app on port 8080
        └── data.db on the Pi's SD card
                ↓ nightly rsync via SSH
        Synology NAS
        └── /volume1/backups/mealplanner/daily/
```

You edit on Windows. You push. The Pi runs the latest code. The NAS keeps a rolling 14-day backup of the database in case the SD card ever dies.

## First-time setup

### 1. On Windows: clone and edit

```powershell
git clone https://github.com/<your-username>/mealplanner.git
cd mealplanner
code .
```

(Assumes you've installed Git for Windows and VS Code. If you haven't yet, [git-scm.com](https://git-scm.com/download/win) and [code.visualstudio.com](https://code.visualstudio.com/).)

### 2. On the Pi: install

SSH from Windows:

```powershell
ssh admin@<pi-ip>
```

On the Pi:

```bash
git clone https://github.com/<your-username>/mealplanner.git
cd mealplanner
bash scripts/install_pi.sh
```

The install script auto-detects your username and patches the systemd unit accordingly. No need to manually edit `/home/pi/...` paths anymore.

Set the API key:

```bash
echo 'ANTHROPIC_API_KEY=sk-ant-api03-...' > .env
chmod 600 .env
```

Get a key at [console.anthropic.com](https://console.anthropic.com). A week of plans on `claude-haiku-4-5` costs cents.

Start it:

```bash
sudo systemctl start mealplanner
sudo systemctl status mealplanner   # should say "active (running)"
```

Open `http://<pi-ip>:8080` from any device on your home WiFi.

### 3. On the NAS: set up backup

On the Synology:

1. **Enable SSH**: Control Panel → Terminal & SNMP → tick "Enable SSH service".
2. SSH into the NAS from Windows (or use DSM Terminal): `ssh admin@<nas-ip>`.
3. Generate an SSH key for the Pi connection:
   ```bash
   ssh-keygen -t ed25519 -f ~/.ssh/id_pi_backup -N ""
   cat ~/.ssh/id_pi_backup.pub
   ```
4. On the **Pi**, add that public key to `~/.ssh/authorized_keys` so the NAS can connect without a password.
5. Copy the backup script onto the NAS (e.g. `/volume1/backups/mealplanner/backup.sh`) and edit the `PI_HOST` variable at the top.
6. **Schedule it in DSM**: Control Panel → Task Scheduler → Create → Scheduled Task → User-defined script. Run daily at 3am, command: `bash /volume1/backups/mealplanner/backup.sh`.

The script keeps the last 14 daily backups (configurable). Each is a complete consistent SQLite snapshot taken via `sqlite3 .backup`, so it's safe to copy even while the app is writing.

To restore: stop the service, copy the desired backup over `/home/admin/mealplanner/data.db`, restart the service.

## The day-to-day workflow

### Edit and deploy code

```powershell
# in VS Code on Windows
git add .
git commit -m "tweak the toddler prompt"
git push

# from Windows PowerShell — one-liner deploy
ssh admin@<pi-ip> "cd mealplanner && bash scripts/deploy.sh"
```

`deploy.sh` does a `git pull --ff-only`, reinstalls Python deps if `requirements.txt` changed, restarts systemd, and shows the status. Takes about 5 seconds when nothing changed, ~30 seconds when deps need updating.

### Use the app

`http://<pi-ip>:8080` from any device on the home network. Bookmark to the home screen on your phones for an app-like experience.

Once a plan is generated, the page has an **export bar** with three buttons:

- **Print / save as PDF** — opens a clean print-friendly page; Ctrl+P (Cmd+P on Mac) and "Save as PDF" gives you a single document with all recipes and the shopping list.
- **Recipes (.txt)** — plain-text dump of every recipe in the plan. Email it, paste into Notes, share via text.
- **Shopping list (.txt)** — just the shopping list, grouped by supermarket, with checkboxes. Import into your phone's notes app and tick off as you go.

## Project layout

```
mealplanner/
├── app.py                Flask routes + APScheduler for recurring runs
├── ai_planner.py         Claude prompts + tool-use schemas
├── nutrition.py          Toddler nutrition reference (NHMRC NRV)
├── database.py           SQLite layer
├── config.example.py     Copy to config.py on first install
├── requirements.txt      Python deps
├── templates/            Jinja templates
│   ├── base.html
│   ├── index.html, plan_new.html, plan_view.html
│   ├── toddler.html, pantry.html, dislikes.html
│   ├── settings.html, schedules.html
│   └── export_plan.html  Print-friendly export
├── static/
│   └── style.css         Editorial design — single CSS file
├── systemd/
│   └── mealplanner.service
├── scripts/
│   ├── install_pi.sh     First-time setup, auto-detects user
│   ├── deploy.sh         Update from git and restart
│   └── nas_backup.sh     Run on the NAS for nightly backups
├── README.md
└── LICENSE               MIT
```

## Why this shape

### Why the Pi, not Cloudflare or the NAS?

We considered all three:

- **Cloudflare Workers + Pages**: would mean rewriting the entire app in TypeScript, retiring the Pi.  Powerful, free, but a complete rebuild for marginal benefit on a household tool.
- **Synology NAS hosting**: the DS220j doesn't support Container Manager (it's locked to the `+` series and above). Native Python install is possible but fiddly given DSM's permission model.
- **Pi**: already working, edits handled via git, NAS handles backup. Lowest-friction path.

The Pi runs the app. The NAS protects the data. Windows handles editing.

### Why the API key is on the Pi, not Cloudflare

Earlier in the planning we discussed Cloudflare AI Gateway for the key. It's a good pattern but not strictly necessary for a single-user/single-key home tool. The key sits in `.env` on the Pi, restricted to mode 600, behind your home firewall. If you ever want to add the gateway, it's a 2-line change in `ai_planner.py` to point the SDK at a different `base_url`.

### Why SQLite, not Postgres

Pi Zero W has 512MB RAM. SQLite costs nothing at idle. The NAS-side backup script uses `sqlite3 .backup` which produces a consistent snapshot even while the app is writing — no need to stop the service.

### Why tool-use, not free-form JSON

Earlier versions asked Claude for raw JSON. At 7 days × 3 meals + shopping list, occasional malformed JSON would crash the parse. Switching to the SDK's tool-use feature means the model fills in fields against a schema — structurally valid every time.

## Privacy

The only data leaving your network is the planning prompt sent to Anthropic — your household preferences, pantry, dislikes, feedback. No location coordinates (just the suburb you put in Settings). No photos, no contacts. The SQLite database stays on the Pi, the backup stays on the NAS.

## Costs

`claude-haiku-4-5` (default): one weekly plan ≈ a few cents. `claude-sonnet-4-6` for richer plans ≈ 5–10× the cost.

## Disclaimer

The toddler planner uses NHMRC reference values and Australian under-2 safety guidance, but it's not a substitute for paediatric dietetic advice — particularly with allergies, growth concerns, or fussier eaters. Treat the toddler plans as a starting point and adjust on instinct.

## License

MIT — see `LICENSE`.
