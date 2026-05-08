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
            return render_template("plan_new.html", prefs=prefs, error=str(e)), 500
        return redirect(url_for("plan_view", plan_id=payload["plan_id"]))
    return render_template("plan_new.html", prefs=prefs, error=None)


@app.route("/plan/<int:plan_id>")
def plan_view(plan_id: int):
    plan = db.get_plan(config.DB_PATH, plan_id)
    if not plan:
        abort(404)
    return render_template("plan_view.html", plan=plan)


@app.route("/toddler", methods=["GET", "POST"])
def toddler():
    prefs = db.get_preferences(config.DB_PATH)
    children = prefs.get("children", [])
    if not children:
        return render_template("toddler.html", prefs=prefs, error=(
            "No child is configured. Add one in Settings first."
        ), plans=[])
    if request.method == "POST":
        try:
            payload = _build_toddler_plan_from_form(request.form, prefs)
        except Exception as e:
            log.exception("toddler plan generation failed")
            plans = db.list_plans(config.DB_PATH, audience="toddler", limit=6)
            return render_template("toddler.html", prefs=prefs, error=str(e),
                                   plans=plans), 500
        return redirect(url_for("plan_view", plan_id=payload["plan_id"]))
    plans = db.list_plans(config.DB_PATH, audience="toddler", limit=6)
    return render_template("toddler.html", prefs=prefs, error=None, plans=plans)


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
        prefs["nearby_supermarkets"] = [
            s.strip() for s in request.form.get("supermarkets", "").split(",") if s.strip()
        ]
        # Children: rebuild list from indexed form fields
        names = request.form.getlist("child_name")
        ages = request.form.getlist("child_age_months")
        children = []
        for n, a in zip(names, ages):
            n = (n or "").strip()
            if not n:
                continue
            try:
                children.append({"name": n, "age_months": int(a)})
            except ValueError:
                continue
        prefs["children"] = children
        db.set_preferences(config.DB_PATH, prefs)
        return redirect(url_for("settings"))
    return render_template("settings.html", prefs=db.get_preferences(config.DB_PATH))


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
    child_index = int(form.get("child_index", "0") or "0")
    child = prefs["children"][child_index]

    align_with_plan_id = form.get("align_with_plan_id")
    family_plan = None
    if align_with_plan_id:
        fp = db.get_plan(config.DB_PATH, int(align_with_plan_id))
        if fp:
            family_plan = fp["payload"]

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
                    child=children[0],
                    pantry=db.list_pantry(config.DB_PATH),
                    dislikes=db.list_dislikes(config.DB_PATH),
                    family_plan=None,
                    budget_aud=params.get("budget_aud", 50),
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
