import html
import json
import logging
import os
import re
import time
from datetime import datetime
from urllib.parse import parse_qs, urljoin, urlparse

import psycopg
import requests
from bs4 import BeautifulSoup
from psycopg.types.json import Jsonb


# ============================================================
# KONFIGURACJA
# ============================================================

DATABASE_URL = os.environ["DATABASE_URL"]

MATCH_API_URL = (
    "https://tk2-228-23746.vs.sakura.ne.jp/"
    "n01/tournament/n01_user_t.php?cmd=match_view&sid="
)

LEAGUE_API_URL = (
    "https://tk2-228-23746.vs.sakura.ne.jp/"
    "n01/league/n01_league.php"
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/151.0 Safari/537.36"
)

REQUEST_TIMEOUT = 30
REQUEST_DELAY_SECONDS = 0.6
MAX_DISCOVERY_PAGES_PER_SOURCE = 100


# Szukamy wyłącznie jawnego parametru tmid w adresie.
# Nie przeszukujemy dowolnych fragmentów JavaScript.
TMID_PATTERN = re.compile(
    r"[?&]tmid=([A-Za-z0-9_-]+)",
    re.IGNORECASE,
)


# ============================================================
# LOGOWANIE
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)


# ============================================================
# SESJA HTTP
# ============================================================

session = requests.Session()

session.headers.update(
    {
        "User-Agent": USER_AGENT,
        "Accept": (
            "text/html,application/xhtml+xml,"
            "application/json;q=0.9,*/*;q=0.8"
        ),
        "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.8",
    }
)


# ==============
