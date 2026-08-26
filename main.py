import json
import logging
import os
import re
import time
from datetime import datetime
from urllib.parse import parse_qs, urlparse

import psycopg
import requests
from psycopg.types.json import Jsonb


# ============================================================
# KONFIGURACJA
# ============================================================

DATABASE_URL = os.environ["DATABASE_URL"]

LEAGUE_API_URL = (
    "https://tk2-228-23746.vs.sakura.ne.jp/"
    "n01/league/n01_league.php"
)

TOURNAMENT_HISTORY_API_URL = (
    "https://tk2-228-23746.vs.sakura.ne.jp/"
    "n01/tournament/n01_history.php"
)

MATCH_API_URL = (
    "https://tk2-228-23746.vs.sakura.ne.jp/"
    "n01/tournament/n01_user_t.php"
    "?cmd=match_view&sid="
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/151.0 Safari/537.36"
)

REQUEST_TIMEOUT = 30
REQUEST_DELAY_SECONDS = 0.6

TOURNAMENT_PAGE_SIZE = 30
MAX_TOURNAMENT_PAGES = 500


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
        "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.8",
    }
)


# ============================================================
# PODSTAWOWE FUNKCJE POMOCNICZE
# ============================================================

def is_valid_identifier(value):
    """
    Sprawdza, czy identyfikator N01 zawiera wyłącznie:
    - litery,
    - cyfry,
    - podkreślenie,
    - myślnik.
    """

    if not isinstance(value, str):
        return False

    normalized = value.strip()

    if not normalized:
        return False

    if len(normalized) > 150:
        return False

    return bool(
        re.fullmatch(
            r"[A-Za-z0-9_-]+",
            normalized,
        )
    )


def is_valid_tmid(value):
    """
    Sprawdza poprawność techniczną tmid.
    """

    return is_valid_identifier(value)


def is_valid_tdid(value):
    """
    Sprawdza poprawność techniczną tdid.
    """

    return is_valid_identifier(value)


def get_query_parameter(source_url, parameter_name):
    """
    Pobiera parametr z adresu URL.

    Przykłady:
    - lgid z portal.php?lgid=...
    - id z comp.php?id=...
    """

    parsed_url = urlparse(source_url)
    query_params = parse_qs(parsed_url.query)

    values = query_params.get(parameter_name, [])

    if not values:
        raise ValueError(
            f"Nie znaleziono parametru "
            f"{parameter_name} w adresie: {source_url}"
        )

    return values[0]


def get_league_id(source_url):
    """
    Pobiera identyfikator ligi lgid.
    """

    return get_query_parameter(
        source_url,
        "lgid",
    )


def get_tournament_id(source_url):
    """
    Pobiera identyfikator wydarzenia/turnieju z parametru id.
    """

    return get_query_parameter(
        source_url,
        "id",
    )


def extract_ids_by_key(value, key_name):
    """
    Rekurencyjnie przegląda JSON i zbiera wartości pola
    o podanej nazwie.

    Przykłady:
    - tdid,
    - tmid.

    Obsługuje zarówno listy, jak i zagnieżdżone słowniki.
    """

    found_values = set()

    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() == key_name.lower():
                if item not in (None, ""):
                    found_values.add(
                        str(item).strip()
                    )

            found_values.update(
                extract_ids_by_key(
                    item,
                    key_name,
                )
            )

    elif isinstance(value, list):
        for item in value:
            found_values.update(
                extract_ids_by_key(
                    item,
                    key_name,
                )
            )

    return found_values


def extract_tmids_from_urls(value):
    """
    Dodatkowe zabezpieczenie.

    Jeżeli tmid nie występuje jako osobne pole JSON,
    funkcja próbuje znaleźć go w tekstach i adresach URL,
    np.:
    n01_view.html?tmid=abc_123
    """

    found_tmids = set()

    if isinstance(value, dict):
        for item in value.values():
            found_tmids.update(
                extract_tmids_from_urls(item)
            )

    elif isinstance(value, list):
        for item in value:
            found_tmids.update(
                extract_tmids_from_urls(item)
            )

    elif isinstance(value, str):
        matches = re.findall(
            r"[?&]tmid=([A-Za-z0-9_-]+)",
            value,
            flags=re.IGNORECASE,
        )

        for match in matches:
            if is_valid_tmid(match):
                found_tmids.add(match)

    return found_tmids


