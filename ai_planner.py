"""
AI planner — talks to Claude and returns a structured weekly meal plan.

Uses the Anthropic SDK's tool-use feature with a forced tool_choice. The model
fills in fields that match a JSON schema rather than hand-writing JSON syntax,
so we get parse-error-free output every time.

Key reliability tweaks (learned the hard way):
  - Summary field is constrained to a short length so the model doesn't spend
    its token budget on prose and leave `days` empty.
  - max_tokens raised to 16384 so the full week + shopping list fits.
  - We validate that `days` is non-empty before returning, with a clear error
    if the model returned an empty plan.
"""

import json
import logging
from typing import Any, Dict, List, Optional

from anthropic import Anthropic

import nutrition

log = logging.getLogger("mealplanner.ai")


# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

SYSTEM_FAMILY = """You are a practical family meal planner for an Australian household.

You produce structured weekly meal plans by calling the submit_meal_plan tool. Your priorities, in order:
1. Stay within the household's budget (in AUD).
2. Use ingredients already in the pantry where possible.
3. Respect dislikes, allergies, and dietary needs as HARD constraints — never include them.
4. **DESIGN DINNERS THAT WORK FOR A TODDLER AS-IS.** If the household has a toddler under 3, the default assumption is that the toddler eats the family dinner. That means: no whole nuts, no honey-glazed anything for under-1s, no very high-mercury fish, modest salt level (cook salt-light; adults can add at the table), avoid raw/undercooked anything. Cut hazards (grapes, cherry tomatoes, sausage rounds) should be quartered lengthways. The `toddler_modifications` field records what the toddler needs *differently* from the adults (e.g. "portion to 1/2 cup, cut pasta into 2cm lengths, no chilli on toddler portion"). Adults season at the table. This is a HARD constraint when a toddler is in the household.
5. **Lower-calorie default.** Lean toward dishes that are nutritionally dense but not calorically heavy: more vegetables, leaner protein cuts (chicken thigh fine, but skin off; lean mince; fish; legumes), wholegrain or smaller carb portions, olive oil rather than cream/butter as the main fat. Avoid pastry, deep-fried, very cheesy bakes, and cream-based sauces as the default. Per-serve calorie estimate for the standard adult portion should usually land in **450-650 kcal**, occasionally up to 750 for a deliberately satisfying meal. NOT a diet plan — meals should still be properly satisfying, just designed thoughtfully.
6. **Plated portions, per-person.** This household eats individually plated meals, not family-style serve-yourself. Every meal MUST include a `portion_strategies` array with one entry per adult eater — at minimum a "Standard adult" entry, and a "Lifter" entry if the household has a lifter on the day's training days. Each entry records what's actually on that person's plate: protein source quantity, carb/veg quantities, estimated grams of protein and kcal for that serve. The toddler is NOT included in portion_strategies — toddler details continue to go in `toddler_modifications`.
7. **Training-day protein.** If the day is a training day for a household lifter and a protein target is set, the lifter's `portion_strategies` entry must deliver the target (commonly 35-50g). Achieve this through plate-up choices (extra protein on the lifter's plate, or a simple add-on like a boiled egg or a serve of cottage cheese on the side) rather than by re-engineering the dish. On non-training days the lifter's portion can be the same as the standard adult portion.
8. **Breakfast is simple.** When breakfast is in the requested meal slots, do NOT design a recipe. Every breakfast entry should be: name "Weetabix and milk" (or a similar fibre cereal — same family across the week), `active_minutes` = 2, `total_minutes` = 2, method = ["Pour cereal into a bowl. Add milk. Eat before the morning gets away from you."], ingredients = ["Weetabix (or similar fibre cereal)", "milk"]. Set `portion_strategies` with one "Standard adult" entry — roughly 3 biscuits + 250ml milk per serve, ~12g protein, ~250 kcal. If a lifter portion is needed on a training day, bump to 4 biscuits + 300ml milk + a tub of Greek yoghurt on the side and adjust the numbers (~30g protein, ~400 kcal). Then make sure the shopping list includes enough cereal and milk for the week (assume ~3 biscuits per adult per breakfast day, and ~250ml milk per adult per breakfast day plus extra for coffee/tea/cooking). Don't propose toast, eggs, smoothies, avocado, granola, or anything else for breakfast — the household has decided breakfast is cereal so they can get to work.
9. Keep cooking time and effort within the limits given.
10. Prefer ingredients available at the household's nearby supermarkets (Woolworths, Coles, Aldi, IGA in Australia). Group the shopping list so one trip covers it.
11. Vary cuisines across the week so the household doesn't get bored, but stay inside the cuisines they enjoy.
12. Only use cooking methods that match the household's available appliances. If they don't have an oven, don't roast. If they have an air fryer, lean into it where appropriate.
13. **Batch-cook mode (when enabled).** When the user has batch mode on, the household cooks the majority of dinners in one prep session on prep day (Sunday by default), then reheats through the week. This changes how you design the plan:

    a. **2-3 dinners per week are "fresh-cooked" on the night** — these are the variety nights, typically pan-fried, stir-fried, seared, or otherwise things that don't reheat well. The remaining nights are "batch-cooked" — designed to be cooked once on prep day in bulk, portioned into containers, and reheated.

    b. **Use the `prep_phase` field on every meal:** set to "batch" for meals cooked on prep day, "fresh" for meals cooked entirely on the night. There should be roughly 4-5 "batch" dinners and 2-3 "fresh" dinners per 7-day week, adjusted proportionally for shorter weeks.

    c. **Lean batch-cooked nights toward dishes that reheat well**: ragus, bolognese, curries, stews, casseroles, slow-cooked meats, shepherd's pie, mince dishes, soups, rice-based bakes. Avoid stir-fries, pan-fried steaks, schnitzel, anything crispy, or anything with cream sauce as batch dinners — those are the fresh-cook options.

    d. **For batch meals: cook the protein on prep day, but vegetables and sides stay fresh.** Don't batch-cook broccoli on Sunday and reheat it Wednesday — it gets soggy. The reheat instructions on the night should be "microwave the meat/sauce portion 2-3 min, steam fresh veg 4 min, plate".

    e. **EVERY batch meal MUST include detailed storage_notes** with three pieces: (1) which container/portion goes where on prep day (fridge vs freezer), (2) how long it keeps in each state, (3) reheat instructions including method and time.

    f. **Always produce a `sunday_prep_session` block at the plan level** when batch mode is on. This is the combined Sunday cooking workflow that produces all batch meals at once — not 4 separate recipes laid on top of each other, but a single coherent 1.5-2 hour session with steps that interleave (e.g. "preheat oven 180°C; while it heats, brown the mince; put chicken in oven; while chicken roasts, finish ragu..."). List the active time, total time, and the final portioning/labelling instructions.

    g. **CRITICAL TODDLER SAFETY** — toddlers under 3 have lower stomach acid and less developed gut flora. Their shelf-life rules are stricter than adults':

       | Storage | Adult cooked meat | TODDLER cooked meat |
       |---|---|---|
       | Fridge, ready to eat | up to 3 days | up to 2 days |
       | Freezer, frozen on prep day | up to 3 months | up to 1 month |
       | Reheats | once | once only, never re-frozen |
       | Cooked fish in fridge | up to 2 days | up to 1 day or freeze |

       The hard rule: **ANY toddler portion of batch-cooked meat that won't be eaten by Monday MUST be frozen on prep day** (Sunday), not left in the fridge. Adult portions can sit in the fridge through Wednesday; toddler portions cannot. So a single batch cook produces two storage streams: adult portions in fridge for Mon/Tue/Wed + freezer for Thu/Fri, and toddler portions all going to freezer except the very first one or two days.

       On fresh-cook nights where the adults are eating something that's not toddler-safe (e.g. stir-fried prawns, rare steak), the toddler eats a thawed batch portion from prep day's cooking — NEVER raw-from-fridge meat that's been sitting since prep day. Every toddler meat portion must be either (a) cooked fresh that day, or (b) frozen on prep day and thawed for that day.

    h. **The shopping list must mark each meat item with its prep destiny**: include a `prep_destiny` field per shopping item set to one of: "batch_sunday" (cooked on prep day with everything else), "fresh_for_<day>" (bought to be cooked fresh on a specific day — ideally late in the week if a top-up shop happens, or kept very cold), or "pantry" (shelf-stable, no special handling).

14. When batch mode is OFF, design meals normally (cooked fresh on the night), and skip the prep_phase / sunday_prep_session / prep_destiny fields.

CRITICAL OUTPUT RULES:
- The "summary" field MUST be a single short sentence, maximum 25 words. Do NOT describe the plan in detail there — that detail belongs in the individual meal entries.
- The "days" array MUST contain one entry for every day requested, with at least one meal per day for each requested slot.
- Every meal MUST have a populated `portion_strategies` array (at least one entry, "Standard adult"). Be realistic with the gram and kcal estimates — don't invent precise numbers, use sensible ranges based on the cuts and quantities you've specified.
- Be realistic about Australian supermarket prices. Don't invent ingredients that aren't sold here.

CRITICAL CONSISTENCY RULES (the user has been bitten by these — do NOT skip them):
- **Every ingredient mentioned in any recipe MUST appear in the shopping_list**, with the exception of: salt, pepper, water, ice, and ingredients you can see in the household's pantry list. Even small quantities like "30g frozen peas" or "1 tsp tomato paste" must be listed if not in pantry — they're things the shopper needs to buy. Before submitting, mentally walk through every recipe and confirm each ingredient is in the shopping list.
- **The `estimated_total_cost_aud` MUST equal the sum of `approx_cost_aud` across the shopping_list to within $0.50**. Do the addition before submitting. If the sum is $50.60, write 50.60, not 39.80.
- These two consistency checks fail more often than you'd expect. The user has explicitly asked for the planner to be careful about them. Spend a few extra tokens checking — it matters more than the prose summary.

- Always submit your final plan via the submit_meal_plan tool. Never respond with prose."""


