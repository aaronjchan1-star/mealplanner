# How the meal plans get made

A walk-through of what actually happens when you click **Generate plan**. Worth reading once so you can tune what you ask for.

## The short version

1. The form values you fill in get bundled together with your saved settings, pantry contents, dislike list, and the last 15 pieces of meal feedback.
2. That bundle is sent to Claude (Anthropic's AI), with a structured "tool schema" describing exactly what fields the response must contain.
3. Claude fills in the schema. The Anthropic SDK validates the response against the schema before returning it — malformed output is structurally impossible.
4. The result gets saved to SQLite and rendered.

A typical plan generation takes 15–30 seconds. Almost all of that is waiting for Claude — the Pi does almost no work.

## What goes into every plan

When you click Generate, here's what gets assembled into the prompt:

### From the form

- **Budget** in AUD, with a "per week" or "per meal" scope
- **Number of days** (2–7)
- **Max active cooking time** per meal
- **Max ingredients** per meal
- **Meal slots** — breakfast, lunch, and/or dinner
- **Available appliances** — oven, stovetop, microwave, slow cooker, air fryer, etc.
- **Cooking strategy** — batch cook, freezer-friendly, microwave-reheats
- **Cuisines you love** (free text, comma separated)
- **Diet notes** (free text — anything goes)

### From your saved Settings

- Suburb and currency
- Number of adults
- Nearby supermarkets (the planner groups the shopping list by these)
- Max travel time to a supermarket
- Children — name and date of birth (age computed fresh on every plan, so toddler nutrition stays accurate as your child grows)

### From the database

- **Pantry** — everything you've listed, with quantities. The planner prefers using these before adding new items.
- **Dislikes** — treated as hard "never include" constraints, not soft preferences.
- **Recent feedback** — the last 15 thumbs-up/thumbs-down votes you've cast on individual meals. Used to steer taste over time.

### For toddler plans, additionally

- The child's age in months (computed from their DOB)
- The NHMRC nutrient reference values for that age (iron, calcium, protein, fibre, etc.)
- The under-2 avoid list (honey, raw fish, low-fat dairy, etc.) — hard constraints
- Texture and serving size guidance for the age
- A focus-nutrients list of things toddlers commonly under-eat (iron, omega-3 DHA, calcium, vitamin D, fibre, iodine)
- Optionally: an existing family plan, so the toddler meals can borrow toddler-safe versions of family dinners

## What Claude is told to do

The "system prompt" sets the planner's personality and priorities. For the **family planner**, in priority order:

1. **Stay within budget** — hard ceiling, not a target
2. **Use the pantry first** — minimizes the shopping list
3. **Respect dislikes and allergies as forbidden** — never include
4. **Stay within cooking time and effort limits**
5. **Prefer ingredients from your nearby supermarkets** — groups the list for one trip
6. **Vary cuisines** — no Italian five nights in a row
7. **Ensure at least one toddler-safe meal per day**
8. **Only use the appliances you have** — if you don't have an oven, no roasts
9. **Honour the cooking strategy** — batch cooks span multiple days, freezer-friendly items get freeze/reheat instructions, microwave-reheatable meals avoid components that go limp

For the **toddler planner**, the priority shift is significant: hitting the NHMRC nutrient targets becomes #1, and budget/simplicity are supporting constraints rather than the primary objective. The planner tries to:

- Include iron-rich foods most days, paired with vitamin C for absorption
- Include oily fish 1–2× per week (for omega-3 DHA)
- Use full-fat dairy (for calcium and energy density)
- Hit the daily fibre target with vegetables, fruit, and wholegrains
- Use iodised salt in cooking water (but keep total sodium under 1000 mg/day)
- Vary textures and flavours so the toddler is exposed to a wide repertoire
- Avoid the choking-hazard list and the under-2 unsafe foods

## How the output is structured

Claude doesn't write JSON freehand any more — it fills in a **schema** defined in `ai_planner.py`. There are two main schemas:

- `FAMILY_TOOL_SCHEMA` — exact shape of a family plan: summary, total cost, shopping list, days array, meals array, with strict types on every field.
- `TODDLER_TOOL_SCHEMA` — same idea, with extra fields for `weekly_nutrition_check`, `key_nutrients` per meal, `texture_notes`, and `shares_with_family_meal`.

The Anthropic SDK validates Claude's tool-call against the schema before returning it to our code. The benefit is concrete: we don't get parse errors any more. If a field is wrong, the SDK refuses the response and Claude tries again automatically.

There's also a `summary` field constrained to ~200 characters / 25 words. Earlier the model would write a beautiful 100-word essay in `summary` and run out of tokens before populating `days`, leaving you with "0 days planned". That's been engineered around.

## What Claude is *not* doing

Worth being clear so you have realistic expectations:

- **No live price lookup.** Prices are Claude's estimates based on what it knows about typical Australian supermarket pricing. Usually within ~20%, sometimes off — especially for meat, seafood, and seasonal produce. The planner won't know that flathead is $8/kg cheaper this week.
- **No real-time stock check.** It doesn't know what's actually on the shelves.
- **No routing or travel-time math.** Your "max travel minutes" setting influences which supermarkets it prefers, but doesn't actually compute a route.
- **No precise nutritional database lookup for the toddler plans.** It's directionally right (red meat is iron-rich, salmon has DHA), but not lab-precise. The `weekly_nutrition_check` field is Claude's commentary on how the week meets the targets, not a calculated nutritional breakdown.

Where actual APIs would matter (live prices, in-stock check, routing), there are paid services that do this. For a household tool where you'll mentally check the price on the shelf anyway, AI estimates are good enough.

## The feedback loop — how it learns

When you click thumbs-down on a meal, that gets saved to the `feedback` table. On the next plan generation, the prompt includes "Recent feedback from this household:" with the last 15 entries:

```
{"meal_name": "Tuna Niçoise", "rating": -1, "note": null, ...}
{"meal_name": "Beef pho", "rating": +1, "note": null, ...}
```

So if you've thumbs-downed three pasta dishes, Claude sees that pattern in the context and pulls back on pasta in the next plan. There's no model fine-tuning — it's just context. Simple, but it works for steering taste over a few weeks.

The **Swap** button works differently: it triggers a separate, smaller call to Claude asking for a single replacement meal that fits the same slot, same constraints, but addresses whatever reason you typed in.

## Cost per plan

Roughly:

- 3,000–5,000 input tokens (your preferences, pantry, dislikes, feedback, schema)
- 4,000–8,000 output tokens (a 7-day plan with shopping list)

On `claude-haiku-4-5` (the default), that's around $0.01–$0.03 per plan. A weekly household using this is spending pennies per month.

If you change `MODEL` in `config.py` to `claude-sonnet-4-6`, you get richer, more thoughtful plans for ~5–10× the cost. Still cheap.

## Tuning what you get

Things worth tweaking when the planner isn't quite right:

- **"Too much pasta / chicken / rice"** — add `"avoid heavy reliance on pasta"` to the diet notes box, or thumbs-down the offending meals when they appear.
- **"Too expensive"** — drop the budget. The planner respects it as a hard ceiling, so a $120/week budget will force cheaper choices than $180/week.
- **"Too repetitive"** — list more cuisines in the cuisines field. The planner will spread across them.
- **"Meals are too long"** — lower max active minutes. 15 min is properly fast; 25 min is weeknight easy; 40 min is Sunday-ish.
- **"Doesn't reheat well"** — tick "must reheat well in the microwave". The planner will avoid crispy/textured items that don't survive the lunchbox.

The prompt itself lives in `ai_planner.py` — `SYSTEM_FAMILY` and `SYSTEM_TODDLER`. If you find yourself repeating the same tweak across the diet notes field every time, lift it into the system prompt instead.