def first_existing(data, candidate_keys):
    """
    Zwraca pierwszą znalezioną niepustą wartość
    spośród podanych nazw pól.
    """

    if not isinstance(data, dict):
        return None

    for key in candidate_keys:
        value = data.get(key)

        if value not in (None, ""):
            return value

    return None


def find_first_value_recursive(value, candidate_keys):
    """
    Rekurencyjnie szuka pierwszej wartości dla jednej
    z podanych nazw pól.

    Jest używana, ponieważ JSON meczu N01 może mieć dane
    zagnieżdżone w dodatkowych obiektach.
    """

    normalized_keys = {
        key.lower()
        for key in candidate_keys
    }

    if isinstance(value, dict):
        for key, item in value.items():
            if (
                str(key).lower() in normalized_keys
                and item not in (None, "")
            ):
                return item

        for item in value.values():
            result = find_first_value_recursive(
                item,
                candidate_keys,
            )

            if result not in (None, ""):
                return result

    elif isinstance(value, list):
        for item in value:
            result = find_first_value_recursive(
                item,
                candidate_keys,
            )

            if result not in (None, ""):
                return result

    return None


def parse_match_date(data):
    """
    Próbuje odczytać datę meczu z kilku możliwych pól.
    """

    raw_value = find_first_value_recursive(
        data,
        [
            "match_date",
            "date",
            "start_date",
            "started_at",
            "datetime",
            "matchDate",
            "startDate",
        ],
    )

    if raw_value in (None, ""):
        return None

    if isinstance(raw_value, datetime):
        return raw_value

    if isinstance(raw_value, (int, float)):
        try:
            return datetime.fromtimestamp(
                raw_value
            )
        except (ValueError, OSError, OverflowError):
            return None

    text_value = str(raw_value).strip()

    try:
        return datetime.fromisoformat(
            text_value.replace("Z", "+00:00")
        )
    except ValueError:
        return None


def json_signature(value):
    """
    Tworzy stabilny podpis JSON używany do wykrywania,
    czy endpoint zwrócił drugi raz dokładnie tę samą stronę.
    """

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


# ============================================================
# ROZPOZNAWANIE REKORDÓW W ODPOWIEDZI HISTORII
# ============================================================

def get_records_list(value):
    """
    Próbuje odnaleźć właściwą listę rekordów w odpowiedzi
    endpointu historii.

    Obsługiwane przykłady:
    - [{...}, {...}]
    - {"data": [...]}
    - {"list": [...]}
    - {"items": [...]}
    - {"rows": [...]}
    - {"matches": [...]}
    - lista zagnieżdżona głębiej w JSON.
    """

    if isinstance(value, list):
        return value

    if not isinstance(value, dict):
        return None

    preferred_keys = [
        "data",
        "list",
        "items",
        "result",
        "results",
        "rows",
        "matches",
        "history",
        "t_list",
    ]

    for key in preferred_keys:
        candidate = value.get(key)

        if isinstance(candidate, list):
            return candidate

    for candidate in value.values():
        if isinstance(candidate, dict):
            nested_result = get_records_list(
                candidate
            )

            if nested_result is not None:
                return nested_result

    return None


def count_json_records(value):
    """
    Zwraca liczbę rekordów na stronie historii.

    Jeżeli nie uda się odnaleźć listy rekordów,
    zwraca None.
    """

    records = get_records_list(value)

    if records is None:
        return None

    return len(records)


def response_is_empty(value):
    """
    Sprawdza, czy odpowiedź endpointu jest faktycznie pusta.
    """

    if value is None:
        return True

    if value == "":
        return True

    if value == []:
        return True

    if value == {}:
        return True

    records = get_records_list(value)

    if records == []:
        return True

    return False


# ============================================================
# POBIERANIE LISTY WYDARZEŃ LIGI
# ============================================================

