"""
Flask app — kept deliberately small so a Pi Zero W can serve it comfortably.

Run with:
    python app.py
or behind a tiny WSGI server (see scripts/install_pi.sh for systemd setup).
"""

import json
import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, List

from apscheduler.schedulers.background import BackgroundScheduler
from flask import (
    Flask, Response, abort, jsonify, redirect, render_template, request,
    url_for,
)

import ai_planner
import database as db
from reference_data import KNOWN_SUPERMARKETS, KITCHEN_APPLIANCES, NSW_SUBURBS

try:
    import config
except ImportError:
    raise SystemExit(
        "config.py is missing. Copy config.example.py to config.py and fill it in."
    )


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("mealplanner")

app = Flask(__name__)

# Initialise DB on first start
db.init_db(config.DB_PATH, config.HOUSEHOLD)


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    plans = db.list_plans(config.DB_PATH, limit=6)
    schedules = db.list_schedules(config.DB_PATH)
    pantry = db.list_pantry(config.DB_PATH)
    return render_template(
        "index.html",
        plans=plans,
        schedules=schedules,
        pantry_count=len(pantry),
        prefs=db.get_preferences(config.DB_PATH),
    )


@app.route("/plan/new", methods=["GET", "POST"])
def plan_new():
    prefs = db.get_preferences(config.DB_PATH)
    if request.method == "POST":
        try:
            payload = _build_family_plan_from_form(request.form, prefs)
        except Exception as e:
            log.exception("plan generation failed")
            return render_template(
                "plan_new.html", prefs=prefs, error=str(e),
                kitchen_appliances=KITCHEN_APPLIANCES,
            ), 500
        return redirect(url_for("plan_view", plan_id=payload["plan_id"]))
    return render_template(
        "plan_new.html", prefs=prefs, error=None,
        kitchen_appliances=KITCHEN_APPLIANCES,
    )


@app.route("/plan/<int:plan_id>")
def plan_view(plan_id: int):
    plan = db.get_plan(config.DB_PATH, plan_id)
    if not plan:
        abort(404)
    return render_template("plan_view.html", plan=plan)


@app.post("/plan/<int:plan_id>/delete")
def plan_delete(plan_id: int):
    plan = db.get_plan(config.DB_PATH, plan_id)
    if not plan:
        abort(404)
    db.delete_plan(config.DB_PATH, plan_id)
    log.info("deleted plan %s (%s)", plan_id, plan["audience"])
    # Send the user back to the listing for whichever audience this was
    if plan["audience"] == "toddler":
        return redirect(url_for("toddler"))
    return redirect(url_for("index"))


@app.route("/toddler", methods=["GET", "POST"])
def toddler():
    prefs = db.get_preferences(config.DB_PATH)
    raw_children = prefs.get("children", [])
    if not raw_children:
        return render_template("toddler.html", prefs=prefs, error=(
            "No child is configured. Add one in Settings first."
        ), plans=[])
    # Resolve DOB -> age_months for display
    prefs_view = dict(prefs)
    prefs_view["children"] = [_resolve_child(c) for c in raw_children]
    if request.method == "POST":
        try:
            payload = _build_toddler_plan_from_form(request.form, prefs)
        except Exception as e:
            log.exception("toddler plan generation failed")
            plans = db.list_plans(config.DB_PATH, audience="toddler", limit=6)
            return render_template("toddler.html", prefs=prefs_view, error=str(e),
                                   plans=plans), 500
        return redirect(url_for("plan_view", plan_id=payload["plan_id"]))
    plans = db.list_plans(config.DB_PATH, audience="toddler", limit=6)
    return render_template("toddler.html", prefs=prefs_view, error=None, plans=plans)


@app.route("/pantry", methods=["GET", "POST"])
def pantry():
    if request.method == "POST":
        action = request.form.get("action")
        if action == "add":
            db.add_pantry_item(
                config.DB_PATH,
                name=request.form.get("name", ""),
                quantity=request.form.get("quantity"),
                expires_on=request.form.get("expires_on") or None,
            )
        elif action == "remove":
            item_id = int(request.form.get("id", "0"))
            if item_id:
                db.remove_pantry_item(config.DB_PATH, item_id)
        return redirect(url_for("pantry"))
    return render_template("pantry.html", items=db.list_pantry(config.DB_PATH))