SYSTEM_TODDLER = """You are a paediatric meal planner producing weekly meal plans for an Australian toddler.

You will be told which meal slots to plan (any subset of breakfast, morning_snack, lunch, afternoon_snack, dinner) and which days. Plan ONLY the slots requested — do not fabricate meals for slots the user didn't ask for.

You will be told the daycare context:
- "weekdays_full": daycare provides breakfast, morning snack, lunch, afternoon snack on weekdays. You're planning what fills the remaining gaps (dinner most days, plus whatever else the user ticked).
- "weekdays_lunch_only": daycare provides only lunch on weekdays.
- "none": no daycare; the toddler eats everything you plan.

You will also be told the specific `daycare_days` (e.g. ["Monday", "Wednesday", "Friday"]). Treat the daycare context as applying ONLY on those days. On non-daycare weekdays, the toddler is home with a parent all day — plan the meal slots requested for those days and design dinners that deliver more of the day's nutrition (since the toddler hasn't been eating concentrated calories at daycare). Dinner portions on non-daycare days can be slightly larger.

You will be told whether the toddler "eats with family":
- If true, dinners should match the FAMILY plan provided. Your job is not to invent new dinners but to record what's different for the toddler (smaller portion, no salt, cut to fork-pieces, etc.) using the `shares_with_family_meal` field and texture_notes. The actual cooking is the same.
- If false, plan a separate, simple toddler dinner.

You will be told whether to design dinners that pack as next-day daycare lunches:
- If true, ensure most dinners produce safe lunchbox leftovers — pack-cold-friendly or gentle-rewarm. Record what to pack and any "add fresh" items in `daycare_lunch_packing_notes`.

NUTRITIONAL APPROACH:
- When daycare provides multiple meals during the week, you are NOT trying to hit full NHMRC daily targets from the dinners you control. You are designing the **anchor meal** that complements what daycare provides. Daycare typically gives a balanced mix of grains, dairy, fruit/veg, and a protein. Your dinners should round out the day — most dinners iron-rich, oily fish 1-2x per week, full-fat dairy somewhere, fibre from vegetables on every dinner.
- When no daycare context is set, you ARE designing the full day's nutrition across the slots you've been asked to plan, and you should hit the full NHMRC daily targets across the week.
- Either way, tag every meal you produce with an `iron_profile`:
   - "heme_rich": meaningful red meat or organ meat in the meal
   - "non_heme_with_c": plant iron source (lentils, fortified cereal, leafy greens, tofu) paired with vitamin C (citrus, tomato, capsicum, kiwi)
   - "non_heme_no_c": plant iron source without a vitamin C pair
   - "low_iron": dinner is low in iron (it's OK to have some of these; not every meal needs iron, but most should)
- Always include `key_nutrients` listing what this meal genuinely contributes (iron, calcium, omega-3, fibre, etc).

MEAL BALANCE (HARD RULE for breakfast, lunch, dinner — NOT required for snacks):
Every main meal MUST contain all three of:
  1. A PROTEIN source (meat, fish, egg, dairy, lentils/legumes, tofu) — for growth.
  2. A CARBOHYDRATE source (rice, pasta, bread, potato, oats, other grains) — toddlers need steady energy and have small stomachs, so every main meal needs an energy base.
  3. A HEALTHY FAT source (olive oil, full-fat dairy, avocado, oily fish, nut butters thinly spread, egg yolk) — toddlers under 2 need a high proportion of dietary fat (30-40% of energy) for brain development. Do not design low-fat toddler meals.
Fill in the `macros` object on every main meal naming the specific protein, carb, and fat in that meal. If you find a meal is missing one of the three, fix the meal before submitting — add a component rather than leaving it unbalanced. Snacks (morning_snack, afternoon_snack) are exempt from this rule and can be lighter (just fruit, just yoghurt, etc.), though a little protein or fat in a snack is welcome.

SAFETY (HARD CONSTRAINTS):
- No whole nuts, whole grapes/cherry tomatoes/sausage rounds (always quarter lengthways)
- No honey under 12 months
- No high-mercury fish (shark, swordfish, marlin)
- No added salt for very young toddlers — flavour with herbs/spices/citrus/garlic
- No raw or undercooked egg, meat, seafood
- Texture matches the age band you're given

TODDLER FOOD-SAFETY SHELF LIFE (STRICTER THAN ADULTS):
When the household runs batch-cook mode, the toddler's portions need stricter handling than the adults'. Toddlers under 3 have lower stomach acid and less developed gut flora.

  | Storage | Toddler cooked meat | Toddler cooked fish |
  |---|---|---|
  | Fridge, cooked | up to 2 days | up to 1 day |
  | Freezer, frozen on prep day | up to 1 month | up to 1 month |
  | Re-freezing thawed meat | NEVER | NEVER |
  | Reheats | once only | once only |

HARD RULE FOR BATCH MODE: any toddler portion that isn't eaten by the day after prep day MUST be frozen on prep day. Record this explicitly in each meal's `texture_notes` or `daycare_lunch_packing_notes`. On fresh-cook adult nights where the toddler can't share, the toddler eats a thawed batch portion from prep day — NEVER raw-from-fridge meat that's been sitting since prep day.

BATCH MODE (when enabled):
- You will be told whether the household runs batch mode, and what prep_day is.
- If batch mode is on AND eats_with_family is on, defer to the family plan's batch structure — the toddler's dinners borrow from family batch portions, frozen on prep day where needed.
- If batch mode is on AND eats_with_family is off, design toddler dinners so the proteins are cooked together on prep_day and either fridge-stored (up to 2 days) or frozen in single-portion containers (up to 1 month). Use `texture_notes` to record the prep-day storage AND the reheat method ("thaw overnight in fridge, microwave 60 sec medium, stir, check temperature, serve").
- Cooked vegetables for the toddler are best made fresh on the night (steamed broccoli, blanched carrot sticks) — these are quick enough that batch isn't needed.

OUTPUT:
- Always submit via the submit_toddler_plan tool, never prose.
- Summary: ONE sentence, max 25 words. Nutritional reasoning goes in `weekly_nutrition_check`.
- The `days` array must contain ONE entry for every day requested.
- Each day's `meals` array contains ONLY the slots requested — no extras.

CRITICAL CONSISTENCY RULES (the user has been bitten by these — do NOT skip them):
- **Every ingredient mentioned in any recipe MUST appear in the shopping_list**, with the exception of: salt, pepper, water, ice, and ingredients in the household's pantry list. Even small quantities (30g frozen peas, 1 tsp tomato paste) need to be on the list — they're things the shopper has to buy. Walk through every recipe before submitting and confirm.
- **The `estimated_total_cost_aud` MUST equal the sum of `approx_cost_aud` across the shopping_list to within $0.50**. Do the addition before submitting. Don't quote the household's stated budget as the total — quote the actual sum of items.
- These two consistency checks fail more often than you'd expect. Spend a few extra tokens checking."""


