"""HMND AIOps Dashboard — entry script.

Uses Streamlit's `st.navigation(position="hidden")` to suppress the auto
page picker, then renders our own brand + page_link list so the HUMANOID
logo sits at the top-left of the sidebar above all page entries.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st

from backend.config import load_config
from data.db import DB_PATH, init_schema
from data.seed import seed
from frontend.theme import inject, render_brand


st.set_page_config(
    page_title="HMND · AIOps",
    page_icon="https://i.ibb.co/nsfMVMGM/1.png",
    layout="wide",
    initial_sidebar_state="expanded",
)
inject()

CFG = load_config()


def _bootstrap_db() -> None:
    init_schema()
    if (not DB_PATH.exists() or os.environ.get("HMND_FORCE_SEED") == "1") and CFG.demo_data:
        with st.spinner("Initialising demo dataset…"):
            seed()


_bootstrap_db()


# Build page registry (always defined; some hidden behind feature flags).
overview = st.Page("sections/overview.py",     title="Overview",         icon=":material/dashboard:", default=True)
ai_tools = st.Page("sections/ai_tools.py",     title="AI Tools",         icon=":material/auto_awesome:")
costs    = st.Page("sections/costs.py",        title="Costs by People",  icon=":material/payments:")
seats    = st.Page("sections/seats.py",        title="Seats & Licenses", icon=":material/badge:")
devs     = st.Page("sections/developers.py",   title="Developer Usage",  icon=":material/code:")
keys     = st.Page("sections/api_keys.py",     title="API Keys",         icon=":material/key:")
models   = st.Page("sections/models.py",       title="Models",           icon=":material/smart_toy:")
alerts   = st.Page("sections/alerts.py",       title="Alerts",           icon=":material/notifications:")
ceo_brain = st.Page("sections/ceo_brain.py",   title="CEO Brain",        icon=":material/psychology:")
settings = st.Page("sections/settings.py",     title="Settings",         icon=":material/settings:")

pages = [overview, ai_tools, costs, seats, devs, keys, models, alerts, ceo_brain, settings]
if CFG.github_enabled:
    repos   = st.Page("sections/repositories.py", title="Repositories", icon=":material/folder:")
    pr_qual = st.Page("sections/pr_quality.py",   title="PR Quality",   icon=":material/rule:")
    pages = [overview, ai_tools, costs, seats, devs, keys, repos, pr_qual, models, alerts, ceo_brain, settings]

# Hide the auto-rendered page picker so we can fully control sidebar order.
nav = st.navigation(pages, position="hidden")

# 1. Brand at the very top-left of the sidebar.
render_brand()

# 2. Manual page links — appear BELOW the brand, with Material icons.
for page in pages:
    st.sidebar.page_link(page)

nav.run()