@app.route("/dislikes", methods=["GET", "POST"])
def dislikes():
    if request.method == "POST":
        action = request.form.get("action")
        if action == "add":
            db.add_dislike(
                config.DB_PATH,
                item=request.form.get("item", ""),
                kind=request.form.get("kind", "ingredient"),
                person=request.form.get("person", "household"),
            )
        elif action == "remove":
            dislike_id = int(request.form.get("id", "0"))
            if dislike_id:
                db.remove_dislike(config.DB_PATH, dislike_id)
        return redirect(url_for("dislikes"))
    return render_template("dislikes.html", items=db.list_dislikes(config.DB_PATH))


@app.route("/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        prefs = db.get_preferences(config.DB_PATH)
        prefs["location"] = request.form.get("location", prefs.get("location", ""))
        prefs["currency"] = request.form.get("currency", "AUD")
        prefs["adults"] = int(request.form.get("adults", "2") or "2")
        prefs["max_travel_minutes"] = int(
            request.form.get("max_travel_minutes", "15") or "15"
        )
        # Supermarkets: now arrive as checkbox list + an "other" text field
        chains = request.form.getlist("supermarket")
        other = request.form.get("supermarket_other", "").strip()
        if other:
            chains.extend(s.strip() for s in other.split(",") if s.strip())
        # Preserve insertion order, drop duplicates
        prefs["nearby_supermarkets"] = list(dict.fromkeys(chains))
        # Lifter protein target (per-serve, optional). 0 / empty => not set.
        raw_target = request.form.get("lifter_protein_target", "").strip()
        if raw_target:
            try:
                v = int(raw_target)
                prefs["lifter_protein_target"] = v if v > 0 else None
            except ValueError:
                prefs["lifter_protein_target"] = None
        else:
            prefs["lifter_protein_target"] = None
        # Default training days (also overridable per plan).
        prefs["default_training_days"] = [
            d for d in request.form.getlist("default_training_days")
            if d in {"Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"}
        ]
        # Children: rebuild list from indexed form fields. We now prefer DOB
        # (ISO date string) and compute age_months on the fly, but we keep
        # accepting age_months for back-compat / manual override.
        names = request.form.getlist("child_name")
        dobs = request.form.getlist("child_dob")
        children = []
        for n, dob in zip(names, dobs):
            n = (n or "").strip()
            dob = (dob or "").strip()
            if not n:
                continue
            entry = {"name": n}
            if dob:
                entry["dob"] = dob
            children.append(entry)
        prefs["children"] = children
        db.set_preferences(config.DB_PATH, prefs)
        return redirect(url_for("settings"))
    return render_template(
        "settings.html",
        prefs=db.get_preferences(config.DB_PATH),
        known_supermarkets=KNOWN_SUPERMARKETS,
        nsw_suburbs=NSW_SUBURBS,
        today_iso=_today_iso(),
    )


@app.route("/schedules", methods=["GET", "POST"])
def schedules():
    if request.method == "POST":
        action = request.form.get("action")
        if action == "add":
            params = {
                "budget_aud": float(request.form.get("budget_aud", "0") or "0"),
                "budget_scope": request.form.get("budget_scope", "per_week"),
                "max_active_minutes": int(request.form.get("max_active_minutes", "30") or "30"),
                "max_ingredients": int(request.form.get("max_ingredients", "10") or "10"),
                "meal_slots": [s for s in request.form.getlist("meal_slots") if s],
                "cuisines_loved": [c.strip() for c in request.form.get("cuisines_loved", "").split(",") if c.strip()],
                "diet_notes": request.form.get("diet_notes", ""),
            }
            next_run = request.form.get("next_run") or _next_sunday_iso()
            db.add_schedule(
                config.DB_PATH,
                audience=request.form.get("audience", "family"),
                cadence=request.form.get("cadence", "weekly"),
                next_run=next_run,
                params=params,
            )
        elif action == "toggle":
            sid = int(request.form.get("id", "0"))
            active = request.form.get("active") == "1"
            db.toggle_schedule(config.DB_PATH, sid, active)
        elif action == "delete":
            sid = int(request.form.get("id", "0"))
            db.delete_schedule(config.DB_PATH, sid)
        return redirect(url_for("schedules"))
    return render_template("schedules.html", schedules=db.list_schedules(config.DB_PATH))


# ---------------------------------------------------------------------------
# JSON endpoints (for the JS in plan_view.html)
# ---------------------------------------------------------------------------

@app.post("/api/feedback")
def api_feedback():
    data = request.get_json(force=True) or {}
    db.record_feedback(
        config.DB_PATH,
        plan_id=data.get("plan_id"),
        meal_name=data.get("meal_name", ""),
        rating=int(data.get("rating", 0)),
        note=data.get("note"),
    )
    return jsonify({"ok": True})


@app.post("/api/swap")
def api_swap():
    data = request.get_json(force=True) or {}
    prefs = db.get_preferences(config.DB_PATH)
    new_meal = ai_planner.suggest_swap(
        api_key=config.ANTHROPIC_API_KEY,
        model=config.MODEL,
        meal=data["meal"],
        reason=data.get("reason", "user wanted a different option"),
        household=prefs,
        dislikes=db.list_dislikes(config.DB_PATH),
    )
    return jsonify({"ok": True, "meal": new_meal})


@app.route("/meal/quick", methods=["GET", "POST"])
def meal_quick():
    """Single-meal generator — for 'what should I make right now'."""
    prefs = db.get_preferences(config.DB_PATH)
    raw_children = prefs.get("children", [])
    children = [_resolve_child(c) for c in raw_children]
    result = None
    error = None
    form_state = {
        "have_on_hand": "",
        "constraints": "",
        "audience": "family",
        "child_index": "0",
    }
    if request.method == "POST":
        form_state["have_on_hand"] = request.form.get("have_on_hand", "").strip()
        form_state["constraints"] = request.form.get("constraints", "").strip()
        form_state["audience"] = request.form.get("audience", "family")
        form_state["child_index"] = request.form.get("child_index", "0")
        if not form_state["have_on_hand"]:
            error = "Tell me what you've got — even just two or three things."
        else:
            try:
                child = None
                if form_state["audience"] == "toddler" and children:
                    idx = int(form_state["child_index"] or "0")
                    if 0 <= idx < len(children):
                        child = children[idx]
                result = ai_planner.quick_meal(
                    api_key=config.ANTHROPIC_API_KEY,
                    model=config.MODEL,
                    have_on_hand=form_state["have_on_hand"],
                    constraints=form_state["constraints"] or "Standard weeknight: 20-30 min, simple.",
                    household=prefs,
                    dislikes=db.list_dislikes(config.DB_PATH),
                    audience=form_state["audience"],
                    child=child,
                )
            except Exception as e:
                log.exception("quick meal failed")
                error = str(e)
    return render_template(
        "meal_quick.html",
        children=children,
        form_state=form_state,
        result=result,
        error=error,
    )


# ---------------------------------------------------------------------------
# Export endpoints
# ---------------------------------------------------------------------------

@app.get("/plan/<int:plan_id>/export/recipes.txt")
def export_recipes_text(plan_id: int):
    """Plain-text export of all recipes — paste into Notes, email, etc."""
    plan = db.get_plan(config.DB_PATH, plan_id)
    if not plan:
        abort(404)
    body = _format_recipes_text(plan)
    return Response(
        body,
        mimetype="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="recipes-{plan["week_start"]}.txt"'
        },
    )