def download_league_events(source_url):
    """
    Pobiera wszystkie wydarzenia przypisane do ligi.

    Wynikiem są identyfikatory tdid.
    """

    league_id = get_league_id(source_url)

    endpoint_url = (
        f"{LEAGUE_API_URL}"
        f"?cmd=get_season_list"
        f"&lgid={league_id}"
    )

    payload = {
        "skip": 0,
        "count": 500,
        "keyword": "",
        "status": [10, 20, 25, 30, 40],
        "sort": "date",
        "sort_order": -1,
    }

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Content-Type": (
            "application/x-www-form-urlencoded; charset=UTF-8"
        ),
        "Origin": "https://n01darts.com",
        "Referer": source_url,
    }

    logging.info(
        "Pobieranie listy wydarzeń ligi: %s",
        league_id,
    )

    response = session.post(
        endpoint_url,
        data=json.dumps(payload),
        headers=headers,
        timeout=REQUEST_TIMEOUT,
    )

    logging.info(
        "Endpoint wydarzeń ligi zwrócił HTTP %s",
        response.status_code,
    )

    response.raise_for_status()

    try:
        data = response.json()

    except requests.JSONDecodeError as exc:
        logging.error(
            "Odpowiedź endpointu ligi nie jest JSON-em. "
            "Początek odpowiedzi:\n%s",
            response.text[:3000],
        )

        raise ValueError(
            "Endpoint listy wydarzeń ligi "
            "nie zwrócił poprawnego JSON."
        ) from exc

    logging.info(
        "Typ odpowiedzi listy wydarzeń: %s",
        type(data).__name__,
    )

    logging.info(
        "LEAGUE EVENTS JSON:\n%s",
        json.dumps(
            data,
            ensure_ascii=False,
            indent=2,
        )[:30000],
    )

    tournament_ids = extract_ids_by_key(
        data,
        "tdid",
    )

    tournament_ids = {
        tdid
        for tdid in tournament_ids
        if is_valid_tdid(tdid)
    }

    direct_tmids = extract_ids_by_key(
        data,
        "tmid",
    )

    direct_tmids.update(
        extract_tmids_from_urls(data)
    )

    direct_tmids = {
        tmid
        for tmid in direct_tmids
        if is_valid_tmid(tmid)
    }

    logging.info(
        "Liczba znalezionych tdid: %s",
        len(tournament_ids),
    )

    if tournament_ids:
        logging.info(
            "ZNALEZIONE TDID:"
        )

        for tournament_id in sorted(
            tournament_ids
        ):
            logging.info(
                "TDID: %s",
                tournament_id,
            )
    else:
        logging.warning(
            "W odpowiedzi endpointu ligi "
            "nie znaleziono żadnych tdid."
        )

    logging.info(
        "Liczba tmid znalezionych bezpośrednio "
        "w odpowiedzi ligi: %s",
        len(direct_tmids),
    )

    return {
        "data": data,
        "tdids": tournament_ids,
        "tmids": direct_tmids,
    }


# ============================================================
# POBIERANIE TMID DLA JEDNEGO WYDARZENIA
# ============================================================

