import json
import logging
import os
import re
import time
from datetime import datetime, timezone
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
REQUEST_DELAY_SECONDS = 0.25

TOURNAMENT_PAGE_SIZE = 30
MAX_TOURNAMENT_PAGES = 500

# Pierwsze uruchomienie po zmianie może przejrzeć całą historię.
# Szczegółowe dane zostaną jednak pobrane tylko dla brakujących tmid.
STOP_AFTER_EXISTING_PAGES = 0


# ============================================================
# LOGOWANIE
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

session = requests.Session()

session.headers.update(
    {
        "User-Agent": USER_AGENT,
        "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.8",
    }
)


# ============================================================
# IDENTYFIKATORY I URL
# ============================================================

def is_valid_identifier(value):
    if not isinstance(value, str):
        return False

    value = value.strip()

    if not value or len(value) > 150:
        return False

    return bool(
        re.fullmatch(
            r"[A-Za-z0-9_-]+",
            value,
        )
    )


def get_query_parameter(source_url, parameter_name):
    parsed_url = urlparse(source_url)
    parameters = parse_qs(parsed_url.query)
    values = parameters.get(parameter_name, [])

    if not values:
        raise ValueError(
            f"Brak parametru {parameter_name} "
            f"w adresie {source_url}"
        )

    return values[0]


def get_league_id(source_url):
    return get_query_parameter(
        source_url,
        "lgid",
    )


def get_tournament_id(source_url):
    return get_query_parameter(
        source_url,
        "id",
    )


# ============================================================
# PRZESZUKIWANIE JSON
# ============================================================

def extract_ids_by_key(value, key_name):
    found = set()

    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() == key_name.lower():
                if item not in (None, ""):
                    identifier = str(item).strip()

                    if is_valid_identifier(identifier):
                        found.add(identifier)

            found.update(
                extract_ids_by_key(
                    item,
                    key_name,
                )
            )

    elif isinstance(value, list):
        for item in value:
            found.update(
                extract_ids_by_key(
                    item,
                    key_name,
                )
            )

    return found


def extract_tmids_from_urls(value):
    found = set()

    if isinstance(value, dict):
        for item in value.values():
            found.update(
                extract_tmids_from_urls(item)
            )

    elif isinstance(value, list):
        for item in value:
            found.update(
                extract_tmids_from_urls(item)
            )

    elif isinstance(value, str):
        matches = re.findall(
            r"[?&]tmid=([A-Za-z0-9_-]+)",
            value,
            flags=re.IGNORECASE,
        )

        for match in matches:
            if is_valid_identifier(match):
                found.add(match)

    return found


