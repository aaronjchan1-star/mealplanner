# Mealplanner

A self-hosted weekly meal planner designed to live on a **Raspberry Pi Zero W** in a quiet corner of the house, accessible from any phone or laptop on the local network. Uses the Anthropic API (Claude) as the planner brain so the Pi itself doesn't need to do any heavy thinking — it just serves a small Flask app and keeps your preferences.

Originally built for an Australian household with a toddler in mind. The defaults assume Sydney supermarkets and AUD prices, but the prompts are easy to retarget.

## What it does

- Generates a week of meals against a **hard budget** (per meal or per week).
- Filters by **cooking time, ingredient count, cuisines you love, dislikes, allergies**.
- Uses your **pantry** before adding items to the shopping list.
- Knows your **location and nearby supermarkets** and groups the shopping list so one trip should cover it.
- Has a **separate planner for a toddler** that hits NHMRC nutrient targets, prioritises iron / omega-3 DHA / calcium / fibre, and lifts toddler-safe versions of family meals where it can.
- **Recurring schedules**: tell it to plan every Sunday morning automatically.
- **Feedback**: thumbs up / thumbs down on meals, and a swap button — both feed back into future plans.

## Hardware notes

The Pi Zero W is constrained — single-core ARMv6 at 1 GHz, 512 MB RAM. The app is built to fit:

- Flask + raw `sqlite3` (no SQLAlchemy).
- All the LLM work happens at Anthropic, not on the Pi. The Pi just makes HTTPS calls and renders templates.
- `MemoryMax=180M` in the systemd unit so it can't ever crowd out the rest of the OS.
- ARMv6 wheels installed via [piwheels](https://www.piwheels.org/).

A 7-day plan generation takes ~15–30 seconds — almost all of that is API time, not Pi time.

Works fine on a Pi 3 / 4 / 5 too, or any Linux box. Also runs on Windows or macOS for local development.

## Install on a Raspberry Pi

```bash
git clone https://github.com/<your-username>/mealplanner.git
cd mealplanner
bash scripts/install_pi.sh
```

The script installs system packages, creates a venv, installs Python deps via piwheels, and registers the systemd unit.

Then create a `.env` file with your Anthropic API key:

```bash
echo 'ANTHROPIC_API_KEY=sk-ant-api03-...' > .env
chmod 600 .env
```

Get a key at [console.anthropic.com](https://console.anthropic.com) — you'll need a few dollars of credit. A week of plans costs cents on `claude-haiku-4-5`.

Start it:

```bash
sudo systemctl start mealplanner
sudo systemctl status mealplanner
```

Then open `http://<pi-ip>:8080` from any device on your home network.

**Note**: the install script and systemd unit assume the user `pi` and project at `/home/pi/mealplanner`. If your user is different (modern Raspberry Pi OS no longer creates a `pi` user by default — you might be `admin` or your name), edit those before running:

```bash
# fix paths if your user isn't 'pi'
sed -i "s|/home/pi/|/home/$USER/|g; s|User=pi|User=$USER|g" \
  systemd/mealplanner.service scripts/install_pi.sh
```

## Local dev (Windows / macOS / Linux)

```bash
python -m venv .venv
# Linux/macOS:
source .venv/bin/activate
# Windows PowerShell:
.venv\Scripts\Activate.ps1

pip install -r requirements.txt
cp config.example.py config.py    # or copy on Windows
# edit config.py and set ANTHROPIC_API_KEY (or use a .env file)
python app.py
```

Then http://localhost:8080.

## Project layout

```
mealplanner/
├── app.py              Flask routes + APScheduler for recurring runs
├── ai_planner.py       Claude prompts + tool-use schemas (parse-error-free output)
├── nutrition.py        Toddler nutrition reference (NHMRC NRV)
├── database.py         SQLite layer
├── config.example.py   Copy to config.py and edit
├── templates/          Jinja templates (index, plan, toddler, pantry, …)
├── static/             Single CSS file, no JS framework
├── systemd/            Service unit
└── scripts/install_pi.sh
```

## Implementation notes

### Why tool-use, not free-form JSON

Earlier versions asked Claude to "respond only in JSON". This works most of the time, but at 7 days × 3 meals + shopping list, the output gets long enough that occasional malformed JSON (stray commas, unescaped quotes) crashes the parse. Switching to the Anthropic SDK's tool-use feature with a forced `tool_choice` means the model fills in fields against a JSON schema rather than hand-writing JSON syntax. The SDK validates and the response is structurally guaranteed valid.

### Why short summaries are constrained

When given a free-form summary field, models will happily spend their entire token budget on a beautiful description of the plan and run out before populating the actual `days` array — leaving you with a "0 days planned" page. The schema enforces `maxLength: 200` and the system prompt reinforces a 25-word cap. There's also a post-hoc truncate as belt-and-braces.

### Why the database is SQLite, not Postgres

The Pi Zero W has 512 MB of RAM. SQLite costs nothing at idle, requires no separate process, and easily handles a household's worth of meal plans (probably ~5 KB per week). If this ever needs to scale to multiple households, swap it for Postgres later — the database layer is ~200 lines of raw SQL in `database.py`.

## Privacy

The only data leaving your network is the planning prompt sent to Anthropic — your household preferences, pantry, dislikes, and feedback. No location coordinates are sent (just the suburb name you put in Settings). No photos, no contacts. The SQLite database stays on the Pi.

## Costs

Using `claude-haiku-4-5` (default), one weekly plan generation costs roughly a few cents. Toddler plans similar. If you switch to `claude-sonnet-4-6` in `config.py` you get richer plans for ~5–10× the cost.

## Disclaimer

The toddler planner uses NHMRC reference values and standard Australian under-2s safety guidance, but it isn't a substitute for paediatric dietetic advice — particularly if your child has any allergies, growth concerns, or is on the fussier end. Treat the toddler plans as a starting point and adjust on instinct.

## License

MIT — see [LICENSE](LICENSE).

## Contributing

Pull requests welcome. Areas that could use love:

- Email/SMS the shopping list when a plan generates
- Multi-supermarket price comparison via real APIs
- Per-day cuisine overrides
- Internationalisation beyond Australia