def download_tournament_tmids(tdid):
    """
    Pobiera wszystkie mecze wydarzenia za pomocą stronicowania:

    skip=0
    skip=30
    skip=60
    skip=90
    itd.

    Pętla kończy się, gdy:
    - endpoint zwróci pustą odpowiedź,
    - lista rekordów będzie pusta,
    - zwróconych zostanie mniej niż 30 rekordów,
    - kolejna odpowiedź będzie identyczna,
    - kolejna strona nie będzie zawierać nowych danych,
    - zostanie osiągnięty limit bezpieczeństwa.
    """

    if not is_valid_tdid(tdid):
        raise ValueError(
            f"Nieprawidłowy format tdid: {tdid}"
        )

    page_size = TOURNAMENT_PAGE_SIZE
    skip = 0

    all_tmids = set()
    previous_page_signature = None

    logging.info(
        "Rozpoczęcie pobierania meczów "
        "dla wydarzenia TDID: %s",
        tdid,
    )

    for page_number in range(
        1,
        MAX_TOURNAMENT_PAGES + 1,
    ):
        endpoint_url = (
            f"{TOURNAMENT_HISTORY_API_URL}"
            f"?cmd=get_t_list"
            f"&tdid={tdid}"
            f"&skip={skip}"
            f"&count={page_size}"
            f"&name="
        )

        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
            "Origin": "https://n01darts.com",
            "Referer": (
                "https://n01darts.com/n01/tournament/"
                f"n01_v2/history.html?tdid={tdid}"
            ),
        }

        logging.info(
            "TDID %s: pobieranie strony %s, skip=%s",
            tdid,
            page_number,
            skip,
        )

        response = session.post(
            endpoint_url,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )

        logging.info(
            "TDID %s, skip=%s: HTTP %s",
            tdid,
            skip,
            response.status_code,
        )

        response.raise_for_status()

        try:
            page_data = response.json()

        except requests.JSONDecodeError as exc:
            logging.error(
                "TDID %s, skip=%s: odpowiedź "
                "nie jest JSON-em.\n%s",
                tdid,
                skip,
                response.text[:3000],
            )

            raise ValueError(
                f"Historia wydarzenia {tdid} "
                f"dla skip={skip} nie zwróciła JSON."
            ) from exc

        if page_number == 1:
            logging.info(
                "PRZYKŁADOWY JSON HISTORII TDID %s:\n%s",
                tdid,
                json.dumps(
                    page_data,
                    ensure_ascii=False,
                    indent=2,
                )[:15000],
            )

        if response_is_empty(page_data):
            logging.info(
                "TDID %s, skip=%s: pusta odpowiedź. "
                "Koniec stronicowania.",
                tdid,
                skip,
            )
            break

        current_signature = json_signature(
            page_data
        )

        if (
            previous_page_signature is not None
            and current_signature == previous_page_signature
        ):
            logging.warning(
                "TDID %s, skip=%s: endpoint zwrócił "
                "ponownie tę samą stronę. "
                "Koniec stronicowania.",
                tdid,
                skip,
            )
            break

        previous_page_signature = current_signature

        page_tmids = extract_ids_by_key(
            page_data,
            "tmid",
        )

        page_tmids.update(
            extract_tmids_from_urls(page_data)
        )

        page_tmids = {
            tmid
            for tmid in page_tmids
            if is_valid_tmid(tmid)
        }

        new_tmids = (
            page_tmids
            - all_tmids
        )

        record_count = count_json_records(
            page_data
        )

        logging.info(
            "TDID %s, skip=%s: rekordów=%s, "
            "tmid na stronie=%s, nowych tmid=%s",
            tdid,
            skip,
            (
                record_count
                if record_count is not None
                else "nieustalona liczba"
            ),
            len(page_tmids),
            len(new_tmids),
        )

        if page_number == 1 and not page_tmids:
            logging.warning(
                "TDID %s: pierwsza odpowiedź zawiera dane, "
                "ale nie znaleziono pola ani adresu z tmid. "
                "Sprawdź sekcję PRZYKŁADOWY JSON HISTORII.",
                tdid,
            )

        all_tmids.update(
            page_tmids
        )

        if record_count == 0:
            logging.info(
                "TDID %s: lista rekordów jest pusta. "
                "Koniec stronicowania.",
                tdid,
            )
            break

        if (
            record_count is not None
            and record_count < page_size
        ):
            logging.info(
                "TDID %s: ostatnia strona zawiera "
                "%s rekordów, czyli mniej niż %s. "
                "Koniec stronicowania.",
                tdid,
                record_count,
                page_size,
            )
            break

        if (
            record_count is None
            and not page_tmids
        ):
            logging.warning(
                "TDID %s, skip=%s: nie udało się ustalić "
                "liczby rekordów ani znaleźć tmid. "
                "Koniec stronicowania.",
                tdid,
                skip,
            )
            break

        if (
            page_number > 1
            and not new_tmids
        ):
            logging.warning(
                "TDID %s, skip=%s: strona nie zawiera "
                "nowych tmid. Koniec stronicowania.",
                tdid,
                skip,
            )
            break

        skip += page_size

        time.sleep(
            REQUEST_DELAY_SECONDS
        )

    else:
        logging.warning(
            "TDID %s: osiągnięto limit bezpieczeństwa "
            "%s stron.",
            tdid,
            MAX_TOURNAMENT_PAGES,
        )

    logging.info(
        "TDID %s: łącznie znaleziono "
        "%s unikalnych tmid.",
        tdid,
        len(all_tmids),
    )

    return all_tmids