@app.get("/plan/<int:plan_id>/export/shopping-list.txt")
def export_shopping_text(plan_id: int):
    """Plain-text shopping list — one item per line, supermarket-grouped."""
    plan = db.get_plan(config.DB_PATH, plan_id)
    if not plan:
        abort(404)
    body = _format_shopping_text(plan)
    return Response(
        body,
        mimetype="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="shopping-{plan["week_start"]}.txt"'
        },
    )


@app.get("/plan/<int:plan_id>/export/plan.html")
def export_plan_html(plan_id: int):
    """Print-friendly HTML export of everything — open in browser, Cmd/Ctrl+P to PDF."""
    plan = db.get_plan(config.DB_PATH, plan_id)
    if not plan:
        abort(404)
    return render_template("export_plan.html", plan=plan)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _format_recipes_text(plan: Dict[str, Any]) -> str:
    """Render the entire plan as plain text — easy to paste anywhere."""
    out = []
    p = plan["payload"]
    audience = "TODDLER" if plan["audience"] == "toddler" else "FAMILY"
    out.append(f"{audience} MEAL PLAN — week of {plan['week_start']}")
    out.append("=" * 60)
    if p.get("summary"):
        out.append(p["summary"])
    if p.get("estimated_total_cost_aud"):
        out.append(f"Estimated total: ${p['estimated_total_cost_aud']:.2f} AUD")
    out.append("")

    for day in p.get("days", []):
        out.append(f"\n{day['day'].upper()}")
        out.append("-" * 60)
        for meal in day.get("meals", []):
            out.append(f"\n  [{meal.get('slot','').upper()}] {meal.get('name','')}")
            tags = []
            if meal.get("cuisine"):
                tags.append(meal["cuisine"])
            if meal.get("active_minutes"):
                tags.append(f"{meal['active_minutes']} min active")
            if meal.get("servings"):
                tags.append(f"{meal['servings']} serves")
            if meal.get("estimated_cost_aud"):
                tags.append(f"${meal['estimated_cost_aud']:.2f}")
            if tags:
                out.append("  " + " · ".join(tags))
            if meal.get("ingredients"):
                out.append("\n  Ingredients:")
                for ing in meal["ingredients"]:
                    out.append(f"    - {ing}")
            if meal.get("method"):
                out.append("\n  Method:")
                for i, step in enumerate(meal["method"], 1):
                    out.append(f"    {i}. {step}")
            if meal.get("portion_strategies"):
                out.append("\n  On the plates:")
                for p_ in meal["portion_strategies"]:
                    line = f"    - {p_.get('person','?')}: {p_.get('serve_description','')}"
                    bits = []
                    if p_.get("protein_g_estimate"):
                        bits.append(f"~{p_['protein_g_estimate']}g protein")
                    if p_.get("kcal_estimate"):
                        bits.append(f"~{p_['kcal_estimate']} kcal")
                    if bits:
                        line += f"  ({', '.join(bits)})"
                    out.append(line)
                    if p_.get("addons"):
                        out.append(f"      + {p_['addons']}")
            if meal.get("toddler_modifications"):
                out.append(f"\n  For the little one: {meal['toddler_modifications']}")
            if meal.get("texture_notes"):
                out.append(f"  Texture: {meal['texture_notes']}")
    return "\n".join(out)