# ---------------------------------------------------------------------------
# Tool schemas — these define the exact shape of the output
# ---------------------------------------------------------------------------

FAMILY_TOOL_SCHEMA = {
    "name": "submit_meal_plan",
    "description": "Submit the completed weekly meal plan for the family.",
    "input_schema": {
        "type": "object",
        "required": ["summary", "estimated_total_cost_aud", "shopping_list", "days"],
        "properties": {
            "summary": {
                "type": "string",
                "description": "ONE short sentence, maximum 25 words. Do not describe the plan in detail here.",
                "maxLength": 200,
            },
            "estimated_total_cost_aud": {
                "type": "number",
                "description": "Sum of all meal costs in AUD.",
            },
            "sunday_prep_session": {
                "type": "object",
                "description": "Required when batch mode is on. The unified Sunday prep workflow that produces all batch meals at once — one coherent session, not many separate recipes stacked. Omit entirely when batch mode is off.",
                "properties": {
                    "active_minutes": {
                        "type": "integer",
                        "description": "Hands-on time for the whole session.",
                    },
                    "total_minutes": {
                        "type": "integer",
                        "description": "End-to-end time including oven/simmer time.",
                    },
                    "steps": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Interleaved steps that handle multiple meals in parallel. e.g. ['Preheat oven 180°C', 'While oven heats: brown 500g beef mince in a large pot', 'Put chicken thighs (800g) in oven, 25 min', 'Add passata, garlic, onion to mince; simmer 20 min', ...]. Aim for 8-15 steps that produce 4-5 batch meals.",
                    },
                    "portioning_and_storage": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "After cooking is done, exactly how to portion and where to store. Be explicit about adult vs toddler portions and fridge vs freezer destinations. e.g. ['Beef ragu: 2 adult portions to fridge for Mon/Tue dinners; 1 adult portion to freezer for Thu; 3 toddler-sized portions to freezer (use ice cube trays or small containers)', 'Roast chicken: pull meat off bone, ...']",
                    },
                    "containers_needed": {
                        "type": "string",
                        "description": "Rough count of containers/bags needed. e.g. '4 fridge containers, 6 freezer-safe bags, 4 small toddler-portion containers'",
                    },
                },
            },
            "shopping_list": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["item", "approx_cost_aud"],
                    "properties": {
                        "item": {"type": "string"},
                        "quantity": {"type": "string", "description": "e.g. '500 g' or '2 bunches'"},
                        "best_at": {
                            "type": "string",
                            "description": "Woolworths, Coles, Aldi, IGA, or 'any'",
                        },
                        "approx_cost_aud": {"type": "number"},
                        "prep_destiny": {
                            "type": "string",
                            "enum": ["batch_sunday", "fresh_for_wed", "fresh_for_thu", "fresh_for_fri", "fresh_for_sat", "fresh_for_sun", "pantry", ""],
                            "description": "For meat/fish: 'batch_sunday' = cooked all together on prep day, then portioned and stored. 'fresh_for_<day>' = bought to be cooked fresh on a specific weeknight (use the day closest to the cook night so the meat stays fresh). 'pantry' = shelf-stable. Empty for vegetables / dairy / non-perishable items where the user doesn't need a prep destiny.",
                        },
                    },
                },
            },
            "days": {
                "type": "array",
                "minItems": 1,
                "description": "ONE entry per day. Must not be empty.",
                "items": {
                    "type": "object",
                    "required": ["day", "meals"],
                    "properties": {
                        "day": {"type": "string", "description": "Monday, Tuesday, etc."},
                        "meals": {
                            "type": "array",
                            "minItems": 1,
                            "items": {
                                "type": "object",
                                "required": ["slot", "name", "ingredients", "method"],
                                "properties": {
                                    "slot": {
                                        "type": "string",
                                        "description": "breakfast, lunch, dinner, etc.",
                                    },
                                    "name": {"type": "string"},
                                    "cuisine": {"type": "string"},
                                    "active_minutes": {"type": "integer"},
                                    "total_minutes": {"type": "integer"},
                                    "difficulty": {
                                        "type": "string",
                                        "enum": ["easy", "medium", "hard"],
                                    },
                                    "servings": {"type": "integer"},
                                    "estimated_cost_aud": {"type": "number"},
                                    "ingredients": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                    },
                                    "method": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "description": "Step-by-step instructions.",
                                    },
                                    "toddler_friendly": {"type": "boolean"},
                                    "toddler_modifications": {
                                        "type": "string",
                                        "description": "e.g. 'omit salt, cut pasta small' or empty if same as adults",
                                    },
                                    "uses_pantry": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "description": "Items from the household pantry used in this meal",
                                    },
                                    "storage_notes": {
                                        "type": "string",
                                        "description": "Fridge/freezer life and reheat instructions, if relevant. e.g. 'Keeps 3 days in the fridge. Microwave covered, 90 sec.' Empty if not applicable.",
                                    },
                                    "prep_phase": {
                                        "type": "string",
                                        "enum": ["batch", "fresh"],
                                        "description": "When batch-cook mode is on: 'batch' = cooked on prep day (Sunday) and reheated on the night. 'fresh' = cooked entirely on the night (variety nights). 'batch' meals MUST also fill in storage_notes and reheat_instructions thoroughly. Omit when batch mode is off.",
                                    },
                                    "reheat_instructions": {
                                        "type": "string",
                                        "description": "For batch meals only: exact reheat method on the night, separated for adult vs toddler portions if they differ. e.g. 'Adult: microwave covered 2 min on high, stir, 1 min more. Toddler: thaw overnight in fridge then microwave 60 sec on medium, stir, check temperature.'",
                                    },
                                    "appliances_used": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "description": "Which appliances this meal needs (oven, stovetop, microwave, air fryer, etc.)",
                                    },
                                    "portion_strategies": {
                                        "type": "array",
                                        "description": "Per-eater plate breakdown for adults. ALWAYS include at least one entry ('Standard adult'). Add a 'Lifter' entry on training days when a lifter is in the household. Do NOT include toddler entries here — toddler info goes in toddler_modifications.",
                                        "items": {
                                            "type": "object",
                                            "required": ["person", "serve_description", "protein_g_estimate", "kcal_estimate"],
                                            "properties": {
                                                "person": {
                                                    "type": "string",
                                                    "description": "Who this plate is for. Common values: 'Standard adult', 'Lifter', 'Lighter serve'.",
                                                },
                                                "serve_description": {
                                                    "type": "string",
                                                    "description": "What's actually on this plate, in plain English. e.g. '180g chicken thigh, 2/3 cup brown rice, large handful of broccoli'",
                                                },
                                                "protein_g_estimate": {
                                                    "type": "integer",
                                                    "description": "Estimated grams of protein for this serve. Sensible range: 20-60.",
                                                },
                                                "kcal_estimate": {
                                                    "type": "integer",
                                                    "description": "Estimated kcal for this serve. Sensible range: 400-800 for adult meals.",
                                                },
                                                "addons": {
                                                    "type": "string",
                                                    "description": "Optional simple side that distinguishes this plate, e.g. 'plus a boiled egg' or 'plus a tub of plain yoghurt'. Empty if none.",
                                                },
                                            },
                                        },
                                    },
                                },
                            },
                        },
                    },
                },
            },
        },
    },
}