# ============================================================
# POBIERANIE WSZYSTKICH TMID DLA LIGI
# ============================================================

def download_all_league_tmids(source_url):
    """
    Realizuje pełny proces:

    1. pobiera lgid z adresu ligi,
    2. pobiera listę wydarzeń tdid,
    3. pobiera wszystkie strony każdego wydarzenia,
    4. zbiera unikalne tmid.
    """

    league_result = download_league_events(
        source_url
    )

    tournament_ids = league_result["tdids"]

    all_tmids = set(
        league_result["tmids"]
    )

    logging.info(
        "Liga %s: pobieranie meczów dla "
        "%s wydarzeń.",
        source_url,
        len(tournament_ids),
    )

    for tournament_number, tdid in enumerate(
        sorted(tournament_ids),
        start=1,
    ):
        logging.info(
            "Przetwarzanie wydarzenia "
            "%s z %s: %s",
            tournament_number,
            len(tournament_ids),
            tdid,
        )

        try:
            tournament_tmids = (
                download_tournament_tmids(
                    tdid
                )
            )

            all_tmids.update(
                tournament_tmids
            )

        except Exception as exc:
            logging.exception(
                "Nie udało się pobrać meczów "
                "dla TDID %s: %s",
                tdid,
                exc,
            )

        time.sleep(
            REQUEST_DELAY_SECONDS
        )

    logging.info(
        "Liga %s: łącznie znaleziono "
        "%s unikalnych tmid.",
        source_url,
        len(all_tmids),
    )

    return all_tmids


# ============================================================
# ROZPOZNAWANIE RODZAJU ŹRÓDŁA
# ============================================================

def discover_tmids(source_url):
    """
    Rozpoznaje rodzaj źródła.

    Obsługiwane źródła:

    1. liga:
       /n01/league/portal.php?lgid=...

    2. konkretny turniej:
       /n01/tournament/comp.php?id=...
    """

    if "/n01/league/portal.php" in source_url:
        return download_all_league_tmids(
            source_url
        )

    if "/n01/tournament/comp.php" in source_url:
        tdid = get_tournament_id(
            source_url
        )

        return download_tournament_tmids(
            tdid
        )

    raise ValueError(
        "Nieobsługiwany rodzaj źródła: "
        f"{source_url}"
    )


# ============================================================
# POBIERANIE JSON KONKRETNEGO MECZU
# ============================================================

def download_match(tmid):
    """
    Pobiera szczegółowy JSON konkretnego meczu.
    """

    if not is_valid_tmid(tmid):
        raise ValueError(
            f"Nieprawidłowy format tmid: {tmid}"
        )

    payload = {
        "tmid": tmid,
    }

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Content-Type": (
            "application/json; charset=UTF-8"
        ),
        "Origin": "https://n01darts.com",
        "Referer": (
            "https://n01darts.com/n01/league/"
            f"n01_view.html?tmid={tmid}"
        ),
    }

    response = session.post(
        MATCH_API_URL,
        json=payload,
        headers=headers,
        timeout=REQUEST_TIMEOUT,
    )

    logging.info(
        "Endpoint meczu %s zwrócił HTTP %s",
        tmid,
        response.status_code,
    )

    response.raise_for_status()

    try:
        data = response.json()

    except requests.JSONDecodeError as exc:
        logging.error(
            "Odpowiedź endpointu meczu %s "
            "nie jest JSON-em:\n%s",
            tmid,
            response.text[:2000],
        )

        raise ValueError(
            f"Odpowiedź dla meczu {tmid} "
            "nie jest JSON-em."
        ) from exc

    if not isinstance(data, dict):
        raise ValueError(
            f"Nieoczekiwany typ odpowiedzi "
            f"dla {tmid}: {type(data).__name__}"
        )

    if not data:
        raise ValueError(
            f"Endpoint zwrócił pusty JSON "
            f"dla tmid: {tmid}"
        )

    return data


