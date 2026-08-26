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


# ============================================================
# FUNKCJE POMOCNICZE
# ============================================================

def is_valid_tmid(tmid):
    """
    Sprawdza, czy wartość wygląda jak identyfikator N01.

    Dopuszczamy tylko:
    - litery,
    - cyfry,
    - znak podkreślenia,
    - myślnik.
    """

    if not isinstance(tmid, str):
        return False

    normalized = tmid.strip()

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


def first_existing(data, candidate_keys):
    """
    Zwraca pierwszą niepustą wartość z podanej listy kluczy.
    """

    if not isinstance(data, dict):
        return None

    for key in candidate_keys:
        value = data.get(key)

        if value not in (None, ""):
            return value

    return None


def parse_match_date(data):
    """
    Próbuje pobrać datę meczu z kilku możliwych pól JSON.
    """

    raw_value = first_existing(
        data,
        [
            "match_date",
            "date",
            "start_date",
            "started_at",
            "datetime",
        ],
    )

    if not raw_value:
        return None

    if isinstance(raw_value, datetime):
        return raw_value

    text_value = str(raw_value).strip()

    try:
        return datetime.fromisoformat(
            text_value.replace("Z", "+00:00")
        )
    except ValueError:
        return None


def fetch_text(url):
    """
    Pobiera stronę HTML lub plik JavaScript jako tekst.
    """

    response = session.get(
        url,
        timeout=REQUEST_TIMEOUT,
        allow_redirects=True,
    )

    response.raise_for_status()

    return response.text, response.url


def extract_tmids(text):
    """
    Wyciąga tmid wyłącznie z jawnych parametrów adresów:
    ?tmid=...
    &tmid=...
    """

    if not text:
        return set()

    decoded_text = html.unescape(str(text))
    tmids = set()

    for match in TMID_PATTERN.findall(decoded_text):
        normalized = match.strip()

        if is_valid_tmid(normalized):
            tmids.add(normalized)

    return tmids


def extract_ids_by_key(value, key_name):
    """
    Rekurencyjnie przechodzi po JSON-ie i odnajduje wartości
    przypisane do wskazanego klucza, np. tdid albo tmid.

    Funkcja nie zakłada, czy odpowiedź jest listą, słownikiem,
    czy ma dane zagnieżdżone w kilku poziomach.
    """

    found_values = set()

    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() == key_name.lower():
                if item not in (None, ""):
                    found_values.add(str(item).strip())

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


# ============================================================
# OBSŁUGA IDENTYFIKATORÓW W URL
# ============================================================

def get_query_parameter(source_url, parameter_name):
    """
    Pobiera wskazany parametr z adresu URL.
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
    Pobiera lgid z adresu ligi.
    """

    return get_query_parameter(
        source_url,
        "lgid",
    )


def get_tournament_id(source_url):
    """
    Pobiera id turnieju z adresu turnieju.
    """

    return get_query_parameter(
        source_url,
        "id",
    )


# ============================================================
# POBIERANIE WYDARZEŃ LIGI
# ============================================================

