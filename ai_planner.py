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
5. Keep cooking time and effort within the limits given.
6. Prefer ingredients available at the household's nearby supermarkets (Woolworths, Coles, Aldi, IGA in Australia). Group the shopping list so one trip covers it.
7. Vary cuisines across the week so the household doesn't get bored, but stay inside the cuisines they enjoy.
8. Only use cooking methods that match the household's available appliances. If they don't have an oven, don't roast. If they have an air fryer, lean into it where appropriate.
9. When the user requests batch cooking or freezer-friendly meals, design dinners that are eaten across multiple nights rather than a single sitting. Use the meal `name` to make this explicit (e.g. "Slow-cooker beef ragu — Sunday batch (also serves Mon & Tue)"). Use `storage_notes` to record fridge/freezer life and reheat instructions.

CRITICAL OUTPUT RULES:
- The "summary" field MUST be a single short sentence, maximum 25 words. Do NOT describe the plan in detail there — that detail belongs in the individual meal entries.
- The "days" array MUST contain one entry for every day requested, with at least one meal per day for each requested slot.
- Be realistic about Australian supermarket prices. Don't invent ingredients that aren't sold here.
- Always submit your final plan via the submit_meal_plan tool. Never respond with prose."""


SYSTEM_TODDLER = """You are a paediatric meal planner producing weekly meal plans for an Australian toddler.

You will be told which meal slots to plan (any subset of breakfast, morning_snack, lunch, afternoon_snack, dinner) and which days. Plan ONLY the slots requested — do not fabricate meals for slots the user didn't ask for.

You will be told the daycare context:
- "weekdays_full": daycare provides breakfast, morning snack, lunch, afternoon snack on weekdays. You're planning what fills the remaining gaps (dinner most days, plus whatever else the user ticked).
- "weekdays_lunch_only": daycare provides only lunch on weekdays.
- "none": no daycare; the toddler eats everything you plan.

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

SAFETY (HARD CONSTRAINTS):
- No whole nuts, whole grapes/cherry tomatoes/sausage rounds (always quarter lengthways)
- No honey under 12 months
- No high-mercury fish (shark, swordfish, marlin)
- No added salt for very young toddlers — flavour with herbs/spices/citrus/garlic
- No raw or undercooked egg, meat, seafood
- Texture matches the age band you're given

OUTPUT:
- Always submit via the submit_toddler_plan tool, never prose.
- Summary: ONE sentence, max 25 words. Nutritional reasoning goes in `weekly_nutrition_check`.
- The `days` array must contain ONE entry for every day requested.
- Each day's `meals` array contains ONLY the slots requested — no extras."""


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
                                    "appliances_used": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "description": "Which appliances this meal needs (oven, stovetop, microwave, air fryer, etc.)",
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
) -> Dict[str, Any]:
    """Generate a family meal plan and return the validated dict."""

    appliances = appliances or ["oven", "stovetop", "microwave"]
    cooking_strategy = cooking_strategy or {}

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
        "days": days,
    }

    strategy_notes = []
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
) -> Dict[str, Any]:
    """Generate a toddler meal plan.

    meal_slots: which slots to plan on WEEKDAYS. Defaults to ['dinner'] which
                matches the common daycare-kid case.
    weekend_meal_slots: which slots to plan on weekends. Defaults to whatever
                       meal_slots is set to.
    daycare_context: 'weekdays_full' means daycare provides breakfast / snacks /
                     lunch on weekdays (so don't try to hit full nutrition from
                     dinner alone). 'weekdays_lunch_only' means daycare provides
                     just lunch. 'none' means plan everything.
    eats_with_family: when True, dinners borrow from family_plan and the toddler
                      plan just records modifications. Requires family_plan.
    daycare_lunch_reuse: when True, dinners should produce safe leftovers that
                         become the next day's daycare lunch.
    """

    if meal_slots is None or not meal_slots:
        meal_slots = ["dinner"]
    if weekend_meal_slots is None or not weekend_meal_slots:
        weekend_meal_slots = list(meal_slots)

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
        "eats_with_family": eats_with_family,
        "daycare_lunch_reuse": daycare_lunch_reuse,
    }

    # Tailor the user message to the mode the user picked.
    mode_notes = []
    if daycare_context == "weekdays_full":
        mode_notes.append(
            "WEEKDAY DAYCARE COVERS most meals — focus on the dinners (and any "
            "weekend meals requested) as the nutritional anchor. Don't try to "
            "stuff a full day's iron/calcium/fibre into a single weekday dinner."
        )
    elif daycare_context == "weekdays_lunch_only":
        mode_notes.append(
            "DAYCARE COVERS LUNCH on weekdays. The other slots requested are "
            "yours to plan. Treat lunch as a balanced grain+protein+veg input "
            "from daycare and complement it across the rest of the day."
        )
    if eats_with_family and family_plan:
        mode_notes.append(
            "EATS WITH FAMILY: the toddler dinners on the days the family plan "
            "covers should `shares_with_family_meal` set to the family meal name, "
            "with `texture_notes` recording the cuts/portion changes and "
            "`toddler_modifications`-style guidance. Do NOT invent new dinners on "
            "those days — record the modifications to the family meal."
        )
    if daycare_lunch_reuse:
        mode_notes.append(
            "DAYCARE LUNCH FROM LEFTOVERS: most dinners should produce a safe, "
            "lunchbox-friendly leftover for the next weekday. Fill in "
            "`daycare_lunch_packing_notes` with what to pack cold, what to add "
            "fresh in the morning (cucumber sticks, cheese, fruit), and whether "
            "any item shouldn't be packed at all (e.g. anything that needs to "
            "stay crispy)."
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
