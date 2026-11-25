"""
Script di aggiornamento automatico per Game Pass Tracker.

Scarica i giochi in arrivo e annunciati da Pure Xbox e XboxGamePassList.
Per le uscite usa Xbox Wire e Pure Xbox, con richieste HTTP gratuite.
ZenRows e' solo un fallback per le fonti dei giochi in arrivo, con un
massimo di due richieste per esecuzione. Salva tutto in
upcoming_data.json, letto direttamente dall'app Flutter.

La chiave API di ZenRows viene letta dalla variabile d'ambiente
ZENROWS_API_KEY (impostata come "secret" nel workflow GitHub Actions,
non scritta qui nel codice).
"""

import json
import os
import re
import time
from urllib.parse import urljoin, urlparse
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

ZENROWS_API_KEY = os.environ.get("ZENROWS_API_KEY", "")
ZENROWS_ENDPOINT = "https://api.zenrows.com/v1/"
ZENROWS_MAX_REQUESTS = 2  # 2 x 25 credits max per run
zenrows_requests = 0
XBOX_WIRE_TAG = "https://news.xbox.com/en-us/tag/xbox-game-pass/"
PUREXBOX_NEWS = "https://www.purexbox.com/news/"

COMING_URL = "https://gg.deals/subscription-news/the-list-of-all-games-coming-to-game-pass/"
XGPL_BASE_URL = "https://www.xboxgamepasslist.com/"
XGPL_MAX_PAGES = 15
PUREXBOX_URL = "https://www.purexbox.com/guides/xbox-game-pass-in-2026-the-full-list-of-everything-announced-so-far"
LEAVING_INDEX_URL = "https://gg.deals/news/games-leaving-game-pass/"
WAVE_INDEX_URL = "https://gg.deals/news/subscriptions/"


