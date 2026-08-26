import html
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import psycopg
import requests
from bs4 import BeautifulSoup
from psycopg.types.json import Jsonb


DATABASE_URL = os.environ["DATABASE_URL"]

MATCH_API_URL = (
    "https://tk2-228-23746.vs.sakura.ne.jp/"
    "n01/tournament/n01_user_t.php?cmd=match_view&sid="
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/139.0 Safari/537.36"
)

REQUEST_TIMEOUT = 30
REQUEST_DELAY_SECONDS = 0.6
MAX_DISCOVERY_PAGES_PER_SOURCE = 100

TMID_PATTERNS = [
    re.compile(
        r"[?&]tmid=([A-Za-z0-9_-]+)",
        re.IGNORECASE,
    )
]


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

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


def utc_now():
    return datetime.now(timezone.utc)


def get_active_sources(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            select id, source_url, source_type
            from source_urls
            where is_active = true
            order by id;
            """
        )
        return cur.fetchall()


def fetch_text(url):
    response = session.get(
        url,
        timeout=REQUEST_TIMEOUT,
        allow_redirects=True,
    )
    response.raise_for_status()
    return response.text, response.url


def extract_tmids(text):
    decoded_text = html.unescape(text)
    tmids = set()

    for pattern in TMID_PATTERNS:
        for match in pattern.findall(decoded_text):
            normalized = match.strip()

            if normalized:
                tmids.add(normalized)

    return tmids


def is_relevant_n01_link(base_url, candidate_url):
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
            "tmid=",
        )
    )

    return relevant_path and relevant_query


def discover_tmids(source_url):
    """
    Przechodzi po stronie źródłowej i istotnych linkach N01.
    Szuka tmid w:
    - HTML,
    - adresach href,
    - skryptach inline,
    - danych osadzonych w kodzie strony.
    """

    queue = [source_url]
    visited = set()
    found_tmids = set()

    while queue and len(visited) < MAX_DISCOVERY_PAGES_PER_SOURCE:
        current_url = queue.pop(0)

        if current_url in visited:
            continue

        visited.add(current_url)
        logging.info("Analiza strony: %s", current_url)

        try:
            page_text, final_url = fetch_text(current_url)
        except requests.RequestException as exc:
            logging.warning(
                "Nie udało się pobrać %s: %s",
                current_url,
                exc,
            )
            continue

        found_tmids.update(extract_tmids(page_text))
        found_tmids.update(extract_tmids(final_url))

        soup = BeautifulSoup(page_text, "html.parser")

        for tag in soup.find_all(["a", "script"]):
            if tag.name == "a":
                href = tag.get("href")

                if not href:
                    continue

                absolute_url = urljoin(final_url, href)

                found_tmids.update(extract_tmids(absolute_url))

                if (
                    is_relevant_n01_link(final_url, absolute_url)
                    and absolute_url not in visited
                    and absolute_url not in queue
                ):
                    queue.append(absolute_url)

            elif tag.name == "script":
                script_src = tag.get("src")

                if script_src:
                    script_url = urljoin(final_url, script_src)

                    try:
                        script_text, _ = fetch_text(script_url)
                        found_tmids.update(extract_tmids(script_text))
                    except requests.RequestException:
                        pass

                if tag.string:
                    found_tmids.update(extract_tmids(tag.string))

        time.sleep(REQUEST_DELAY_SECONDS)

    logging.info(
        "Źródło %s: odwiedzono %s stron, znaleziono %s tmid",
        source_url,
        len(visited),
        len(found_tmids),
    )

    print("TMID FOUND:")
    for t in sorted(found_tmids):
        print(t)
    
    return found_tmids


def download_match(tmid):
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

    payload = {"tmid": tmid}

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
        preview = response.text[:500]

        raise ValueError(
            f"Odpowiedź dla {tmid} nie jest JSON-em: {preview}"
        ) from exc

    if not isinstance(data, dict):
        raise ValueError(
            f"Nieoczekiwany typ odpowiedzi dla {tmid}: "
            f"{type(data).__name__}"
        )

    return data


def first_existing(data, candidate_keys):
    for key in candidate_keys:
        value = data.get(key)

        if value not in (None, ""):
            return value

    return None


def parse_match_date(data):
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


def save_match(conn, source_url, requested_tmid, data):
    actual_tmid = str(
        data.get("tmid") or requested_tmid
    ).strip()

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


def update_source_status(conn, source_id, error_message=None):
    with conn.cursor() as cur:
        cur.execute(
            """
            update source_urls
            set
                last_checked_at = now(),
                last_error = %s
            where id = %s;
            """,
            (error_message, source_id),
        )


def create_sync_log(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into sync_log(status)
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


def main():
    sources_processed = 0
    all_tmids_found = set()
    matches_downloaded = 0
    errors_count = 0

    with psycopg.connect(
        DATABASE_URL,
        autocommit=False,
    ) as conn:
        log_id = create_sync_log(conn)
        conn.commit()

        try:
            sources = get_active_sources(conn)

            if not sources:
                logging.warning(
                    "Brak aktywnych adresów w source_urls."
                )

            for source_id, source_url, source_type in sources:
                sources_processed += 1

                logging.info(
                    "Przetwarzanie źródła %s: %s",
                    source_type,
                    source_url,
                )

                try:
                    source_tmids = discover_tmids(source_url)
                    all_tmids_found.update(source_tmids)

                    for tmid in sorted(source_tmids):
                        try:
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
                                "Zapisano mecz %s",
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

                        time.sleep(REQUEST_DELAY_SECONDS)

                    update_source_status(
                        conn,
                        source_id,
                        error_message=None,
                    )
                    conn.commit()

                except Exception as exc:
                    conn.rollback()
                    errors_count += 1

                    update_source_status(
                        conn,
                        source_id,
                        error_message=str(exc)[:2000],
                    )
                    conn.commit()

                    logging.exception(
                        "Błąd źródła %s: %s",
                        source_url,
                        exc,
                    )

            final_status = (
                "success"
                if errors_count == 0
                else "completed_with_errors"
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