TODDLER_TOOL_SCHEMA = {
    "name": "submit_toddler_plan",
    "description": "Submit the completed weekly meal plan for the toddler.",
    "input_schema": {
        "type": "object",
        "required": ["summary", "estimated_total_cost_aud", "shopping_list", "days"],
        "properties": {
            "summary": {
                "type": "string",
                "description": "ONE short sentence, maximum 25 words. Detail goes elsewhere.",
                "maxLength": 200,
            },
            "estimated_total_cost_aud": {"type": "number"},
            "weekly_nutrition_check": {
                "type": "object",
                "description": "Map of nutrient -> short description of how the week meets the target.",
                "properties": {
                    "iron": {"type": "string"},
                    "omega3_dha": {"type": "string"},
                    "calcium": {"type": "string"},
                    "fibre": {"type": "string"},
                    "notes": {"type": "string"},
                },
            },
            "shopping_list": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["item", "approx_cost_aud"],
                    "properties": {
                        "item": {"type": "string"},
                        "quantity": {"type": "string"},
                        "approx_cost_aud": {"type": "number"},
                    },
                },
            },
            "days": {
                "type": "array",
                "minItems": 1,
                "description": "ONE entry per day. Must not be empty.",
                "items": {
                    "type": "object",
                    "required": ["day", "meals"],
                    "properties": {
                        "day": {"type": "string"},
                        "meals": {
                            "type": "array",
                            "minItems": 1,
                            "items": {
                                "type": "object",
                                "required": ["slot", "name", "ingredients", "method"],
                                "properties": {
                                    "slot": {
                                        "type": "string",
                                        "description": "breakfast, morning_snack, lunch, afternoon_snack, dinner",
                                    },
                                    "name": {"type": "string"},
                                    "active_minutes": {"type": "integer"},
                                    "total_minutes": {"type": "integer"},
                                    "estimated_cost_aud": {"type": "number"},
                                    "ingredients": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                    },
                                    "method": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                    },
                                    "key_nutrients": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                    },
                                    "macros": {
                                        "type": "object",
                                        "description": "REQUIRED for breakfast, lunch, dinner. The protein/carb/fat breakdown for this meal. Each names the specific food providing it. Omit for snacks.",
                                        "properties": {
                                            "protein": {"type": "string", "description": "e.g. 'minced beef', 'red lentils', 'egg', 'full-fat yoghurt'"},
                                            "carb": {"type": "string", "description": "e.g. 'soft pasta', 'mashed potato', 'rice', 'wholemeal toast'"},
                                            "fat": {"type": "string", "description": "e.g. 'olive oil', 'full-fat dairy', 'avocado', 'oily fish'"},
                                        },
                                    },
                                    "texture_notes": {
                                        "type": "string",
                                        "description": "e.g. 'cut grapes lengthways into quarters'",
                                    },
                                    "shares_with_family_meal": {
                                        "type": "string",
                                        "description": "Name of family meal this is based on, or empty",
                                    },
                                    "iron_profile": {
                                        "type": "string",
                                        "enum": ["heme_rich", "non_heme_with_c", "non_heme_no_c", "low_iron"],
                                        "description": "Iron quality of this meal. heme_rich = red/organ meat. non_heme_with_c = plant iron paired with vitamin C. non_heme_no_c = plant iron alone. low_iron = neither.",
                                    },
                                    "daycare_lunch_packing_notes": {
                                        "type": "string",
                                        "description": "If this dinner is intended to also become tomorrow's daycare lunch, record what to pack, what to add fresh, and any reheat-vs-cold guidance. Empty if not applicable.",
                                    },
                                },
                            },
                        },
                    },
                },
            },
        },
    },
}


