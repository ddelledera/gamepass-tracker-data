"""
Script di aggiornamento automatico per Game Pass Tracker.

Scarica le pagine di gg.deals con i giochi in arrivo/annunciati/in uscita
da Xbox Game Pass, le analizza, e salva tutto in un file JSON
(upcoming_data.json) che l'app Flutter scarica direttamente.

Pensato per girare automaticamente ogni giorno tramite GitHub Actions.
"""

import json
import re
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
}

COMING_URL = "https://gg.deals/subscription-news/the-list-of-all-games-coming-to-game-pass/"
LEAVING_INDEX_URL = "https://gg.deals/news/games-leaving-game-pass/"

MONTHS_IT = {
    1: "Gennaio", 2: "Febbraio", 3: "Marzo", 4: "Aprile",
    5: "Maggio", 6: "Giugno", 7: "Luglio", 8: "Agosto",
    9: "Settembre", 10: "Ottobre", 11: "Novembre", 12: "Dicembre",
}


def fetch_soup(url: str) -> BeautifulSoup:
    response = requests.get(url, headers=HEADERS, timeout=20)
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")


def parse_date(text: str):
    """
    Prova a interpretare una data testuale (es. "August 4, 2026") in un
    oggetto datetime. Ritorna None se non è una data esatta riconoscibile
    (es. "TBC", "Q4 2026", "2026" da soli restano come testo libero).
    """
    text = text.strip()
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%d %B %Y"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def clean_date_text(text: str) -> str:
    """Rimuove annotazioni tipo '( source )' o '( source, source )'."""
    text = re.sub(r"\(\s*source\s*(,\s*source\s*)*\)", "", text, flags=re.IGNORECASE)
    return text.strip(" -–—\t")


def scrape_coming_and_announced():
    """
    Analizza la pagina "lista completa" di gg.deals. È UNA lista unica per
    sezione, dove ogni riga è "Titolo - Data" e i giochi senza data
    confermata hanno semplicemente "TBC" al posto della data.
    """
    soup = fetch_soup(COMING_URL)

    with_date = []
    announced = []
    seen_titles = set()

    for heading in soup.find_all(re.compile("^h[1-4]$")):
        heading_text = heading.get_text(strip=True).lower()

        matches_keyword = "coming" in heading_text or "announced" in heading_text
        # Alcune sezioni sono intestate solo con l'anno (es. "2027"), senza
        # le parole "coming"/"announced": le riconosciamo comunque così.
        matches_year = re.search(r"\b20\d{2}\b", heading_text) is not None

        if not (matches_keyword or matches_year):
            continue

        # Raccogliamo TUTTE le liste (<ul>) che si trovano tra questa
        # intestazione e la prossima, non solo la prima: alcune sezioni
        # hanno più blocchi separati (es. suddivisi per mese).
        lists_found = []
        sibling = heading.find_next_sibling()
        steps = 0
        while sibling is not None and not re.match(r"^h[1-4]$", sibling.name or "") and steps < 30:
            if sibling.name == "ul":
                lists_found.append(sibling)
            sibling = sibling.find_next_sibling()
            steps += 1

        for ul in lists_found:
            for li in ul.find_all("li"):
                raw_text = li.get_text(" ", strip=True)
                if not raw_text:
                    continue

                # Separatore Titolo/Data: un trattino CIRCONDATO DA SPAZI,
                # per non spezzare titoli con trattini (es. "E-Day").
                parts = re.split(r"\s[-–—]\s", raw_text, maxsplit=1)
                title = parts[0].strip()
                date_text = clean_date_text(parts[1]) if len(parts) > 1 else ""

                if (not title or title in seen_titles
                        or not looks_like_game_title(title)):
                    continue
                seen_titles.add(title)

                if not date_text or date_text.upper() == "TBC":
                    announced.append({"title": title})
                    continue

                parsed = parse_date(date_text)
                if parsed:
                    with_date.append({
                        "title": title,
                        "exactDate": parsed.strftime("%Y-%m-%d"),
                    })
                else:
                    with_date.append({
                        "title": title,
                        "approxLabel": date_text,
                    })

    print(f"[coming] Con data: {len(with_date)}, Annunciati: {len(announced)}")
    return with_date, announced


def looks_like_game_title(text: str) -> bool:
    """
    Filtro per scartare 'rumore' tipico dei siti (FAQ, link di menu,
    testo promozionale) che finisce nei tag <li> insieme ai veri titoli.
    """
    if not (2 <= len(text) <= 80):
        return False

    lowered = text.lower()

    # Le domande FAQ e i link generici del sito non sono titoli di giochi.
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


GAME_PASS_TAG_PATTERN = re.compile(
    r"^(.*?)\s*\(([^)]*Game Pass[^)]*)\)\s*$"
)


def scrape_leaving_soon():
    """
    Trova l'articolo più recente sulla pagina indice "giochi in uscita" e
    ne estrae l'elenco dei titoli. Più fragile delle altre funzioni perché
    si basa su un articolo di notizie, non su una lista sempre aggiornata.

    Riconosciamo i veri giochi (e scartiamo menu/link del sito) cercando
    solo le righe che terminano con un tag tipo "(PC Game Pass, Ultimate,
    Premium)" — è il formato che questo sito usa sempre per i giochi.
    """
    index_soup = fetch_soup(LEAVING_INDEX_URL)

    candidate_links = []
    for a in index_soup.find_all("a", href=True):
        href = a["href"]
        if "/subscription-news/" in href and "leav" in href.lower():
            candidate_links.append(href)

    print(f"[leaving] Trovati {len(candidate_links)} link candidati.")
    if not candidate_links:
        return []

    article_link = candidate_links[0]
    if article_link.startswith("/"):
        article_link = "https://gg.deals" + article_link
    print(f"[leaving] Uso l'articolo: {article_link}")

    article_soup = fetch_soup(article_link)

    leaving = []
    # Cerchiamo il pattern sia nei tag <li> sia nei <p>, per non dipendere
    # troppo dalla struttura HTML esatta usata dall'articolo.
    for element in article_soup.find_all(["li", "p", "strong"]):
        text = element.get_text(" ", strip=True)
        match = GAME_PASS_TAG_PATTERN.match(text)
        if not match:
            continue

        title = match.group(1).strip(" -–—:")
        tiers = match.group(2).strip()

        if not looks_like_game_title(title) or len(title.split()) > 8:
            continue

        leaving.append({"title": title, "tiers": tiers})

    print(f"[leaving] Giochi riconosciuti tramite tag Game Pass: {len(leaving)}")

    seen = set()
    unique_leaving = []
    for game in leaving:
        if game["title"] not in seen:
            seen.add(game["title"])
            unique_leaving.append(game)

    return unique_leaving[:20]  # Limite di sicurezza


def main():
    try:
        with_date, announced = scrape_coming_and_announced()
    except Exception as e:
        print(f"Errore nello scraping 'coming/announced': {e}")
        with_date, announced = [], []

    try:
        leaving = scrape_leaving_soon()
    except Exception as e:
        print(f"Errore nello scraping 'leaving soon': {e}")
        leaving = []

    data = {
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "upcomingWithDate": with_date,
        "upcomingAnnounced": announced,
        "leavingSoon": leaving,
    }

    with open("upcoming_data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"Salvati: {len(with_date)} con data, {len(announced)} annunciati, "
          f"{len(leaving)} in uscita.")


if __name__ == "__main__":
    main()