# ============================================================
# ODCZYT ŹRÓDEŁ Z SUPABASE
# ============================================================

def get_active_sources(conn):
    """
    Pobiera aktywne adresy z tabeli source_urls.
    """

    with conn.cursor() as cur:
        cur.execute(
            """
            select
                id,
                source_url,
                source_type
            from source_urls
            where is_active = true
            order by id;
            """
        )

        return cur.fetchall()


# ============================================================
# ZAPIS MECZU DO SUPABASE
# ============================================================

def save_match(
    conn,
    source_url,
    requested_tmid,
    data,
):
    """
    Zapisuje nowy mecz albo aktualizuje istniejący.

    Pełna odpowiedź N01 jest zapisywana
    w kolumnie match_data typu jsonb.
    """

    actual_tmid = str(
        find_first_value_recursive(
            data,
            ["tmid"],
        )
        or requested_tmid
    ).strip()

    if not is_valid_tmid(actual_tmid):
        raise ValueError(
            "Nieprawidłowy tmid zwrócony "
            f"przez endpoint: {actual_tmid}"
        )

    player_1 = find_first_value_recursive(
        data,
        [
            "player1",
            "player_1",
            "p1_name",
            "name1",
            "playerName1",
            "player_name_1",
        ],
    )

    player_2 = find_first_value_recursive(
        data,
        [
            "player2",
            "player_2",
            "p2_name",
            "name2",
            "playerName2",
            "player_name_2",
        ],
    )

    match_status = find_first_value_recursive(
        data,
        [
            "status",
            "match_status",
            "state",
        ],
    )

    match_date = parse_match_date(
        data
    )

    match_url = (
        "https://n01darts.com/n01/league/"
        f"n01_view.html?tmid={actual_tmid}"
    )

    with conn.cursor() as cur:
        cur.execute(
            """
            insert into matches
            (
                tmid,
                source_url,
                match_url,
                match_data,
                player_1,
                player_2,
                match_status,
                match_date,
                first_seen_at,
                last_seen_at,
                updated_at
            )
            values
            (
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                now(),
                now(),
                now()
            )
            on conflict (tmid)
            do update set
                source_url = excluded.source_url,
                match_url = excluded.match_url,
                match_data = excluded.match_data,
                player_1 = excluded.player_1,
                player_2 = excluded.player_2,
                match_status = excluded.match_status,
                match_date = excluded.match_date,
                last_seen_at = now(),
                updated_at =
                    case
                        when matches.match_data
                             is distinct from excluded.match_data
                        then now()
                        else matches.updated_at
                    end;
            """,
            (
                actual_tmid,
                source_url,
                match_url,
                Jsonb(data),
                (
                    str(player_1)
                    if player_1 is not None
                    else None
                ),
                (
                    str(player_2)
                    if player_2 is not None
                    else None
                ),
                (
                    str(match_status)
                    if match_status is not None
                    else None
                ),
                match_date,
            ),
        )


# ============================================================
# STATUS ŹRÓDŁA
# ============================================================

def update_source_status(
    conn,
    source_id,
    error_message=None,
):
    """
    Aktualizuje datę ostatniego sprawdzenia źródła
    oraz ewentualny komunikat błędu.
    """

    with conn.cursor() as cur:
        cur.execute(
            """
            update source_urls
            set
                last_checked_at = now(),
                last_error = %s
            where id = %s;
            """,
            (
                error_message,
                source_id,
            ),
        )


# ============================================================
# LOG SYNCHRONIZACJI
# ============================================================

def create_sync_log(conn):
    """
    Tworzy wpis rozpoczęcia synchronizacji.
    """

    with conn.cursor() as cur:
        cur.execute(
            """
            insert into sync_log
            (
                status
            )
            values
            (
                'running'
            )
            returning id;
            """
        )

        return cur.fetchone()[0]