SWAP_TOOL_SCHEMA = {
    "name": "submit_replacement_meal",
    "description": "Submit a single replacement meal.",
    "input_schema": {
        "type": "object",
        "required": ["slot", "name", "ingredients", "method"],
        "properties": {
            "slot": {"type": "string"},
            "name": {"type": "string"},
            "cuisine": {"type": "string"},
            "active_minutes": {"type": "integer"},
            "total_minutes": {"type": "integer"},
            "difficulty": {"type": "string", "enum": ["easy", "medium", "hard"]},
            "servings": {"type": "integer"},
            "estimated_cost_aud": {"type": "number"},
            "ingredients": {"type": "array", "items": {"type": "string"}},
            "method": {"type": "array", "items": {"type": "string"}},
            "toddler_friendly": {"type": "boolean"},
            "toddler_modifications": {"type": "string"},
            "uses_pantry": {"type": "array", "items": {"type": "string"}},
        },
    },
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MAX_TOKENS = 16384  # generous so a 7-day plan always fits


def _client(api_key: str, base_url: Optional[str] = None) -> Anthropic:
    """Build an Anthropic client. If base_url is set, requests are routed via
    Cloudflare AI Gateway (or any other compatible proxy)."""
    if base_url:
        return Anthropic(api_key=api_key, base_url=base_url)
    return Anthropic(api_key=api_key)


def _extract_tool_input(msg, tool_name: str) -> Dict[str, Any]:
    """Pull the tool-use block out of an Anthropic Message and return its input dict."""
    for block in msg.content:
        if getattr(block, "type", None) == "tool_use" and block.name == tool_name:
            return block.input
    text_parts = [b.text for b in msg.content if getattr(b, "type", None) == "text"]
    raise RuntimeError(
        f"Model did not call {tool_name}. Response was: {' | '.join(text_parts)[:500]}"
    )


def _truncate_summary(plan: Dict[str, Any], max_words: int = 25) -> None:
    """Some models still ramble in summary. Trim it in-place."""
    s = plan.get("summary", "")
    if not s:
        return
    words = s.split()
    if len(words) > max_words:
        plan["summary"] = " ".join(words[:max_words]).rstrip(".,") + "…"


def _validate_plan_or_raise(plan: Dict[str, Any], expected_days: int) -> None:
    days = plan.get("days") or []
    if not days:
        raise RuntimeError(
            "Model returned an empty plan (no days). This usually means the response "
            "hit the token limit before the meals were generated. Try a shorter plan "
            "(fewer days, or fewer meal slots), or a more capable model."
        )
    if len(days) < expected_days:
        log.warning(
            "Plan has %d days but %d were requested", len(days), expected_days
        )


# Ingredients the model can safely assume the household has on hand.
# We don't flag missing-from-shopping-list for these.
_ALWAYS_PANTRY = {
    "salt", "pepper", "black pepper", "white pepper", "sea salt",
    "water", "ice", "tap water",
    "olive oil",  # near-universal — and listed in pantry by default for most households
    "vegetable oil", "cooking oil", "neutral oil", "canola oil", "sunflower oil",
    "cooking spray",
}


def _normalize_ingredient(text: str) -> str:
    """Lower-case, strip parenthetical quantities, drop common prefixes
    like 'fresh', 'finely diced', etc. Used for loose matching."""
    import re
    s = (text or "").lower()
    # strip parenthetical content like "(80g, finely diced)"
    s = re.sub(r"\([^)]*\)", "", s)
    # strip leading quantities/units like "30g " or "1 tsp " or "2 cloves "
    s = re.sub(
        r"^\s*(\d+(?:\.\d+)?\s*(?:g|kg|ml|l|tsp|tbsp|cup|cups|cloves?|cans?|tins?|bunches?|heads?|pieces?)?)\s*",
        "", s,
    )
    # drop common descriptors
    for w in ["fresh", "frozen", "dried", "raw", "cooked", "finely", "diced", "minced",
              "grated", "chopped", "sliced", "whole", "free-range", "organic"]:
        s = s.replace(w, "")
    s = re.sub(r"\s+", " ", s).strip(" ,.-:")
    return s


def _ingredient_matches_shopping(ing_norm: str, shopping_norms: List[str]) -> bool:
    """Loose match — true if any shopping item is a substring of the ingredient,
    or vice versa. Catches 'frozen peas' vs 'peas', 'beef mince' vs 'minced beef'."""
    if not ing_norm:
        return True
    for s in shopping_norms:
        if not s:
            continue
        if ing_norm in s or s in ing_norm:
            return True
        # Also check word overlap — "minced beef" vs "beef mince"
        ing_words = set(ing_norm.split())
        s_words = set(s.split())
        # Drop very-short tokens to avoid spurious matches on 1-2 letter words
        ing_words = {w for w in ing_words if len(w) > 2}
        s_words = {w for w in s_words if len(w) > 2}
        if ing_words and s_words and len(ing_words & s_words) >= max(1, min(len(ing_words), len(s_words)) - 0):
            return True
    return False


def _audit_and_fix_plan(
    plan: Dict[str, Any],
    pantry: List[Dict[str, Any]],
    audience: str = "family",
) -> Dict[str, Any]:
    """After the model returns, sanity-check two known failure modes:

    1. `estimated_total_cost_aud` doesn't equal the sum of `approx_cost_aud` on the
       shopping list. We trust the line items and override the stated total.

    2. Ingredients mentioned in recipes are missing from the shopping list. We
       record these as warnings — and append a stub line for each missing item
       so the user sees them on their shopping list (with price 0, flagged).

    Mutates the plan dict in place and attaches an `audit` block with what was
    fixed. Returns the same plan dict for convenience.
    """
    audit = {
        "total_corrected": None,
        "missing_ingredients": [],
        "batch_warnings": [],
        "macro_warnings": [],
    }

    shopping = plan.get("shopping_list") or []

    # ---- Fix 1: re-sum the line items ----
    line_sum = sum((item.get("approx_cost_aud") or 0) for item in shopping)
    stated = plan.get("estimated_total_cost_aud") or 0
    if line_sum > 0 and abs(line_sum - stated) > 0.50:
        log.warning(
            "Plan total mismatch: stated $%.2f, line items sum to $%.2f. Overriding.",
            stated, line_sum,
        )
        audit["total_corrected"] = {
            "stated": round(stated, 2),
            "actual_sum": round(line_sum, 2),
        }
        plan["estimated_total_cost_aud"] = round(line_sum, 2)

    # ---- Fix 2: detect ingredients missing from shopping list ----
    pantry_norms = [_normalize_ingredient(p.get("name", "")) for p in (pantry or [])]
    shopping_norms = [_normalize_ingredient(s.get("item", "")) for s in shopping]
    # Add pantry items to the "covered" set so we don't flag them
    covered_norms = shopping_norms + pantry_norms

    # Walk all recipe ingredients across all meals
    missing_seen = set()
    for day in (plan.get("days") or []):
        for meal in (day.get("meals") or []):
            for ing in (meal.get("ingredients") or []):
                norm = _normalize_ingredient(ing)
                if not norm:
                    continue
                # Skip always-pantry items
                if any(p in norm for p in _ALWAYS_PANTRY):
                    continue
                if not _ingredient_matches_shopping(norm, covered_norms):
                    # Use the original ingredient string for the warning,
                    # but de-dupe on the normalized form
                    if norm not in missing_seen:
                        missing_seen.add(norm)
                        audit["missing_ingredients"].append(ing)

    # Add stub shopping-list entries for missing items so users see them at
    # checkout. Mark them with a zero price and a known-bad flag so the UI
    # can call them out.
    if audit["missing_ingredients"]:
        log.warning(
            "Plan has %d ingredient(s) missing from shopping list: %s",
            len(audit["missing_ingredients"]),
            audit["missing_ingredients"][:5],
        )
        for ing in audit["missing_ingredients"]:
            shopping.append({
                "item": ing,
                "quantity": "(check quantity)",
                "best_at": "",
                "approx_cost_aud": 0,
                "auto_added": True,
            })
        plan["shopping_list"] = shopping

    # ---- Fix 3: batch-mode consistency ----
    # Only check if the plan claims to be in batch mode (has sunday_prep_session
    # or any meal with prep_phase set). Otherwise these checks don't apply.
    has_batch_signal = bool(plan.get("sunday_prep_session"))
    if not has_batch_signal:
        for day in (plan.get("days") or []):
            for meal in (day.get("meals") or []):
                if meal.get("prep_phase"):
                    has_batch_signal = True
                    break
            if has_batch_signal:
                break

    if has_batch_signal:
        # 3a. Every meal with prep_phase=batch must have storage_notes + reheat_instructions
        for day in (plan.get("days") or []):
            for meal in (day.get("meals") or []):
                if meal.get("prep_phase") == "batch":
                    if not (meal.get("storage_notes") or "").strip():
                        audit["batch_warnings"].append(
                            f"\"{meal.get('name','(unnamed)')}\" is batch-cooked but has no storage notes."
                        )
                    if not (meal.get("reheat_instructions") or "").strip():
                        audit["batch_warnings"].append(
                            f"\"{meal.get('name','(unnamed)')}\" is batch-cooked but has no reheat instructions."
                        )
        # 3b. The sunday_prep_session should have at least a few steps and portioning lines
        prep_session = plan.get("sunday_prep_session") or {}
        if not prep_session.get("steps"):
            audit["batch_warnings"].append(
                "The prep-day session is missing — batch mode is on but no Sunday prep workflow was produced."
            )
        elif len(prep_session.get("steps") or []) < 3:
            audit["batch_warnings"].append(
                f"The prep-day session is very short ({len(prep_session.get('steps') or [])} steps) — it may not cover all the batch meals."
            )
        if not prep_session.get("portioning_and_storage"):
            audit["batch_warnings"].append(
                "The prep-day session has no portioning & storage instructions — you'll know what to cook but not where to put it."
            )

    if audit["batch_warnings"]:
        log.warning(
            "Plan has %d batch-mode warning(s): %s",
            len(audit["batch_warnings"]),
            audit["batch_warnings"][:3],
        )

    # ---- Fix 4: toddler main-meal macro balance ----
    # Every breakfast/lunch/dinner for a toddler should declare a protein and a
    # carb (fat is encouraged but not hard-flagged, since it often comes from
    # cooking oil the model may not always declare). Snacks are exempt.
    if audience == "toddler":
        MAIN_SLOTS = {"breakfast", "lunch", "dinner"}
        for day in (plan.get("days") or []):
            for meal in (day.get("meals") or []):
                slot = (meal.get("slot") or "").lower()
                if slot not in MAIN_SLOTS:
                    continue
                macros = meal.get("macros") or {}
                missing = []
                if not (macros.get("protein") or "").strip():
                    missing.append("protein")
                if not (macros.get("carb") or "").strip():
                    missing.append("carb")
                if missing:
                    audit["macro_warnings"].append(
                        f"{day.get('day','?')} {slot} (\"{meal.get('name','unnamed')}\") is missing a "
                        f"{' and '.join(missing)} — toddler main meals should have protein + carb + fat."
                    )
        if audit["macro_warnings"]:
            log.warning(
                "Toddler plan has %d macro-balance warning(s): %s",
                len(audit["macro_warnings"]),
                audit["macro_warnings"][:3],
            )

    plan["audit"] = audit
    return plan


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------

def build_family_plan(
    api_key: str,
    model: str,
    *,
    household: Dict[str, Any],
    pantry: List[Dict[str, Any]],
    dislikes: List[Dict[str, Any]],
    recent_feedback: List[Dict[str, Any]],
    budget_aud: float,
    budget_scope: str,            # 'per_meal' | 'per_week'
    max_active_minutes: int,
    max_ingredients: int,
    meal_slots: List[str],        # ['dinner'] or ['breakfast','lunch','dinner']
    cuisines_loved: List[str],
    diet_notes: str,
    days: int = 7,
    appliances: Optional[List[str]] = None,
    cooking_strategy: Optional[Dict[str, bool]] = None,
    lifter_protein_target: Optional[int] = None,
    training_days: Optional[List[str]] = None,
    calibration: Optional[Dict[str, Any]] = None,
    batch_mode: bool = True,
    prep_day: str = "Sunday",
    fresh_cook_nights: int = 2,
    has_toddler: bool = False,
) -> Dict[str, Any]:
    """Generate a family meal plan and return the validated dict.

    `calibration` is the multiplier dict returned by db.calibration_multiplier.
    When the household consistently spends more (or less) than estimates, we
    pass that ratio in so the planner can tighten its own estimates.

    `batch_mode`: when True (default), most dinners are cooked on `prep_day`
    and reheated through the week. `fresh_cook_nights` controls how many
    nights per week stay fresh-cooked for variety. `has_toddler` triggers
    stricter shelf-life rules in the prompt.
    """

    appliances = appliances or ["oven", "stovetop", "microwave"]
    cooking_strategy = cooking_strategy or {}
    training_days = training_days or []

    user_payload = {
        "household": household,
        "pantry": pantry,
        "dislikes": dislikes,
        "recent_feedback": recent_feedback,
        "budget_aud": budget_aud,
        "budget_scope": budget_scope,
        "max_active_minutes_per_meal": max_active_minutes,
        "max_ingredients_per_meal": max_ingredients,
        "meal_slots": meal_slots,
        "cuisines_loved": cuisines_loved,
        "diet_notes": diet_notes,
        "available_appliances": appliances,
        "cooking_strategy": cooking_strategy,
        "lifter_protein_target_g": lifter_protein_target,
        "training_days": training_days,
        "batch_mode": batch_mode,
        "prep_day": prep_day,
        "fresh_cook_nights_per_week": fresh_cook_nights,
        "has_toddler": has_toddler,
        "days": days,
    }

    strategy_notes = []
    if batch_mode:
        toddler_note = (
            " THERE IS A TODDLER IN THE HOUSEHOLD. Apply the stricter toddler-safety "
            "shelf-life rules from priority 13(g): toddler cooked-meat portions in the "
            "fridge keep only 2 days (not 3), so any toddler portion that won't be "
            "eaten by the day after prep day MUST be frozen on prep day. Cooked fish "
            "for the toddler keeps only 1 day in the fridge. Every batch meal that "
            "the toddler shares needs explicit toddler-portion freeze instructions "
            "in the portioning_and_storage section of sunday_prep_session and in "
            "each meal's storage_notes."
            if has_toddler else ""
        )
        strategy_notes.append(
            f"BATCH MODE IS ON. Prep day is {prep_day}. Design the week as roughly "
            f"{days - fresh_cook_nights} batch-cooked dinners + {fresh_cook_nights} "
            f"fresh-cooked variety nights. Batch meals are cooked together on {prep_day} "
            f"in a single prep session (described in `sunday_prep_session`), portioned, "
            f"and reheated through the week. Fresh nights are typically mid-to-late week "
            f"(Wed and Fri, or similar) — use them for pan-fried, stir-fried, or seared "
            f"dishes that don't reheat well.\n"
            f"Every meal MUST have `prep_phase` set to 'batch' or 'fresh'. Batch meals "
            f"MUST have `storage_notes` and `reheat_instructions` populated. Shopping list "
            f"meat items MUST have `prep_destiny` set. The plan MUST include a populated "
            f"`sunday_prep_session` block.{toddler_note}"
        )
    if cooking_strategy.get("batch_cook"):
        strategy_notes.append(
            "BATCH COOKING is preferred: design at least two 'anchor' meals "
            "that are cooked once and reused 2-3 times across the week "
            "(e.g. a Sunday roast that becomes Monday wraps and Tuesday fried rice). "
            "When you do this, name the followups explicitly in the meal `name` field "
            "and note the leftover quantities."
        )
    if cooking_strategy.get("freezer_friendly"):
        strategy_notes.append(
            "FREEZER FRIENDLY: prefer meals that freeze well. For each freezable "
            "meal, add to `method` a final step like 'Portion and freeze flat — "
            "thaw overnight, reheat at 180°C 15 min'."
        )
    if cooking_strategy.get("microwave_reheats"):
        strategy_notes.append(
            "Some meals must REHEAT cleanly in the microwave for next-day lunches. "
            "Avoid components that go limp or rubbery (no plain pan-fried steak, "
            "no crispy skin items, etc.) on at least 2-3 dinners. Add reheat "
            "instructions in the method when relevant."
        )
    if lifter_protein_target and training_days:
        strategy_notes.append(
            f"LIFTER IN THE HOUSEHOLD: training days are {', '.join(training_days)}. "
            f"On those days, the meal's `portion_strategies` array must include a "
            f"'Lifter' entry whose serve hits ~{lifter_protein_target}g protein. "
            f"On non-training days, the Lifter entry can be omitted (the lifter eats "
            f"the standard adult portion). Achieve the protein target through portion "
            f"size on the lifter's plate, or simple add-ons (a boiled egg, a tub of "
            f"plain yoghurt, an extra serve of cottage cheese) — never by re-engineering "
            f"the dish for everyone."
        )
    elif lifter_protein_target:
        strategy_notes.append(
            f"LIFTER IN THE HOUSEHOLD with target {lifter_protein_target}g protein per "
            f"serve. No specific training days given — include a 'Lifter' portion_strategy "
            f"on every meal."
        )
    if calibration and calibration.get("ready") and calibration.get("multiplier"):
        m = calibration["multiplier"]
        n = calibration["n"]
        if m >= 1.05:
            strategy_notes.append(
                f"BUDGET CALIBRATION: based on {n} actual receipts, this household consistently "
                f"spends about {(m-1)*100:.0f}% MORE than your AI-estimated totals. Your raw "
                f"price estimates are running low — typically due to brand choices, larger pack "
                f"sizes, or items not in your default training data. Aim to keep your raw "
                f"`estimated_total_cost_aud` at or below ${budget_aud/m:.0f} so that after the "
                f"household's typical {(m-1)*100:.0f}% overshoot, the actual shop will land near "
                f"the ${budget_aud:.0f} budget. Tighten individual `approx_cost_aud` estimates "
                f"proportionally (especially meat, fresh produce, and snack items)."
            )
        elif m <= 0.95:
            strategy_notes.append(
                f"BUDGET CALIBRATION: based on {n} actual receipts, this household typically "
                f"spends about {(1-m)*100:.0f}% LESS than your AI-estimated totals — your "
                f"estimates run high. You can be slightly more generous with portions or "
                f"ingredients while still hitting budget."
            )
        else:
            strategy_notes.append(
                f"BUDGET CALIBRATION: based on {n} actual receipts, your estimates have been "
                f"very close to reality (within 5%). Keep doing what you're doing."
            )
    strategy_block = ("\n\n" + "\n".join(strategy_notes)) if strategy_notes else ""

    user_msg = (
        f"Plan {days} days of meals for the household below. "
        "Treat the dislikes list as forbidden. "
        "Treat the budget as a HARD ceiling — assume Australian supermarket prices."
        f"{strategy_block}\n\n"
        f"Inputs (JSON):\n{json.dumps(user_payload, indent=2)}\n\n"
        "REMEMBER: keep summary to one short sentence (max 25 words). "
        f"The days array MUST contain {days} entries, one for each day. "
        "Submit your final plan via the submit_meal_plan tool."
    )

    msg = _client(api_key).messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_FAMILY,
        tools=[FAMILY_TOOL_SCHEMA],
        tool_choice={"type": "tool", "name": "submit_meal_plan"},
        messages=[{"role": "user", "content": user_msg}],
    )
    plan = _extract_tool_input(msg, "submit_meal_plan")
    _truncate_summary(plan)
    _validate_plan_or_raise(plan, expected_days=days)
    _audit_and_fix_plan(plan, pantry)
    return plan