def fetch_soup_direct(url: str) -> BeautifulSoup:
    """Richiesta HTTP semplice, senza passare da ZenRows: piu' veloce e
    gratuita, da provare per prima sui siti che potrebbero non avere
    bisogno di un servizio anti-bot dedicato."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    response = requests.get(url, headers=headers, timeout=20)
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")


def fetch_soup(url: str, js_render: bool = True, antibot: bool = False) -> BeautifulSoup:
    global zenrows_requests
    if not ZENROWS_API_KEY:
        raise RuntimeError("ZENROWS_API_KEY non impostata.")
    if zenrows_requests >= ZENROWS_MAX_REQUESTS:
        raise RuntimeError("Limite ZenRows per esecuzione raggiunto.")
    zenrows_requests += 1
    print(f"[zenrows] Richiesta {zenrows_requests}/{ZENROWS_MAX_REQUESTS} verso {urlparse(url).hostname}")
    time.sleep(1)
    params = {
        "url": url,
        "apikey": ZENROWS_API_KEY,
        "premium_proxy": "true",
    }
    if js_render:
        params["js_render"] = "true"
        params["wait"] = "3000"
    if antibot:
        params["antibot"] = "true"

    try:
        response = requests.get(ZENROWS_ENDPOINT, params=params, timeout=90)
    except requests.RequestException:
        raise RuntimeError("Richiesta ZenRows fallita (rete/timeout).") from None
    if not response.ok:
        raise RuntimeError(f"ZenRows HTTP {response.status_code}")
    return BeautifulSoup(response.text, "html.parser")


def looks_like_game_title(text: str) -> bool:
    if not (2 <= len(text) <= 80):
        return False
    lowered = text.lower()
    if "?" in text:
        return False
    noise_starts = (
        "how to", "what is", "what are", "why", "when will", "where",
        "regular price", "read more", "compare prices", "best price",
        "buy ", "sign up", "subscribe", "follow us", "related",
    )
    if lowered.startswith(noise_starts):
        return False
    noise_contains = ("cd key", "activate", "cookie", "privacy policy")
    if any(phrase in lowered for phrase in noise_contains):
        return False
    return True


def clean_date_text(text: str) -> str:
    text = re.sub(r"\(\s*source\s*(,\s*source\s*)*\)", "", text, flags=re.IGNORECASE)
    return text.strip(" -–—\t")


def parse_date(text: str):
    text = text.strip()
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%d %B %Y"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


COMING_STATUS_PATTERN = re.compile(
    r"Coming\s+((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?"
    r"\s+\d{1,2},?\s+\d{4}|TBA)",
    re.IGNORECASE,
)



def parse_xgpl_date(text: str):
    text = text.strip().replace(",", "")
    for fmt in ("%b %d %Y", "%B %d %Y"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


TIER_SPLIT_PATTERN = re.compile(
    r"(?=Ultimate|Premium|Essential|Ea Play|Game Catalog)"
)

# Voci del menu/interfaccia del sito osservate direttamente nei risultati
# reali: non sono giochi, vanno scartate a prescindere.
XGPL_NAV_NOISE = {
    "games list", "new", "leaving soon", "best", "plans", "calendar",
    "blog", "search games", "pc games", "cloud games", "all games table",
    "popular searches", "catalog guide", "new and leaving",
    "plan and platform guide", "faq", "new this month", "coming soon",
    "console games", "best rated", "short games", "console games",
    "pc game pass games", "cloud games", "ultimate games", "premium games",
    "essential games", "day-one games", "compare plans", "xbox catalog",
    "xbox wire", "about", "contact", "privacy", "terms", "sitemap",
    "view new games", "view coming soon", "view leaving soon",
    "best games guide", "leaving soon tracker", "new games",
}


def clean_xgpl_title(raw_title: str) -> str:
    """Rimuove le informazioni di piano/piattaforma che il sito attacca
    senza spazio subito dopo il titolo (es. 'Cricket 24Ultimate, ...')."""
    return TIER_SPLIT_PATTERN.split(raw_title, maxsplit=1)[0].strip()


def extract_xgpl_games(soup):
    results = []
    seen_titles = set()

    for link in soup.find_all("a"):
        raw_text = link.get_text(strip=True)
        if not raw_text:
            continue

        # Cerchiamo lo stato "Coming <data>"/"Coming TBA" DIRETTAMENTE nel
        # testo di questo stesso link: card e informazioni sono tutte
        # concatenate insieme in un unico <a>, quindi non serve (ed è
        # rischioso) risalire ai contenitori genitori.
        status_match = COMING_STATUS_PATTERN.search(raw_text)
        if status_match is None:
            continue
        date_text = status_match.group(1)

        title = clean_xgpl_title(raw_text)
        if not title or title in seen_titles or not looks_like_game_title(title):
            continue
        if title.lower() in XGPL_NAV_NOISE:
            continue

        seen_titles.add(title)
        results.append({"title": title, "date_text": date_text})

    return results


def fetch_xgpl_games():
    """Una singola richiesta (la paginazione tramite ?page=N si e'
    rivelata inutile: ogni pagina restituiva lo stesso contenuto)."""
    url = f"{XGPL_BASE_URL}?status=COMING_SOON"
    try:
        games = extract_xgpl_games(fetch_soup_direct(url))
        if games:
            print(f"[coming-xgpl] {len(games)} giochi, richiesta gratuita.")
            return games
    except Exception as e:
        print(f"[coming-xgpl] Richiesta diretta fallita: {type(e).__name__}")
    try:
        games = extract_xgpl_games(fetch_soup(url, js_render=True))
    except Exception as e:
        print(f"[coming-xgpl] Errore: {e}")
        return []
    print(f"[coming-xgpl] {len(games)} giochi trovati (fonte supplementare).")
    return games


def parse_purexbox_date(date_text: str, today_year: int):
    """Analizza le date della tabella Pure Xbox nei loro vari formati:
    'January 6, 2026' (esatta), 'June 2026 TBD' (mese+anno), 'December
    2026' (mese+anno), 'TBD' (nessuna data)."""
    text = date_text.strip()

    if text.upper() == "TBD":
        return None, None

    # Formato "Month YYYY TBD" -> trattiamo come approssimativa
    cleaned = re.sub(r"\s*TBD\s*$", "", text, flags=re.IGNORECASE).strip()

    for fmt in ("%B %d, %Y", "%B %d. %Y"):
        try:
            return datetime.strptime(cleaned, fmt), None
        except ValueError:
            continue

    # Formato "Month YYYY" (solo mese e anno, senza giorno preciso)
    for fmt in ("%B %Y",):
        try:
            parsed = datetime.strptime(cleaned, fmt)
            return None, cleaned  # lo trattiamo come approssimativo
        except ValueError:
            continue

    return None, cleaned if cleaned else None