def finish_sync_log(
    conn,
    log_id,
    sources_processed,
    tmids_found,
    matches_downloaded,
    errors_count,
    status,
    message,
):
    """
    Aktualizuje wpis po zakończeniu synchronizacji.
    """

    with conn.cursor() as cur:
        cur.execute(
            """
            update sync_log
            set
                finished_at = now(),
                sources_processed = %s,
                tmids_found = %s,
                matches_downloaded = %s,
                errors_count = %s,
                status = %s,
                message = %s
            where id = %s;
            """,
            (
                sources_processed,
                tmids_found,
                matches_downloaded,
                errors_count,
                status,
                message,
                log_id,
            ),
        )


# ============================================================
# GŁÓWNA SYNCHRONIZACJA
# ============================================================

def main():
    sources_processed = 0
    all_tmids_found = set()
    matches_downloaded = 0
    errors_count = 0

    logging.info(
        "Rozpoczęcie synchronizacji N01."
    )

    with psycopg.connect(
        DATABASE_URL,
        autocommit=False,
    ) as conn:

        log_id = create_sync_log(
            conn
        )

        conn.commit()

        try:
            sources = get_active_sources(
                conn
            )

            logging.info(
                "Liczba aktywnych źródeł: %s",
                len(sources),
            )

            if not sources:
                logging.warning(
                    "Brak aktywnych adresów "
                    "w tabeli source_urls."
                )

            for (
                source_id,
                source_url,
                source_type,
            ) in sources:

                sources_processed += 1

                logging.info(
                    "Przetwarzanie źródła %s: %s",
                    source_type,
                    source_url,
                )

                try:
                    source_tmids = discover_tmids(
                        source_url
                    )

                    source_tmids = {
                        tmid
                        for tmid in source_tmids
                        if is_valid_tmid(tmid)
                    }

                    all_tmids_found.update(
                        source_tmids
                    )

                    logging.info(
                        "Źródło %s zwróciło %s "
                        "unikalnych tmid.",
                        source_url,
                        len(source_tmids),
                    )

                    for match_number, tmid in enumerate(
                        sorted(source_tmids),
                        start=1,
                    ):
                        try:
                            logging.info(
                                "Pobieranie meczu "
                                "%s z %s: %s",
                                match_number,
                                len(source_tmids),
                                tmid,
                            )

                            match_data = download_match(
                                tmid
                            )

                            save_match(
                                conn,
                                source_url,
                                tmid,
                                match_data,
                            )

                            conn.commit()

                            matches_downloaded += 1

                            logging.info(
                                "Zapisano mecz: %s",
                                tmid,
                            )

                        except Exception as exc:
                            conn.rollback()
                            errors_count += 1

                            logging.exception(
                                "Błąd pobierania lub "
                                "zapisywania meczu %s: %s",
                                tmid,
                                exc,
                            )

                        time.sleep(
                            REQUEST_DELAY_SECONDS
                        )

                    update_source_status(
                        conn,
                        source_id,
                        error_message=None,
                    )

                    conn.commit()

                except Exception as exc:
                    conn.rollback()
                    errors_count += 1

                    logging.exception(
                        "Błąd źródła %s: %s",
                        source_url,
                        exc,
                    )

                    update_source_status(
                        conn,
                        source_id,
                        error_message=str(exc)[:2000],
                    )

                    conn.commit()

            final_status = (
                "success"
                if errors_count == 0
                else "completed_with_errors"
            )

            final_message = (
                f"Źródła: {sources_processed}; "
                f"tmid: {len(all_tmids_found)}; "
                f"mecze zapisane lub zaktualizowane: "
                f"{matches_downloaded}; "
                f"błędy: {errors_count}"
            )

            finish_sync_log(
                conn,
                log_id,
                sources_processed,
                len(all_tmids_found),
                matches_downloaded,
                errors_count,
                final_status,
                final_message,
            )

            conn.commit()

            logging.info(
                final_message
            )

        except Exception as exc:
            conn.rollback()

            logging.exception(
                "Krytyczny błąd synchronizacji: %s",
                exc,
            )

            finish_sync_log(
                conn,
                log_id,
                sources_processed,
                len(all_tmids_found),
                matches_downloaded,
                errors_count + 1,
                "failed",
                str(exc)[:2000],
            )

            conn.commit()

            raise


if __name__ == "__main__":
    main()