def download_league_events(source_url):
    """
    Pobiera z endpointu XHR listę wydarzeń przypisanych do ligi.

    Endpoint został odtworzony na podstawie żądania widocznego
    w zakładce Network przeglądarki.
    """

    league_id = get_league_id(source_url)

    endpoint_url = (
        f"{LEAGUE_API_URL}"
        f"?cmd=get_season_list&lgid={league_id}"
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

    direct_tmids = extract_ids_by_key(
        data,
        "tmid",
    )

    logging.info(
        "Liczba znalezionych tdid: %s",
        len(tournament_ids),
    )

    if tournament_ids:
        logging.info("ZNALEZIONE TDID:")

        for tournament_id in sorted(tournament_ids):
            logging.info("TDID: %s", tournament_id)
    else:
        logging.warning(
            "W odpowiedzi endpointu ligi "
            "nie znaleziono pola tdid."
        )

    logging.info(
        "Liczba tmid znalezionych bezpośrednio "
        "w odpowiedzi ligi: %s",
        len(direct_tmids),
    )

    if direct_tmids:
        logging.info("TMID Z ODPOWIEDZI LIGI:")

        for tmid in sorted(direct_tmids):
            logging.info("TMID: %s", tmid)

    return {
        "data": data,
        "tdids": tournament_ids,
        "tmids": {
            tmid
            for tmid in direct_tmids
            if is_valid_tmid(tmid)
        },
    }


# ============================================================
# ANALIZA STRONY TURNIEJU LUB INNEGO ŹRÓDŁA
# ============================================================

def is_relevant_n01_link(base_url, candidate_url):
    """
    Sprawdza, czy link prowadzi do istotnej strony N01.
    """

    base_host = urlparse(base_url).netloc
    candidate = urlparse(candidate_url)

    if candidate.netloc and candidate.netloc != base_host:
        return False

    path = candidate.path.lower()
    query = candidate.query.lower()

    relevant_path = (
        "/n01/league/" in path
        or "/n01/tournament/" in path
    )

    relevant_query = any(
        marker in query
        for marker in (
            "lgid=",
            "id=t_",
            "tdid=",
            "tmid=",
        )
    )

    return relevant_path and relevant_query


def discover_tmids_from_html(source_url):
    """
    Przechodzi po stronie i wybranych linkach N01.

    Szuka wyłącznie jawnego parametru:
    ?tmid=...
    lub:
    &tmid=...
    """

    queue = [source_url]
    visited = set()
    found_tmids = set()

    while (
        queue
        and len(visited) < MAX_DISCOVERY_PAGES_PER_SOURCE
    ):
        current_url = queue.pop(0)

        if current_url in visited:
            continue

        visited.add(current_url)

        logging.info(
            "Analiza strony: %s",
            current_url,
        )

        try:
            page_text, final_url = fetch_text(current_url)

        except requests.RequestException as exc:
            logging.warning(
                "Nie udało się pobrać %s: %s",
                current_url,
                exc,
            )
            continue

        found_tmids.update(
            extract_tmids(final_url)
        )

        found_tmids.update(
            extract_tmids(page_text)
        )

        soup = BeautifulSoup(
            page_text,
            "html.parser",
        )

        for tag in soup.find_all("a"):
            href = tag.get("href")

            if not href:
                continue

            absolute_url = urljoin(
                final_url,
                href,
            )

            found_tmids.update(
                extract_tmids(absolute_url)
            )

            if (
                is_relevant_n01_link(
                    final_url,
                    absolute_url,
                )
                and absolute_url not in visited
                and absolute_url not in queue
            ):
                queue.append(absolute_url)

        time.sleep(REQUEST_DELAY_SECONDS)

    valid_tmids = {
        tmid
        for tmid in found_tmids
        if is_valid_tmid(tmid)
    }

    logging.info(
        "Źródło %s: odwiedzono %s stron, "
        "znaleziono %s prawidłowych tmid",
        source_url,
        len(visited),
        len(valid_tmids),
    )

    return valid_tmids


def discover_tmids(source_url):
    """
    Rozpoznaje rodzaj źródła i wybiera właściwy sposób
    wyszukiwania identyfikatorów meczów.
    """

    if "/n01/league/portal.php" in source_url:
        league_result = download_league_events(
            source_url
        )

        return league_result["tmids"]

    return discover_tmids_from_html(
        source_url
    )


# ============================================================
# POBIERANIE DANYCH KONKRETNEGO MECZU
# ============================================================

def download_match(tmid):
    """
    Pobiera pełny JSON konkretnego meczu.
    """

    if not is_valid_tmid(tmid):
        raise ValueError(
            f"Nieprawidłowy format tmid: {tmid}"
        )

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Content-Type": "application/json; charset=UTF-8",
        "Origin": "https://n01darts.com",
        "Referer": (
            "https://n01darts.com/n01/league/"
            f"n01_view.html?tmid={tmid}"
        ),
    }

    payload = {
        "tmid": tmid,
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
        preview = response.text[:1000]

        raise ValueError(
            f"Odpowiedź dla {tmid} nie jest JSON-em: "
            f"{preview}"
        ) from exc

    if not isinstance(data, dict):
        raise ValueError(
            f"Nieoczekiwany typ odpowiedzi dla {tmid}: "
            f"{type(data).__name__}"
        )

    if not data:
        raise ValueError(
            f"Endpoint zwrócił pusty JSON dla tmid: {tmid}"
        )

    return data


# ============================================================
# OPERACJE NA BAZIE DANYCH
# ============================================================

def get_active_sources(conn):
    """
    Pobiera aktywne adresy źródłowe z Supabase.
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


def save_match(
    conn,
    source_url,
    requested_tmid,
    data,
):
    """
    Zapisuje lub aktualizuje mecz w tabeli matches.
    """

    actual_tmid = str(
        data.get("tmid")
        or requested_tmid
    ).strip()

    if not is_valid_tmid(actual_tmid):
        raise ValueError(
            f"Nieprawidłowy tmid zwrócony "
            f"przez endpoint: {actual_tmid}"
        )

    player_1 = first_existing(
        data,
        [
            "player1",
            "player_1",
            "p1_name",
            "name1",
        ],
    )

    player_2 = first_existing(
        data,
        [
            "player2",
            "player_2",
            "p2_name",
            "name2",
        ],
    )

    match_status = first_existing(
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
                player_1,
                player_2,
                match_status,
                match_date,
            ),
        )


def update_source_status(
    conn,
    source_id,
    error_message=None,
):
    """
    Aktualizuje datę sprawdzenia i ewentualny błąd źródła.
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
    Kończy wpis synchronizacji.
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

        log_id = create_sync_log(conn)
        conn.commit()

        try:
            sources = get_active_sources(conn)

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
                        "Źródło %s zwróciło %s tmid.",
                        source_url,
                        len(source_tmids),
                    )

                    for tmid in sorted(source_tmids):
                        try:
                            logging.info(
                                "Pobieranie meczu: %s",
                                tmid,
                            )

                            data = download_match(tmid)

                            save_match(
                                conn,
                                source_url,
                                tmid,
                                data,
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

            if errors_count == 0:
                final_status = "success"
            else:
                final_status = (
                    "completed_with_errors"
                )

            final_message = (
                f"Źródła: {sources_processed}; "
                f"tmid: {len(all_tmids_found)}; "
                f"mecze: {matches_downloaded}; "
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

            logging.info(final_message)

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