def scrape_coming_and_announced():
    """
    Usiamo la guida di Pure Xbox (sito di notizie con una tabella
    Data/Gioco/Piattaforma aggiornata regolarmente), instradata tramite
    ZenRows. E' una pagina "normale" da sito di notizie, molto piu'
    semplice da leggere in modo affidabile delle web-app complesse
    provate in precedenza.
    """
    try:
        soup = fetch_soup_direct(PUREXBOX_URL)
        if soup.find("table") is None:
            raise ValueError("Tabella mancante nella risposta diretta")
        print("[coming] Richiesta semplice riuscita (senza ZenRows).")
    except Exception as e:
        print(f"[coming] Richiesta semplice fallita ({e}), riprovo con ZenRows...")
        soup = fetch_soup(PUREXBOX_URL, js_render=True)

    table = soup.find("table")
    if table is None:
        print("[coming] Nessuna tabella trovata nella pagina.")
        return [], []

    today = datetime.now(timezone.utc).replace(tzinfo=None)
    with_date = []
    announced = []
    seen = set()

    rows = table.find_all("tr")
    print(f"[coming] Righe trovate nella tabella: {len(rows)}")

    for row in rows:
        # Scartiamo le righe di intestazione della tabella (celle <th>,
        # o testo letterale "Date"/"Game" che a volte si ripete anche
        # dentro il corpo della tabella per motivi di accessibilita').
        if row.find("th") is not None:
            continue

        cells = row.find_all("td")
        if len(cells) < 2:
            continue

        date_text = cells[0].get_text(strip=True)
        title = cells[1].get_text(strip=True)

        if date_text.lower() == "date" or title.lower() == "game":
            continue
        if not title or not date_text or not looks_like_game_title(title):
            continue
        if title in seen:
            continue
        seen.add(title)

        if date_text.upper() == "TBD":
            announced.append({"title": title})
            continue

        exact_date, approx_label = parse_purexbox_date(date_text, today.year)

        if exact_date:
            if exact_date.date() < today.date():
                continue  # gia' uscito, non e' "in arrivo"
            with_date.append({
                "title": title,
                "exactDate": exact_date.strftime("%Y-%m-%d"),
            })
        elif approx_label:
            with_date.append({"title": title, "approxLabel": approx_label})
        else:
            announced.append({"title": title})

    print(f"[coming] Pure Xbox — Con data: {len(with_date)}, "
          f"Annunciati: {len(announced)}")

    # Fonte supplementare: xboxgamepasslist.com, per recuperare titoli
    # che l'articolo Pure Xbox (specifico per il 2026) non copre, es.
    # giochi gia' confermati per il 2027.
    try:
        xgpl_games = fetch_xgpl_games()
    except Exception as e:
        print(f"[coming-xgpl] Errore: {e}")
        xgpl_games = []

    for game in xgpl_games:
        title = game["title"]
        if title in seen:
            continue  # gia' presente da Pure Xbox

        date_text = game["date_text"]
        if date_text.upper() == "TBA":
            seen.add(title)
            announced.append({"title": title})
            continue

        parsed = parse_xgpl_date(date_text)
        if parsed is None:
            continue
        if parsed.date() < today.date():
            continue  # gia' uscito

        seen.add(title)
        with_date.append({
            "title": title,
            "exactDate": parsed.strftime("%Y-%m-%d"),
        })

    print(f"[coming] Totale dopo la fusione — Con data: {len(with_date)}, "
          f"Annunciati: {len(announced)}")
    return with_date, announced



# Fonti gratuite per le uscite: Xbox Wire (ufficiale) e Pure Xbox.
# Non usare ZenRows per le uscite: nessun retry a pagamento su GG.deals.
WIRE_LINK = re.compile(r"^/en-us/(\d{4})/(\d{2})/(\d{2})/([^/]+)/?$")
LEAVING_HEADING = re.compile(
    r"^Leaving\s+([A-Za-z]+)\s+(\d{1,2})(?:,?\s+(\d{4}))?$", re.I
)
PLATFORM_SUFFIX = re.compile(r"\s*\(([^()]*)\)\s*$")


def departure_date(month, day, year, article_date):
    """Interpreta il cambio d'anno degli annunci di dicembre."""
    try:
        month_num = datetime.strptime(month[:3].title(), "%b").month
        resolved_year = int(year) if year else article_date.year
        if not year and article_date.month == 12 and month_num == 1:
            resolved_year += 1
        return datetime(resolved_year, month_num, int(day)).date()
    except (ValueError, TypeError):
        return None