def _format_shopping_text(plan: Dict[str, Any]) -> str:
    """Plain-text shopping list, grouped by supermarket where available."""
    out = []
    p = plan["payload"]
    out.append(f"SHOPPING LIST — week of {plan['week_start']}")
    out.append("=" * 60)

    items = p.get("shopping_list", []) or []
    if not items:
        out.append("(empty)")
        return "\n".join(out)

    # Group by best_at if any items have it; otherwise flat list
    has_groups = any(i.get("best_at") for i in items)
    if has_groups:
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for item in items:
            key = item.get("best_at") or "any"
            groups.setdefault(key, []).append(item)
        for store, store_items in sorted(groups.items()):
            out.append(f"\n{store.upper()}")
            out.append("-" * 30)
            for it in store_items:
                line = f"[ ] {it['item']}"
                if it.get("quantity"):
                    line += f" — {it['quantity']}"
                if it.get("approx_cost_aud"):
                    line += f"  (${it['approx_cost_aud']:.2f})"
                out.append(line)
    else:
        for it in items:
            line = f"[ ] {it['item']}"
            if it.get("quantity"):
                line += f" — {it['quantity']}"
            if it.get("approx_cost_aud"):
                line += f"  (${it['approx_cost_aud']:.2f})"
            out.append(line)

    if p.get("estimated_total_cost_aud"):
        out.append("")
        out.append(f"TOTAL ESTIMATE: ${p['estimated_total_cost_aud']:.2f} AUD")
    return "\n".join(out)