def build_toddler_plan(
    api_key: str,
    model: str,
    *,
    household: Dict[str, Any],
    child: Dict[str, Any],         # {"name": "...", "age_months": 18}
    pantry: List[Dict[str, Any]],
    dislikes: List[Dict[str, Any]],
    family_plan: Optional[Dict[str, Any]],
    budget_aud: float,
    days: int = 7,
    meal_slots: Optional[List[str]] = None,
    daycare_context: str = "none",        # 'weekdays_full' | 'weekdays_lunch_only' | 'none'
    eats_with_family: bool = False,
    daycare_lunch_reuse: bool = False,
    weekend_meal_slots: Optional[List[str]] = None,
    daycare_days: Optional[List[str]] = None,
    batch_mode: bool = True,
    prep_day: str = "Sunday",
) -> Dict[str, Any]:
    """Generate a toddler meal plan.

    meal_slots: which slots to plan on WEEKDAYS. Defaults to ['dinner'].
    weekend_meal_slots: which slots to plan on weekends. Defaults to meal_slots.
    daycare_context: 'weekdays_full' means daycare provides breakfast / snacks /
                     lunch. 'weekdays_lunch_only' means just lunch. 'none' means
                     plan everything.
    daycare_days: which specific weekdays the toddler attends daycare, e.g.
                  ['Monday', 'Wednesday', 'Friday']. If empty, assumed all
                  weekdays. Only matters when daycare_context != 'none'.
    eats_with_family: when True, dinners borrow from family_plan.
    daycare_lunch_reuse: when True, dinners produce next-day daycare lunches.
                         Only applies on dinners that come *before* a daycare day.
    """

    if meal_slots is None or not meal_slots:
        meal_slots = ["dinner"]
    if weekend_meal_slots is None or not weekend_meal_slots:
        weekend_meal_slots = list(meal_slots)
    # If a daycare context is set but no specific days listed, assume all weekdays.
    if daycare_context != "none" and not daycare_days:
        daycare_days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
    elif daycare_days is None:
        daycare_days = []

    brief = nutrition.toddler_brief(child["age_months"])

    user_payload = {
        "household": household,
        "child": child,
        "pantry": pantry,
        "dislikes": dislikes,
        "nutrition_brief": brief,
        "family_plan_for_alignment": family_plan if eats_with_family else None,
        "budget_aud": budget_aud,
        "days": days,
        "weekday_meal_slots": meal_slots,
        "weekend_meal_slots": weekend_meal_slots,
        "daycare_context": daycare_context,
        "daycare_days": daycare_days,
        "eats_with_family": eats_with_family,
        "daycare_lunch_reuse": daycare_lunch_reuse,
        "batch_mode": batch_mode,
        "prep_day": prep_day,
    }

    # Tailor the user message to the mode the user picked.
    mode_notes = []
    if batch_mode:
        mode_notes.append(
            f"BATCH MODE IS ON. Prep day is {prep_day}. Apply the stricter toddler "
            f"shelf-life rules: cooked meat in fridge ≤2 days, cooked fish ≤1 day. "
            f"Any toddler portion not eaten by the day after {prep_day} MUST be frozen "
            f"on {prep_day} — record this in each meal's texture_notes. On fresh-cook "
            f"adult nights where the toddler can't share, the toddler eats a thawed "
            f"batch portion from {prep_day}'s cooking — never raw-from-fridge meat. "
            f"If eats_with_family is on AND a family plan is provided, defer to the "
            f"family plan's batch structure; otherwise design toddler dinners with "
            f"the proteins cooked together on {prep_day}, portioned into "
            f"single-toddler-serve containers, and reheated through the week."
        )
    if daycare_context == "weekdays_full" and daycare_days:
        dc_list = ", ".join(daycare_days)
        non_dc_weekdays = [d for d in ["Monday","Tuesday","Wednesday","Thursday","Friday"]
                           if d not in daycare_days]
        non_dc_list = ", ".join(non_dc_weekdays) if non_dc_weekdays else "(none)"
        mode_notes.append(
            f"DAYCARE DAYS: {dc_list}. On these days, daycare provides breakfast, "
            f"morning snack, lunch, and afternoon snack — your dinner is the "
            f"nutritional anchor and doesn't need to deliver a full day's iron/calcium.\n"
            f"NON-DAYCARE WEEKDAYS: {non_dc_list}. On these days you are home with "
            f"the toddler all day — plan the meal slots requested (including more "
            f"substantial lunches if asked) and design dinners to deliver more of the "
            f"day's nutrition. Dinner portions may be slightly LARGER on these days "
            f"since the toddler ate less concentrated calories during the day."
        )
    elif daycare_context == "weekdays_lunch_only" and daycare_days:
        dc_list = ", ".join(daycare_days)
        mode_notes.append(
            f"DAYCARE DAYS (lunch only): {dc_list}. On these days, daycare provides "
            f"only lunch. You plan the other slots requested. Treat lunch as a balanced "
            f"input (grain + protein + veg) from daycare and complement around it. "
            f"On non-daycare weekdays, plan lunch too if it's in the slot list."
        )
    if eats_with_family and family_plan:
        mode_notes.append(
            "EATS WITH FAMILY: the toddler dinners on the days the family plan "
            "covers should set `shares_with_family_meal` to the family meal name, "
            "with `texture_notes` recording the cuts/portion changes and "
            "`toddler_modifications`-style guidance. Do NOT invent new dinners on "
            "those days — record the modifications to the family meal."
        )
    if daycare_lunch_reuse and daycare_days:
        # Compute which dinners come BEFORE a daycare day, so we know which ones
        # actually need lunchbox leftovers.
        weekday_order = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
        # The toddler needs a packed lunch tomorrow if tomorrow is a daycare day.
        # So we want dinner-night-before-daycare-day to be lunchbox-ready.
        eve_days = []
        for i, d in enumerate(weekday_order):
            tomorrow = weekday_order[(i + 1) % 7]
            if tomorrow in daycare_days:
                eve_days.append(d)
        eve_list = ", ".join(eve_days)
        mode_notes.append(
            f"DAYCARE LUNCH FROM LEFTOVERS: pack lunches for daycare days. Daycare days are "
            f"{', '.join(daycare_days)}, so the dinners on {eve_list} should produce safe, "
            f"lunchbox-friendly leftovers. On OTHER nights you don't need to design for leftovers — "
            f"those dinners can be smaller / fresher / single-portion. Fill in "
            f"`daycare_lunch_packing_notes` ONLY on the dinners that precede a daycare day."
        )
    elif daycare_lunch_reuse:
        mode_notes.append(
            "DAYCARE LUNCH FROM LEFTOVERS: most dinners should produce a safe, "
            "lunchbox-friendly leftover. Fill in `daycare_lunch_packing_notes` "
            "with what to pack cold, what to add fresh, what to skip."
        )
    mode_block = ("\n\nMODE NOTES:\n- " + "\n- ".join(mode_notes)) if mode_notes else ""

    user_msg = (
        f"Plan {days} days of meals for the toddler described below. "
        "Plan ONLY the meal slots specified for each day (weekday vs weekend). "
        "Respect the dislikes list as forbidden, treat the avoid list as forbidden, "
        "and treat the budget as a HARD ceiling.\n"
        "Iron, omega-3 DHA, and calcium are the focus nutrients — tag each meal "
        "with an iron_profile, and aim for most dinners to be iron_profile=heme_rich "
        "or non_heme_with_c."
        f"{mode_block}\n\n"
        f"Inputs (JSON):\n{json.dumps(user_payload, indent=2, default=str)}\n\n"
        "REMEMBER: keep summary to one short sentence (max 25 words) — put nutrition reasoning in weekly_nutrition_check. "
        f"The days array MUST contain {days} entries. "
        "Submit your final plan via the submit_toddler_plan tool."
    )

    msg = _client(api_key).messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_TODDLER,
        tools=[TODDLER_TOOL_SCHEMA],
        tool_choice={"type": "tool", "name": "submit_toddler_plan"},
        messages=[{"role": "user", "content": user_msg}],
    )
    plan = _extract_tool_input(msg, "submit_toddler_plan")
    _truncate_summary(plan)
    _validate_plan_or_raise(plan, expected_days=days)
    _audit_and_fix_plan(plan, pantry, audience="toddler")
    return plan