def parse_wire_leaving(soup, article_date, today):
    games = {}
    for heading in soup.find_all(["h2", "h3", "h4"]):
        match = LEAVING_HEADING.fullmatch(heading.get_text(" ", strip=True))
        if not match:
            continue
        date = departure_date(*match.groups(), article_date)
        if date is None or date < today:
            continue
        # Il primo elenco dopo il titolo, prima della prossima sezione.
        for node in heading.find_all_next(["h2", "h3", "h4", "ul", "ol"]):
            if node.name in ("h2", "h3", "h4"):
                break
            for item in node.find_all("li", recursive=False):
                title = item.get_text(" ", strip=True)
                suffix = PLATFORM_SUFFIX.search(title)
                if suffix and re.search(r"\b(cloud|console|pc|handheld|xbox)\b",
                                        suffix.group(1), re.I):
                    title = title[:suffix.start()].strip()
                if title and len(title) <= 150:
                    games[title.casefold()] = {"title": title, "exactDate": date.isoformat()}
            break
    return list(games.values())


def wire_leaving(today):
    soup = fetch_soup_direct(XBOX_WIRE_TAG)
    candidates = {}
    for link in soup.find_all("a", href=True):
        url = urlparse(urljoin(XBOX_WIRE_TAG, link["href"]))
        if url.hostname != "news.xbox.com":
            continue
        match = WIRE_LINK.fullmatch(url.path)
        if not match:
            continue
        year, month, day, slug = match.groups()
        if "game-pass" not in slug or not (
            re.search(r"wave[-_]\d", slug) or
            "coming to xbox game pass" in link.get_text(" ", strip=True).lower()
        ):
            continue
        try:
            published = datetime(int(year), int(month), int(day)).date()
        except ValueError:
            continue
        if 0 <= (today - published).days <= 40:
            candidates["https://news.xbox.com" + url.path] = published
    for url, published in sorted(candidates.items(), key=lambda p: p[1], reverse=True)[:3]:
        try:
            games = parse_wire_leaving(fetch_soup_direct(url), published, today)
        except Exception as exc:
            print(f"[leaving-wire] {type(exc).__name__}")
            continue
        if games:
            print(f"[leaving-wire] {len(games)} giochi: {url}")
            return games
    return []


def purexbox_leaving(today):
    # Archivio del mese corrente, non le previsioni non confermate.
    index_url = PUREXBOX_NEWS + today.strftime("%Y/%m")
    soup = fetch_soup_direct(index_url)
    candidates = {}
    for link in soup.find_all("a", href=True):
        url = urlparse(urljoin(index_url, link["href"]))
        if url.hostname != "www.purexbox.com":
            continue
        match = re.fullmatch(r"/news/(\d{4})/(\d{2})/([^/]+)/?", url.path)
        if not match or "-games-are-leaving-xbox-game-pass-" not in match.group(3):
            continue
        slug = match.group(3)
        if "might" in slug:
            continue
        candidates["https://www.purexbox.com" + url.path] = ("-in-late-" in slug)
    for url, _ in sorted(candidates.items(), key=lambda p: p[1], reverse=True)[:3]:
        try:
            article = fetch_soup_direct(url)
        except Exception as exc:
            print(f"[leaving-purexbox] {type(exc).__name__}")
            continue
        games = {}
        for table in article.find_all("table"):
            for row in table.find_all("tr"):
                cells = row.find_all("td")
                if len(cells) < 3:
                    continue
                date_text = cells[0].get_text(" ", strip=True)
                match = re.fullmatch(r"([A-Za-z]+)\s+(\d{1,2})(?:,?\s+(\d{4}))?", date_text)
                if not match:
                    continue
                date = departure_date(*match.groups(), today)
                title = cells[1].get_text(" ", strip=True)
                if date and date >= today and title and len(title) <= 150:
                    games[title.casefold()] = {"title": title, "exactDate": date.isoformat()}
        if games:
            print(f"[leaving-purexbox] {len(games)} giochi: {url}")
            return list(games.values())
    return []


def scrape_leaving_soon():
    today = datetime.now(timezone.utc).date()
    for source in (wire_leaving, purexbox_leaving):
        try:
            games = source(today)
            if games:
                return games
        except Exception as exc:
            print(f"[leaving] {source.__name__}: {type(exc).__name__}")
    print("[leaving] Nessuna uscita futura verificabile dalle fonti gratuite.")
    return []