def find_first_value_recursive(value, candidate_keys):
    normalized_keys = {
        str(key).lower()
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


def get_records_list(value):
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
            nested = get_records_list(candidate)

            if nested is not None:
                return nested

    return None


def response_is_empty(value):
    if value in (None, "", [], {}):
        return True

    records = get_records_list(value)

    return records == []


def json_signature(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


# ============================================================
# DANE JUŻ ZAPISANE W SUPABASE
# ============================================================

def get_existing_tmids(conn):
    """
    Pobiera jednorazowo wszystkie tmid już zapisane w matches.

    Dzięki temu nie wykonujemy osobnego zapytania SQL
    dla każdego meczu.
    """

    with conn.cursor() as cur:
        cur.execute(
            """
            select tmid
            from public.matches;
            """
        )

        return {
            str(row[0]).strip()
            for row in cur.fetchall()
            if row[0] not in (None, "")
        }


# ============================================================
# ŹRÓDŁA
# ============================================================

def get_active_sources(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            select
                id,
                source_url,
                source_type
            from public.source_urls
            where is_active = true
            order by id;
            """
        )

        return cur.fetchall()


def update_source_status(
    conn,
    source_id,
    error_message=None,
):
    with conn.cursor() as cur:
        cur.execute(
            """
            update public.source_urls
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
# LISTA WYDARZEŃ LIGI
# ============================================================

def download_league_events(source_url):
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
        "Pobieranie wydarzeń ligi %s",
        league_id,
    )

    response = session.post(
        endpoint_url,
        data=json.dumps(payload),
        headers=headers,
        timeout=REQUEST_TIMEOUT,
    )

    response.raise_for_status()

    try:
        data = response.json()

    except requests.JSONDecodeError as exc:
        raise ValueError(
            "Endpoint wydarzeń ligi nie zwrócił JSON."
        ) from exc

    tournament_ids = {
        tdid
        for tdid in extract_ids_by_key(data, "tdid")
        if is_valid_identifier(tdid)
    }

    logging.info(
        "Liga %s: znaleziono %s wydarzeń TDID.",
        league_id,
        len(tournament_ids),
    )

    return tournament_ids


# ============================================================
# HISTORIA ZAKOŃCZONYCH MECZÓW
# ============================================================

def download_tournament_tmids(
    tdid,
    existing_tmids,
):
    """
    Pobiera identyfikatory zakończonych meczów z historii
    konkretnego wydarzenia.

    Endpoint jest stronicowany:
    skip=0, 30, 60, 90 itd.
    """

    all_history_tmids = set()
    new_tmids = set()

    previous_signature = None
    skip = 0

    consecutive_existing_pages = 0

    for page_number in range(
        1,
        MAX_TOURNAMENT_PAGES + 1,
    ):
        endpoint_url = (
            f"{TOURNAMENT_HISTORY_API_URL}"
            f"?cmd=get_t_list"
            f"&tdid={tdid}"
            f"&skip={skip}"
            f"&count={TOURNAMENT_PAGE_SIZE}"
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

        response = session.post(
            endpoint_url,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )

        response.raise_for_status()

        try:
            page_data = response.json()

        except requests.JSONDecodeError as exc:
            raise ValueError(
                f"Historia {tdid}, skip={skip} "
                "nie zwróciła JSON."
            ) from exc

        if response_is_empty(page_data):
            logging.info(
                "TDID %s: koniec danych przy skip=%s.",
                tdid,
                skip,
            )
            break

        current_signature = json_signature(page_data)

        if current_signature == previous_signature:
            logging.warning(
                "TDID %s: endpoint powtórzył stronę "
                "przy skip=%s.",
                tdid,
                skip,
            )
            break

        previous_signature = current_signature

        records = get_records_list(page_data)

        if records is None:
            records = []

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
            if is_valid_identifier(tmid)
        }

        page_new_tmids = (
            page_tmids
            - existing_tmids
            - all_history_tmids
        )

        all_history_tmids.update(page_tmids)
        new_tmids.update(page_new_tmids)

        logging.info(
            "TDID %s, skip=%s: rekordów=%s, "
            "tmid=%s, nowych=%s, istniejących=%s",
            tdid,
            skip,
            len(records),
            len(page_tmids),
            len(page_new_tmids),
            len(page_tmids & existing_tmids),
        )

        if page_tmids and not page_new_tmids:
            consecutive_existing_pages += 1
        else:
            consecutive_existing_pages = 0

        if (
            STOP_AFTER_EXISTING_PAGES > 0
            and consecutive_existing_pages
            >= STOP_AFTER_EXISTING_PAGES
        ):
            logging.info(
                "TDID %s: zatrzymanie po %s stronach "
                "zawierających wyłącznie istniejące mecze.",
                tdid,
                consecutive_existing_pages,
            )
            break

        if len(records) < TOURNAMENT_PAGE_SIZE:
            break

        if not records and not page_tmids:
            break

        skip += TOURNAMENT_PAGE_SIZE

        time.sleep(REQUEST_DELAY_SECONDS)

    logging.info(
        "TDID %s: historia zawiera %s tmid, "
        "do pobrania pozostało %s.",
        tdid,
        len(all_history_tmids),
        len(new_tmids),
    )

    return new_tmids


def discover_new_completed_tmids(
    source_url,
    existing_tmids,
):
    """
    Zwraca wyłącznie zakończone tmid,
    których nie ma jeszcze w matches.
    """

    all_new_tmids = set()

    if "/n01/league/portal.php" in source_url:
        tournament_ids = download_league_events(
            source_url
        )

    elif "/n01/tournament/comp.php" in source_url:
        tournament_ids = {
            get_tournament_id(source_url)
        }

    else:
        raise ValueError(
            f"Nieobsługiwane źródło: {source_url}"
        )

    for number, tdid in enumerate(
        sorted(tournament_ids),
        start=1,
    ):
        logging.info(
            "Wydarzenie %s z %s: %s",
            number,
            len(tournament_ids),
            tdid,
        )

        event_new_tmids = download_tournament_tmids(
            tdid,
            existing_tmids | all_new_tmids,
        )

        all_new_tmids.update(
            event_new_tmids
        )

        time.sleep(REQUEST_DELAY_SECONDS)

    return all_new_tmids


# ============================================================
# SZCZEGÓŁY MECZU
# ============================================================

def download_match(tmid):
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

    response.raise_for_status()

    try:
        data = response.json()

    except requests.JSONDecodeError as exc:
        raise ValueError(
            f"Mecz {tmid} nie zwrócił JSON."
        ) from exc

    if not isinstance(data, dict) or not data:
        raise ValueError(
            f"Mecz {tmid} zwrócił pustą "
            "lub nieprawidłową odpowiedź."
        )

    return data


def parse_match_date(data):
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
                raw_value,
                tz=timezone.utc,
            )
        except (ValueError, OSError, OverflowError):
            return None

    try:
        return datetime.fromisoformat(
            str(raw_value).replace(
                "Z",
                "+00:00",
            )
        )
    except ValueError:
        return None


def save_match(
    conn,
    source_url,
    requested_tmid,
    data,
):
    actual_tmid = str(
        find_first_value_recursive(
            data,
            ["tmid"],
        )
        or requested_tmid
    ).strip()

    if not is_valid_identifier(actual_tmid):
        raise ValueError(
            f"Nieprawidłowy tmid: {actual_tmid}"
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

    match_date = parse_match_date(data)

    match_url = (
        "https://n01darts.com/n01/league/"
        f"n01_view.html?tmid={actual_tmid}"
    )

    with conn.cursor() as cur:
        cur.execute(
            """
            insert into public.matches
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
            do nothing;
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

        return cur.rowcount == 1


# ============================================================
# LOG SYNCHRONIZACJI
# ============================================================

def create_sync_log(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into public.sync_log(status)
            values ('running')
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
    with conn.cursor() as cur:
        cur.execute(
            """
            update public.sync_log
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
# GŁÓWNY PROCES
# ============================================================

def main():
    sources_processed = 0
    new_tmids_found = set()
    matches_inserted = 0
    errors_count = 0

    with psycopg.connect(
        DATABASE_URL,
        autocommit=False,
    ) as conn:

        log_id = create_sync_log(conn)
        conn.commit()

        try:
            existing_tmids = get_existing_tmids(conn)
            sources = get_active_sources(conn)

            logging.info(
                "W Supabase istnieje już %s meczów.",
                len(existing_tmids),
            )

            logging.info(
                "Liczba aktywnych źródeł: %s.",
                len(sources),
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
                    source_new_tmids = (
                        discover_new_completed_tmids(
                            source_url,
                            existing_tmids
                            | new_tmids_found,
                        )
                    )

                    new_tmids_found.update(
                        source_new_tmids
                    )

                    logging.info(
                        "Źródło zawiera %s nowych "
                        "zakończonych meczów.",
                        len(source_new_tmids),
                    )

                    for number, tmid in enumerate(
                        sorted(source_new_tmids),
                        start=1,
                    ):
                        try:
                            logging.info(
                                "Pobieranie nowego meczu "
                                "%s z %s: %s",
                                number,
                                len(source_new_tmids),
                                tmid,
                            )

                            match_data = download_match(tmid)

                            inserted = save_match(
                                conn,
                                source_url,
                                tmid,
                                match_data,
                            )

                            conn.commit()

                            if inserted:
                                matches_inserted += 1
                                existing_tmids.add(tmid)

                                logging.info(
                                    "Dodano nowy mecz: %s",
                                    tmid,
                                )
                            else:
                                logging.info(
                                    "Mecz %s już istniał. "
                                    "Pominięto.",
                                    tmid,
                                )

                        except Exception as exc:
                            conn.rollback()
                            errors_count += 1

                            logging.exception(
                                "Błąd meczu %s: %s",
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

            status = (
                "success"
                if errors_count == 0
                else "completed_with_errors"
            )

            message = (
                f"Źródła: {sources_processed}; "
                f"nowe zakończone tmid: "
                f"{len(new_tmids_found)}; "
                f"dodane mecze: {matches_inserted}; "
                f"błędy: {errors_count}"
            )

            finish_sync_log(
                conn,
                log_id,
                sources_processed,
                len(new_tmids_found),
                matches_inserted,
                errors_count,
                status,
                message,
            )

            conn.commit()

            logging.info(message)

        except Exception as exc:
            conn.rollback()

            finish_sync_log(
                conn,
                log_id,
                sources_processed,
                len(new_tmids_found),
                matches_inserted,
                errors_count + 1,
                "failed",
                str(exc)[:2000],
            )

            conn.commit()

            raise


if __name__ == "__main__":
    main()
