# Copy this file to config.py and fill in your values.
# config.py is gitignored so your secrets stay local to the Pi.

import os

# --- Anthropic API ---
# Get a key at https://console.anthropic.com
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "sk-ant-...")

# Model used for meal planning. claude-haiku-4-5 is fast and cheap.
# Use claude-sonnet-4-6 if you want richer suggestions and don't mind paying more.
MODEL = "claude-haiku-4-5"

# --- Household defaults ---
# These can be edited later via the Settings page; this is just the initial seed.
HOUSEHOLD = {
    "location": "Lidcombe, Sydney, NSW, Australia",
    "currency": "AUD",
    "adults": 2,
    "children": [
        # Add one entry per child. age_months is what drives toddler nutrition logic.
        {"name": "Bub", "age_months": 18},
    ],
    "nearby_supermarkets": ["Woolworths", "Coles", "Aldi", "IGA"],
    "max_travel_minutes": 15,
}

# --- Database ---
DB_PATH = os.environ.get("MEALPLANNER_DB", "/home/pi/mealplanner/data.db")

# --- Server ---
HOST = "0.0.0.0"   # listen on all interfaces so the family can hit it from phones
PORT = 8080
DEBUG = False
