"""Look up an author's gender and pronouns from public sources.

Strategy: find third-person prose written about the author (Goodreads bio,
Wikipedia lead paragraph, the author's own website) and count the gendered
pronouns actually used, plus query Wikidata's structured "sex or gender"
property. A suggestion is only produced from explicit evidence — names are
never used to guess, since pen names and initials make that unreliable.
"""

import re
import requests
from urllib.parse import quote

from scrapers import fetch_page

API_HEADERS = {'User-Agent': 'BookDatabase/1.0 (personal library app)'}

# Wikidata P21 ("sex or gender") values -> AuthorGender names used by this app
WIKIDATA_GENDER_MAP = {
    'Q6581072': 'Female',      # female
    'Q6581097': 'Male',        # male
    'Q1052281': 'Female',      # trans woman
    'Q2449503': 'Male',        # trans man
    'Q48270': 'Nonbinary',     # non-binary
    'Q18116794': 'Nonbinary',  # genderfluid
    'Q505371': 'Nonbinary',    # agender
}

PRONOUN_SETS = {
    'she/her': {'she', 'her', 'hers', 'herself'},
    'he/him': {'he', 'him', 'his', 'himself'},
    'they/them': {'they', 'them', 'their', 'theirs', 'themself', 'themselves'},
}

PRONOUNS_TO_GENDER = {
    'she/her': 'Female',
    'he/him': 'Male',
    'they/them': 'Nonbinary',
}


def count_pronouns(text):
    """Count gendered pronoun tokens in text. Returns {'she/her': n, ...}."""
    words = re.findall(r"[a-z]+", text.lower())
    counts = {key: 0 for key in PRONOUN_SETS}
    for word in words:
        for key, tokens in PRONOUN_SETS.items():
            if word in tokens:
                counts[key] += 1
    return counts


def dominant_pronouns(counts):
    """Return the pronoun set the text clearly uses, or None if ambiguous.

    she/her and he/him need >=2 occurrences and >=80% dominance over each
    other. they/them is held to a higher bar (>=3, near-zero gendered hits)
    because 'they' is often plural rather than a personal pronoun."""
    she, he, they = counts['she/her'], counts['he/him'], counts['they/them']
    gendered = she + he
    if she >= 2 and she / gendered >= 0.8:
        return 'she/her'
    if he >= 2 and he / gendered >= 0.8:
        return 'he/him'
    if they >= 3 and gendered <= 1:
        return 'they/them'
    return None


def evidence_sentence(text, pronouns):
    """Return the first sentence of text containing one of the pronouns."""
    tokens = PRONOUN_SETS[pronouns]
    for sentence in re.split(r'(?<=[.!?])\s+', text):
        words = set(re.findall(r"[a-z]+", sentence.lower()))
        if words & tokens:
            sentence = sentence.strip()
            return sentence[:250] + ('…' if len(sentence) > 250 else '')
    return None


def _pronouns_from_text(text, source_url):
    """Return a partial suggestion dict from prose, or None."""
    if not text:
        return None
    pronouns = dominant_pronouns(count_pronouns(text))
    if not pronouns:
        return None
    return {
        'pronouns': pronouns,
        'gender': PRONOUNS_TO_GENDER[pronouns],
        'evidence': evidence_sentence(text, pronouns),
        'source_url': source_url,
    }


def bio_from_goodreads(url):
    """Extract the 'about the author' text from a Goodreads author page."""
    soup = fetch_page(url)
    # Full bio is in a span with id freeText<n>; the truncated preview is
    # freeTextContainer<n>. Prefer the longest matching span.
    spans = soup.select('.aboutAuthorInfo span[id^="freeText"]')
    if spans:
        return max((s.get_text(' ', strip=True) for s in spans), key=len)
    about = soup.select_one('.aboutAuthorInfo')
    if about:
        return about.get_text(' ', strip=True)
    return None


def wikidata_lookup(name):
    """Search Wikidata for the author. Returns (gender_name, wikipedia_title,
    entity_url, description) or (None, None, None, None)."""
    search = requests.get(
        'https://www.wikidata.org/w/api.php',
        params={'action': 'wbsearchentities', 'search': name, 'language': 'en',
                'type': 'item', 'limit': 5, 'format': 'json'},
        headers=API_HEADERS, timeout=15,
    ).json()
    ids = [hit['id'] for hit in search.get('search', [])]
    if not ids:
        return None, None, None, None

    entities = requests.get(
        'https://www.wikidata.org/w/api.php',
        params={'action': 'wbgetentities', 'ids': '|'.join(ids),
                'props': 'claims|descriptions|sitelinks', 'format': 'json'},
        headers=API_HEADERS, timeout=15,
    ).json().get('entities', {})

    for entity_id in ids:  # preserve search-relevance order
        entity = entities.get(entity_id, {})
        description = entity.get('descriptions', {}).get('en', {}).get('value', '')
        # Only trust entities described as some kind of writer — a name
        # search alone often matches an unrelated person.
        if not re.search(r'author|writer|novelist|poet|journalist|essayist|cartoonist|comics', description, re.I):
            continue
        gender = None
        for claim in entity.get('claims', {}).get('P21', []):
            value = claim.get('mainsnak', {}).get('datavalue', {}).get('value', {})
            gender = WIKIDATA_GENDER_MAP.get(value.get('id'))
            if gender:
                break
        wikipedia_title = entity.get('sitelinks', {}).get('enwiki', {}).get('title')
        if gender or wikipedia_title:
            return gender, wikipedia_title, f'https://www.wikidata.org/wiki/{entity_id}', description
    return None, None, None, None


def wikipedia_extract(title):
    """Return the lead-paragraph extract for a Wikipedia article."""
    response = requests.get(
        f'https://en.wikipedia.org/api/rest_v1/page/summary/{quote(title.replace(" ", "_"))}',
        headers=API_HEADERS, timeout=15,
    )
    if response.status_code != 200:
        return None
    return response.json().get('extract')


def lookup_author_info(name, goodreads_url=None, website=None):
    """Look up gender and pronouns for an author. Returns a dict with
    'gender', 'pronouns', 'evidence', 'source_url' (any of the first two may
    be None), or None if no source produced evidence. Network errors from one
    source are swallowed so the others still get a chance."""
    suggestion = None

    # 1. Goodreads author bio — best coverage for this library, and usually
    # third-person prose.
    if goodreads_url and '/author/' in goodreads_url:
        try:
            suggestion = _pronouns_from_text(bio_from_goodreads(goodreads_url), goodreads_url)
        except Exception:
            pass

    # 2. Wikidata structured gender + Wikipedia lead for pronoun evidence.
    if not suggestion:
        try:
            gender, wiki_title, entity_url, description = wikidata_lookup(name)
            if wiki_title:
                extract = wikipedia_extract(wiki_title)
                suggestion = _pronouns_from_text(
                    extract, f'https://en.wikipedia.org/wiki/{quote(wiki_title.replace(" ", "_"))}')
            if suggestion:
                # Structured P21 beats pronoun-derived gender if both exist
                if gender:
                    suggestion['gender'] = gender
            elif gender:
                suggestion = {
                    'pronouns': None,
                    'gender': gender,
                    'evidence': f'Wikidata: {description}' if description else 'Wikidata "sex or gender" property',
                    'source_url': entity_url,
                }
        except Exception:
            pass

    # 3. The author's own website bio.
    if not suggestion and website:
        try:
            text = fetch_page(website).get_text(' ', strip=True)
            suggestion = _pronouns_from_text(text, website)
        except Exception:
            pass

    return suggestion
