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
4. Keep cooking time and effort within the limits given.
5. Prefer ingredients available at the household's nearby supermarkets (Woolworths, Coles, Aldi, IGA in Australia). Group the shopping list so one trip covers it.
6. Vary cuisines across the week so the household doesn't get bored, but stay inside the cuisines they enjoy.
7. Make sure at least one meal each day works for a toddler — note which ones, with any modifications (less salt, smaller cuts, softer texture).
8. Only use cooking methods that match the household's available appliances. If they don't have an oven, don't roast. If they have an air fryer, lean into it where appropriate.
9. When the user requests batch cooking or freezer-friendly meals, design dinners that are eaten across multiple nights rather than a single sitting. Use the meal `name` to make this explicit (e.g. "Slow-cooker beef ragu — Sunday batch (also serves Mon & Tue)"). Use `storage_notes` to record fridge/freezer life and reheat instructions.

CRITICAL OUTPUT RULES:
- The "summary" field MUST be a single short sentence, maximum 25 words. Do NOT describe the plan in detail there — that detail belongs in the individual meal entries.
- The "days" array MUST contain one entry for every day requested, with at least one meal per day for each requested slot.
- Be realistic about Australian supermarket prices. Don't invent ingredients that aren't sold here.
- Always submit your final plan via the submit_meal_plan tool. Never respond with prose."""


SYSTEM_TODDLER = """You are a paediatric meal planner producing weekly meal plans for a toddler.

You hit the NHMRC daily targets you are given. You prioritise the listed focus nutrients (iron, omega-3 DHA, calcium, vitamin D, fibre, iodine). You avoid every item in the "avoid" list as a HARD constraint.

You think about:
- Texture and choking safety for the given age.
- Iron-rich foods at most days, paired with vitamin C for absorption.
- Oily fish 1–2x per week.
- Variety so the toddler is exposed to many flavours and textures.
- Realistic Australian supermarket ingredients and AUD prices.
- Keeping prep simple — most toddler meals should take under 15 minutes of active prep, and many should be a smaller version of what the family is eating.

CRITICAL OUTPUT RULES:
- The "summary" field MUST be a single short sentence, maximum 25 words. Do NOT describe the plan in detail there — nutritional reasoning belongs in "weekly_nutrition_check", and meal detail belongs in the individual meal entries.
- The "days" array MUST contain one entry for every day requested, each with breakfast, morning_snack, lunch, afternoon_snack, and dinner.
- Always submit your final plan via the submit_toddler_plan tool. Never respond with prose."""


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
) -> Dict[str, Any]:
    """Generate a toddler meal plan that, where possible, piggybacks off the family plan."""

    brief = nutrition.toddler_brief(child["age_months"])

    user_payload = {
        "household": household,
        "child": child,
        "pantry": pantry,
        "dislikes": dislikes,
        "nutrition_brief": brief,
        "family_plan_for_alignment": family_plan,
        "budget_aud": budget_aud,
        "days": days,
    }

    user_msg = (
        f"Plan {days} days of meals and snacks for the toddler described below. "
        "Where the family plan has a dinner that can be made toddler-safe, share it (note any modifications). "
        "Otherwise plan a separate, simple toddler meal. "
        "Hit the daily nutrition targets across the week, with iron-rich food on most days and oily fish 1–2x.\n\n"
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