def _build_family_plan_from_form(form, prefs: Dict[str, Any]) -> Dict[str, Any]:
    budget_aud = float(form.get("budget_aud", "0") or "0")
    budget_scope = form.get("budget_scope", "per_week")
    max_active_minutes = int(form.get("max_active_minutes", "30") or "30")
    max_ingredients = int(form.get("max_ingredients", "10") or "10")
    meal_slots = [s for s in form.getlist("meal_slots") if s] or ["dinner"]
    cuisines_loved = [c.strip() for c in form.get("cuisines_loved", "").split(",") if c.strip()]
    diet_notes = form.get("diet_notes", "")
    days = int(form.get("days", "7") or "7")
    days = max(2, min(7, days))  # clamp to allowed range
    appliances = [a for a in form.getlist("appliances") if a]
    cooking_strategy = {
        "batch_cook": form.get("batch_cook") == "1",
        "freezer_friendly": form.get("freezer_friendly") == "1",
        "microwave_reheats": form.get("microwave_reheats") == "1",
    }

    # Lifter protein target lives in Settings but can be overridden per plan.
    # 0 / empty means "no lifter, don't tailor protein".
    raw_target = form.get("lifter_protein_target", "").strip()
    if not raw_target:
        raw_target = str(prefs.get("lifter_protein_target", "") or "")
    try:
        lifter_protein_target = int(raw_target) if raw_target else None
        if lifter_protein_target == 0:
            lifter_protein_target = None
    except ValueError:
        lifter_protein_target = None
    training_days = [d for d in form.getlist("training_days") if d in
                     {"Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"}]

    plan = ai_planner.build_family_plan(
        api_key=config.ANTHROPIC_API_KEY,
        model=config.MODEL,
        household=prefs,
        pantry=db.list_pantry(config.DB_PATH),
        dislikes=db.list_dislikes(config.DB_PATH),
        recent_feedback=db.recent_feedback(config.DB_PATH, limit=15),
        budget_aud=budget_aud,
        budget_scope=budget_scope,
        max_active_minutes=max_active_minutes,
        max_ingredients=max_ingredients,
        meal_slots=meal_slots,
        cuisines_loved=cuisines_loved,
        diet_notes=diet_notes,
        days=days,
        appliances=appliances,
        cooking_strategy=cooking_strategy,
        lifter_protein_target=lifter_protein_target,
        training_days=training_days,
    )
    plan_id = db.save_plan(
        config.DB_PATH,
        week_start=_today_iso(),
        audience="family",
        payload=plan,
        budget_cents=int(round(budget_aud * 100)),
    )
    return {"plan_id": plan_id}


def _build_toddler_plan_from_form(form, prefs: Dict[str, Any]) -> Dict[str, Any]:
    budget_aud = float(form.get("budget_aud", "0") or "0")
    days = int(form.get("days", "7") or "7")
    days = max(2, min(7, days))
    child_index = int(form.get("child_index", "0") or "0")
    child = _resolve_child(prefs["children"][child_index])

    align_with_plan_id = form.get("align_with_plan_id")
    family_plan = None
    if align_with_plan_id:
        fp = db.get_plan(config.DB_PATH, int(align_with_plan_id))
        if fp:
            family_plan = fp["payload"]

    meal_slots = [s for s in form.getlist("meal_slots") if s] or ["dinner"]
    weekend_meal_slots = [s for s in form.getlist("weekend_meal_slots") if s] or meal_slots
    daycare_context = form.get("daycare_context", "none")
    if daycare_context not in {"none", "weekdays_full", "weekdays_lunch_only"}:
        daycare_context = "none"
    eats_with_family = form.get("eats_with_family") == "1" and family_plan is not None
    daycare_lunch_reuse = form.get("daycare_lunch_reuse") == "1"
    daycare_days = [d for d in form.getlist("daycare_days") if d in
                    {"Monday", "Tuesday", "Wednesday", "Thursday", "Friday"}]

    plan = ai_planner.build_toddler_plan(
        api_key=config.ANTHROPIC_API_KEY,
        model=config.MODEL,
        household=prefs,
        child=child,
        pantry=db.list_pantry(config.DB_PATH),
        dislikes=db.list_dislikes(config.DB_PATH),
        family_plan=family_plan,
        budget_aud=budget_aud,
        days=days,
        meal_slots=meal_slots,
        weekend_meal_slots=weekend_meal_slots,
        daycare_context=daycare_context,
        eats_with_family=eats_with_family,
        daycare_lunch_reuse=daycare_lunch_reuse,
        daycare_days=daycare_days,
    )
    plan_id = db.save_plan(
        config.DB_PATH,
        week_start=_today_iso(),
        audience="toddler",
        payload=plan,
        budget_cents=int(round(budget_aud * 100)),
    )
    return {"plan_id": plan_id}


def _today_iso() -> str:
    return date.today().isoformat()