# ============================================================
# GIOCHI 2027 CONFERMATI: lista curata a mano.
# Nessuna delle fonti automatiche testate arriva cosi' lontano nel
# tempo in modo affidabile — ma sono pochi titoli che cambiano
# raramente (circa un annuncio al mese), quindi ha senso mantenerli
# qui a mano invece di inseguire un'altra fonte automatica.
# Per aggiornarla: aggiungi/modifica una riga e basta, si unisce da
# sola al resto dei dati ad ogni esecuzione.
# ============================================================
CONFIRMED_2027_WITH_DATE = [
    {"title": "Stranger Than Heaven", "exactDate": "2027-01-15"},
    {"title": "Persona 4 Revival", "exactDate": "2027-02-18"},
    {"title": "Fable", "exactDate": "2027-02-23"},
    {"title": "JOIN US", "approxLabel": "March 2027"},
]
CONFIRMED_2027_ANNOUNCED = [
    {"title": "Clockwork Revolution"},
    {"title": "State of Decay 3"},
    {"title": "Senua"},
    {"title": "Spyro: A Realm Beyond"},
    {"title": "The Expanse: Osiris Reborn"},
    {"title": "Wo Long 2: Wings of Ember"},
    {"title": "Son of Thanjai"},
]


MONTH_YEAR_PATTERN = re.compile(
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(\d{4})",
    re.IGNORECASE,
)
SEASON_TO_MONTH = {"spring": "04", "summer": "07", "fall": "10",
                    "autumn": "10", "winter": "01"}


def sort_key_for_entry(game):
    """Calcola una chiave di ordinamento anche per le date approssimate
    (es. "June 2026", "Spring 2027"), cosi' si intrecciano correttamente
    con le date esatte invece di finire tutte in fondo alla lista."""
    if game.get("exactDate"):
        return game["exactDate"]

    label = (game.get("approxLabel") or "").strip()

    match = MONTH_YEAR_PATTERN.search(label)
    if match:
        month_num = datetime.strptime(match.group(1)[:3], "%b").month
        return f"{match.group(2)}-{month_num:02d}-15"

    for season, month in SEASON_TO_MONTH.items():
        if season in label.lower():
            year_match = re.search(r"\d{4}", label)
            year = year_match.group(0) if year_match else "9999"
            return f"{year}-{month}-15"

    year_only = re.match(r"^\s*(\d{4})\s*$", label)
    if year_only:
        return f"{year_only.group(1)}-06-15"

    return "9999-99-99"  # TBC, TBA e simili: in fondo alla lista


def merge_curated_2027(with_date, announced):
    seen = {g["title"] for g in with_date} | {g["title"] for g in announced}
    for game in CONFIRMED_2027_WITH_DATE:
        if game["title"] not in seen:
            with_date.append(game)
            seen.add(game["title"])
    for game in CONFIRMED_2027_ANNOUNCED:
        if game["title"] not in seen:
            announced.append(game)
            seen.add(game["title"])

    # Ordiniamo cronologicamente: prima le date esatte (in ordine), poi
    # quelle approssimate (mese/anno) in fondo, nell'ordine in cui sono
    # arrivate. Cosi' l'ordine e' corretto gia' nel file salvato, senza
    # dipendere da come l'app lo gestisce.
    with_date.sort(key=sort_key_for_entry)

    return with_date, announced


def main():
    today = datetime.now(timezone.utc).date().isoformat()
    try:
        with open("upcoming_data.json", encoding="utf-8") as f:
            previous = json.load(f)
    except (OSError, ValueError):
        previous = {}

    # Una sola procedura per fonte: niente ripetizioni di chiamate a pagamento.
    try:
        with_date, announced = scrape_coming_and_announced()
    except Exception as exc:
        print(f"[coming] Aggiornamento fallito: {type(exc).__name__}")
        with_date, announced = [], []
    if not (with_date or announced):
        print("[coming] Mantengo i dati precedenti.")
        with_date = [g for g in previous.get("upcomingWithDate", [])
                     if not g.get("exactDate") or g["exactDate"] >= today]
        announced = previous.get("upcomingAnnounced", [])

    with_date, announced = merge_curated_2027(with_date, announced)
    leaving = scrape_leaving_soon()
    if not leaving:
        # Conserva solo uscite precedentemente verificate e non ancora scadute.
        leaving = [g for g in previous.get("leavingSoon", [])
                   if g.get("exactDate") and g["exactDate"] >= today]
        print(f"[leaving] Conservati {len(leaving)} dati precedenti non scaduti.")

    data = {
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "upcomingWithDate": with_date,
        "upcomingAnnounced": announced,
        "leavingSoon": leaving,
    }

    with open("upcoming_data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"Salvati: {len(with_date)} con data, {len(announced)} annunciati, "
          f"{len(leaving)} in uscita. ZenRows: {zenrows_requests} richieste.")




if __name__ == "__main__":
    main()
