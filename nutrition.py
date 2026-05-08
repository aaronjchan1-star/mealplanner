"""
Toddler nutrition reference (12–36 months).

Targets are based on the Australian NHMRC Nutrient Reference Values for ages 1–3.
These are the values the AI is told to hit when planning a toddler's day.

This module is intentionally simple — it returns daily targets and a list of
"focus nutrients" that toddlers commonly miss, which we feed into the prompt.
"""

from typing import Dict, List


# Daily targets for ages ~1–3, mostly per NHMRC NRV (Australia/NZ).
# Energy is a band because activity varies; the AI gets the midpoint.
DAILY_TARGETS_1_TO_3 = {
    "energy_kj":            (4200, 5400),    # ~1000–1300 kcal
    "protein_g":             14,
    "fat_pct_of_energy":     (30, 40),       # higher than adults — brain dev
    "fibre_g":               14,
    "iron_mg":               9,              # often under-consumed
    "zinc_mg":               3,
    "calcium_mg":            500,
    "iodine_ug":             90,
    "vit_a_ug_re":           300,
    "vit_c_mg":              35,
    "vit_d_ug":              5,              # plus sun exposure
    "omega3_dha_mg":         40,             # focus nutrient for brain dev
    "added_sugars_g_max":    15,             # WHO guideline, conservative
    "sodium_mg_max":         1000,           # AI/UL is 1000 mg/day
}


# Foods to avoid or limit for under-2s — fed into the prompt as hard constraints.
TODDLER_AVOID = [
    "honey under 12 months (botulism risk)",
    "whole nuts and large hard chunks (choking)",
    "raw or undercooked egg, meat, or seafood",
    "high-mercury fish (shark, swordfish, marlin)",
    "added salt — keep sodium under 1000 mg/day",
    "added sugar drinks, juice over 125 mL/day",
    "low-fat dairy under age 2",
    "very spicy or strongly caffeinated foods",
]


# Texture and serving size guidance for ~18-month-olds.
TEXTURE_GUIDE_18M = (
    "Toddlers around 18 months can chew most family foods if cut to safe sizes. "
    "Avoid round, firm, slippery shapes (whole grapes, cherry tomatoes, sausage rounds — "
    "always quartered lengthways). Soft finger foods and self-feeding from a spoon should both "
    "be offered. Aim for 3 small meals + 2 snacks per day, ~2/3 the size of an adult portion."
)


# Nutrients commonly under-consumed by Australian toddlers — emphasise in plans.
FOCUS_NUTRIENTS = [
    "iron (red meat, lentils, fortified cereal, leafy greens with vitamin C)",
    "omega-3 DHA (oily fish like salmon 1–2x/week, eggs)",
    "calcium (full-fat dairy, fortified plant milk if dairy-free)",
    "vitamin D (sun + oily fish + eggs)",
    "fibre (vegetables, fruit with skin, wholegrains)",
    "iodine (iodised salt in cooking water — but limit total sodium)",
]


def toddler_brief(age_months: int) -> Dict:
    """Return a structured brief the AI planner uses for toddler plans."""
    return {
        "age_months": age_months,
        "stage": _stage(age_months),
        "daily_targets": DAILY_TARGETS_1_TO_3,
        "avoid": TODDLER_AVOID,
        "focus_nutrients": FOCUS_NUTRIENTS,
        "texture_guidance": TEXTURE_GUIDE_18M if age_months <= 24 else
            "From 2y, most adult textures are fine — keep cutting round/firm foods small.",
    }


def _stage(age_months: int) -> str:
    if age_months < 12:
        return "infant — this planner is not designed for under 12 months; please consult a paediatric dietitian"
    if age_months < 18:
        return "early toddler (12–17m): still transitioning to family foods, soft textures, self-feeding"
    if age_months < 24:
        return "toddler (18–23m): family foods with safe cuts, fussy phase common"
    if age_months < 36:
        return "older toddler (2–3y): can handle most adult textures and meals"
    return "preschooler (3y+): adult textures, slightly smaller portions"