def _next_sunday_iso() -> str:
    today = date.today()
    days_ahead = (6 - today.weekday()) % 7 or 7
    return (today + timedelta(days=days_ahead)).isoformat()


def _resolve_child(child: Dict[str, Any]) -> Dict[str, Any]:
    """Return a child dict with age_months always populated.

    Children are stored with DOB (preferred, accurate forever) and optionally
    a manually-entered age_months (legacy). DOB wins when present.
    """
    out = dict(child)
    if child.get("dob"):
        try:
            dob = datetime.fromisoformat(child["dob"]).date()
            today = date.today()
            months = (today.year - dob.year) * 12 + (today.month - dob.month)
            if today.day < dob.day:
                months -= 1
            out["age_months"] = max(0, months)
        except (ValueError, TypeError):
            log.warning("could not parse dob %r for child %s",
                        child.get("dob"), child.get("name"))
    if "age_months" not in out:
        # Last resort — if neither DOB nor age_months is set, assume 18m
        # so the toddler-brief logic still has something to work with.
        out["age_months"] = 18
    return out


# ---------------------------------------------------------------------------
# Background scheduler — runs recurring plan generation
# ---------------------------------------------------------------------------

def _run_scheduled_plans():
    log.info("scheduler tick")
    today = date.today().isoformat()
    prefs = db.get_preferences(config.DB_PATH)
    for s in db.list_schedules(config.DB_PATH):
        if not s["active"] or s["next_run"] > today:
            continue
        try:
            params = s["params"]
            if s["audience"] == "family":
                plan = ai_planner.build_family_plan(
                    api_key=config.ANTHROPIC_API_KEY,
                    model=config.MODEL,
                    household=prefs,
                    pantry=db.list_pantry(config.DB_PATH),
                    dislikes=db.list_dislikes(config.DB_PATH),
                    recent_feedback=db.recent_feedback(config.DB_PATH, limit=15),
                    budget_aud=params.get("budget_aud", 200),
                    budget_scope=params.get("budget_scope", "per_week"),
                    max_active_minutes=params.get("max_active_minutes", 30),
                    max_ingredients=params.get("max_ingredients", 10),
                    meal_slots=params.get("meal_slots", ["dinner"]),
                    cuisines_loved=params.get("cuisines_loved", []),
                    diet_notes=params.get("diet_notes", ""),
                )
                db.save_plan(config.DB_PATH, today, "family", plan,
                             int(round(params.get("budget_aud", 0) * 100)))
            elif s["audience"] == "toddler":
                children = prefs.get("children", [])
                if not children:
                    continue
                plan = ai_planner.build_toddler_plan(
                    api_key=config.ANTHROPIC_API_KEY,
                    model=config.MODEL,
                    household=prefs,
                    child=_resolve_child(children[0]),
                    pantry=db.list_pantry(config.DB_PATH),
                    dislikes=db.list_dislikes(config.DB_PATH),
                    family_plan=None,
                    budget_aud=params.get("budget_aud", 50),
                    days=params.get("days", 7),
                    meal_slots=params.get("meal_slots", ["dinner"]),
                    weekend_meal_slots=params.get("weekend_meal_slots", None),
                    daycare_context=params.get("daycare_context", "none"),
                    eats_with_family=params.get("eats_with_family", False),
                    daycare_lunch_reuse=params.get("daycare_lunch_reuse", False),
                    daycare_days=params.get("daycare_days", None),
                )
                db.save_plan(config.DB_PATH, today, "toddler", plan,
                             int(round(params.get("budget_aud", 0) * 100)))
            # Move next_run forward
            step = 7 if s["cadence"] == "weekly" else 14
            new_next = (datetime.fromisoformat(s["next_run"]).date() +
                        timedelta(days=step)).isoformat()
            db.update_schedule_next_run(config.DB_PATH, s["id"], new_next)
            log.info("scheduled plan generated for schedule %s", s["id"])
        except Exception:
            log.exception("scheduled plan failed for schedule %s", s["id"])


def _start_scheduler():
    sched = BackgroundScheduler(timezone="Australia/Sydney")
    # Check once a day at 6am
    sched.add_job(_run_scheduled_plans, "cron", hour=6, minute=0)
    sched.start()
    return sched


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    _start_scheduler()
    app.run(host=config.HOST, port=config.PORT, debug=config.DEBUG, threaded=True)
