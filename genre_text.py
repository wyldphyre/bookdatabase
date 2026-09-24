"""Turning a catalogue's answer into tags this library can use.

Shared by every genre source, because the work is the same wherever the words
came from: decide whether a search result really is the book being asked
about, then beat its genre labels into shape. Hardcover and Google Books both
return BISAC-derived headings in various states of damage, and both need the
same title and author checks.

Kept apart from the adapters so there is one copy of these rules rather than
one per source — a third source should need an API client and nothing else.
"""

import re

# Genre lists are partly ingested from library records, and some entries
# arrive as one BISAC heading that has been split on its commas —
# "Fantasy comic books, strips, etc" becomes three entries, two of which are
# meaningless on their own. Others arrive semicolon-joined in a single string,
# or as a whole BISAC hierarchy: "Comics & Graphic Novels / East Asian Style /
# Manga / General" is four useful-ish levels wearing one unusable label.
#
# The slash is only a separator when it has space around it. Catalogues also
# carry genuine single labels containing one — "FanFic/Trashy" — and
# splitting those would invent two tags out of one.
_FRAGMENT_SEPARATORS = re.compile(r'\s*;\s*|(?<=\s)/|/(?=\s)')

# Some entries carry their own gloss — "LitRPG (Literary Role-Playing Game)" —
# which would otherwise become a second tag alongside the plain "LitRPG" the
# same book also carries.
_PARENTHETICAL = re.compile(r'\s*\([^)]*\)')
_JUNK_FRAGMENTS = {'etc', 'strips', 'general', 'other', 'misc'}
_MIN_GENRE_LENGTH = 3

# Where a source's wording differs from the vocabulary this library already
# uses. Only worth an entry when the existing tag is well established; anything
# not listed here is passed through and may create a new tag, which is what the
# Goodreads path has always done.
#
# LGBTQ is deliberately absent: it used to fold onto the older LGBT tag, but
# the longer form is the more standard one, so the source's wording is kept
# and LGBT is the form being moved away from. Don't re-add it.
_GENRE_ALIASES = {
    'young adult fiction': 'Young Adult',
    'juvenile fiction': 'Middle Grade',
    "children's fiction": 'Middle Grade',
    'comics & graphic novels': 'Graphic Novels',
    'comic books': 'Comics',
    'dystopian': 'Dystopia',
    'action & adventure': 'Adventure',
    # A BISAC qualifier that only means anything next to the level it
    # qualifies; folding it onto that level lets the de-duplication drop it.
    'east asian style': 'Manga',
}


def normalise(text):
    """Lowercased, punctuation-free form used for comparing titles and names.

    'volume' and 'part' are folded to the abbreviations catalogues tend to use
    so that "Paper Girls volume 1" can meet "Paper Girls, Vol. 1".
    """
    text = (text or '').lower()
    text = re.sub(r'\bvolume\b', 'vol', text)
    text = re.sub(r'\bpart\b', 'pt', text)
    return re.sub(r'[^a-z0-9]+', ' ', text).strip()


def significant_words(text):
    return {w for w in normalise(text).split() if len(w) > 2}


def author_matches(known, candidate_names):
    """True when the book's author shares a significant word with any of the
    candidate's contributors. Mirrors the rule the Goodreads search already
    uses, and tolerates 'E. J. Stevens' against 'E.J. Stevens'."""
    wanted = significant_words(known)
    if not wanted:
        return True                      # nothing to check against; title alone decides
    return any(wanted & significant_words(name) for name in candidate_names or [])


def titles_agree(wanted, candidate):
    """Deliberately strict: one title has to contain the other.

    A looser word-overlap rule was tried and recovered two of ten missed
    comics, but matched "Moonstruck Volume 3" to "Moonstruck, Vol. 2" in the
    process. A wrong volume's genres are not worth two extra hits."""
    a, b = normalise(wanted), normalise(candidate)
    return bool(a and b and (a in b or b in a))


def clean_genres(raw):
    """Split compound entries, drop ingestion fragments, de-duplicate."""
    cleaned = []
    seen = set()
    for entry in raw or []:
        for piece in _FRAGMENT_SEPARATORS.split(str(entry)):
            # Punctuation first, then whitespace: the other order leaves
            # 'Fiction ,' as 'Fiction ', which is a second tag as far as a
            # name comparison is concerned but looks identical on screen.
            piece = _PARENTHETICAL.sub('', piece).strip('.,; ').strip()
            if len(piece) < _MIN_GENRE_LENGTH or piece.lower() in _JUNK_FRAGMENTS:
                continue
            name = _GENRE_ALIASES.get(piece.lower(), piece)
            if name.lower() in seen:
                continue
            seen.add(name.lower())
            cleaned.append(name)
    return cleaned