def suggest_swap(
    api_key: str,
    model: str,
    *,
    meal: Dict[str, Any],
    reason: str,
    household: Dict[str, Any],
    dislikes: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Ask Claude for a single replacement meal."""

    user_msg = (
        "Suggest a single replacement meal for the one below. "
        "Match the meal slot, keep cooking time and budget similar or lower, "
        "and respect all dislikes. Submit via the submit_replacement_meal tool.\n\n"
        f"Original meal:\n{json.dumps(meal, indent=2)}\n\n"
        f"Reason for swap: {reason}\n"
        f"Dislikes: {json.dumps(dislikes)}\n"
        f"Household: {json.dumps(household)}"
    )

    msg = _client(api_key).messages.create(
        model=model,
        max_tokens=2048,
        system=SYSTEM_FAMILY,
        tools=[SWAP_TOOL_SCHEMA],
        tool_choice={"type": "tool", "name": "submit_replacement_meal"},
        messages=[{"role": "user", "content": user_msg}],
    )
    return _extract_tool_input(msg, "submit_replacement_meal")


# ---------------------------------------------------------------------------
# Single-meal generation — for "it's 5:30pm, what should I make right now"
# ---------------------------------------------------------------------------

QUICK_MEAL_TOOL_SCHEMA = {
    "name": "submit_quick_meal",
    "description": "Submit a single meal idea given on-hand ingredients and constraints.",
    "input_schema": {
        "type": "object",
        "required": ["name", "ingredients", "method"],
        "properties": {
            "name": {"type": "string"},
            "slot": {"type": "string", "description": "breakfast/lunch/dinner/snack — whichever fits the request"},
            "active_minutes": {"type": "integer"},
            "total_minutes": {"type": "integer"},
            "servings": {"type": "integer"},
            "ingredients": {
                "type": "array",
                "items": {"type": "string"},
                "description": "What goes in. Mark items from the user's on-hand list with ✓ at the start, e.g. '✓ salmon fillet 400g'.",
            },
            "method": {"type": "array", "items": {"type": "string"}},
            "toddler_friendly": {"type": "boolean"},
            "toddler_modifications": {"type": "string"},
            "why_this": {
                "type": "string",
                "description": "One sentence: why this works given the constraints (e.g. 'uses everything you mentioned, 18 min total, iron-rich for the toddler')",
            },
            "iron_profile": {
                "type": "string",
                "enum": ["heme_rich", "non_heme_with_c", "non_heme_no_c", "low_iron", "not_relevant"],
            },
        },
    },
}


def quick_meal(
    api_key: str,
    model: str,
    *,
    have_on_hand: str,
    constraints: str,
    household: Dict[str, Any],
    dislikes: List[Dict[str, Any]],
    audience: str = "family",     # 'family' or 'toddler'
    child: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """One-shot meal generation. Used for 'what should I make right now'."""

    extra = ""
    system = SYSTEM_FAMILY
    if audience == "toddler" and child:
        brief = nutrition.toddler_brief(child["age_months"])
        extra = (
            f"\n\nThis meal is for a toddler. Age in months: {child['age_months']}. "
            f"Safety constraints — avoid: {brief['avoid']}. "
            f"Texture guidance: {brief['texture_guidance']}"
        )
        system = SYSTEM_TODDLER

    user_msg = (
        "Suggest a SINGLE meal that uses what's on hand and respects the constraints. "
        "Don't invent ingredients the user didn't say they had unless they're cheap, common pantry items (salt, oil, pepper, garlic, lemon, herbs, common spices, eggs, flour, butter, basic veg). "
        "If the user's constraints can't be met, suggest the closest thing and say what's missing in `why_this`. "
        "Mark on-hand items with ✓ in the ingredients list."
        f"{extra}\n\n"
        f"On hand: {have_on_hand}\n"
        f"Constraints: {constraints}\n"
        f"Household context: {json.dumps(household)}\n"
        f"Dislikes (forbidden): {json.dumps(dislikes)}\n\n"
        "Submit via the submit_quick_meal tool."
    )

    msg = _client(api_key).messages.create(
        model=model,
        max_tokens=2048,
        system=system,
        tools=[QUICK_MEAL_TOOL_SCHEMA],
        tool_choice={"type": "tool", "name": "submit_quick_meal"},
        messages=[{"role": "user", "content": user_msg}],
    )
    return _extract_tool_input(msg, "submit_quick_meal")
