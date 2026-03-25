"""
anki_jlpt_tagger.py
====================
Reads cards from your Anki Japanese decks via AnkiConnect and enriches them
with JLPT-level and common-word tags sourced from the Jisho.org public API.

Requirements
------------
    pip install requests

Setup
-----
1. Install the AnkiConnect add-on in Anki (code: 2055492159)
   Tools → Add-ons → Get Add-ons → paste code → restart Anki
2. Keep Anki open while running this script.
3. Edit CONFIG below to match your deck name and the field that holds the word.

Tags applied
------------
    jlpt::n1  jlpt::n2  jlpt::n3  jlpt::n4  jlpt::n5   (whichever applies)
    common_word                                            (if flagged by Jisho)

Run
---
    # Preview only – no changes written
    python tagger.py --deck "日本語" --dry-run

    # Live run
    python tagger.py --deck "日本語"

    # Override which field contains the Japanese word
    python tagger.py --deck "日本語" --field "Front"
"""

import argparse
import json
import logging
import re
import sys
import time
from pathlib import Path
from typing import Optional

import requests

# ──────────────────────────────────────────────────────────────────────────────
# CONFIG (edit these defaults)
# ──────────────────────────────────────────────────────────────────────────────
DEFAULT_DECK  = "Japanese"       # Anki deck name (supports * wildcard)
DEFAULT_FIELD = "Front"           # Field name that contains the Japanese word
CACHE_FILE    = Path("jisho_cache.json")   # Avoid re-querying the same words
RATE_LIMIT_S  = 0.4              # Seconds between Jisho requests (be polite)

ANKI_URL  = "http://localhost:8765"
JISHO_URL = "https://jisho.org/api/v1/search/words"

LOG_FORMAT = "%(asctime)s  %(levelname)-8s  %(message)s"
logging.basicConfig(format=LOG_FORMAT, datefmt="%H:%M:%S", level=logging.INFO)
log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# AnkiConnect helpers
# ──────────────────────────────────────────────────────────────────────────────

def anki(action: str, **params) -> object:
    """Send one request to AnkiConnect and return its result."""
    payload = {"action": action, "version": 6, "params": params}
    try:
        resp = requests.post(ANKI_URL, json=payload, timeout=10)
        resp.raise_for_status()
    except requests.exceptions.ConnectionError:
        log.error(
            "Cannot reach AnkiConnect at %s.\n"
            "  • Is Anki open?\n"
            "  • Is the AnkiConnect add-on installed? (code 2055492159)",
            ANKI_URL,
        )
        sys.exit(1)
    body = resp.json()
    if body.get("error"):
        raise RuntimeError(f"AnkiConnect error: {body['error']}")
    return body["result"]


def get_note_ids(deck: str) -> list[int]:
    return anki("findNotes", query=f'deck:"{deck}"')


def get_notes_info(note_ids: list[int]) -> list[dict]:
    return anki("notesInfo", notes=note_ids)


def add_tags(note_id: int, tags: list[str]) -> None:
    anki("addTags", notes=[note_id], tags=" ".join(tags))


def remove_tags(note_id: int, tags: list[str]) -> None:
    anki("removeTags", notes=[note_id], tags=" ".join(tags))


# ──────────────────────────────────────────────────────────────────────────────
# Text helpers
# ──────────────────────────────────────────────────────────────────────────────

_STRIP_HTML   = re.compile(r"<[^>]+>")
_PAREN_SUFFIX = re.compile(r"\s*[（(].+")   # strips （reading） / (reading) and any leading space


def strip_html(text: str) -> str:
    return _STRIP_HTML.sub("", text).strip()


def parse_word_field(raw: str) -> str:
    """
    Return only the kanji/word part of a card field, discarding any
    parenthesised reading and trailing whitespace.

    Examples
    --------
    '面白い（おもしろい）'  → '面白い'
    '軽い(かるい)'         → '軽い'
    '日本語'               → '日本語'
    ''                     → ''
    """
    return _PAREN_SUFFIX.sub("", strip_html(raw)).strip()


# ──────────────────────────────────────────────────────────────────────────────
# Jisho API
# ──────────────────────────────────────────────────────────────────────────────

_JLPT_RE = re.compile(r"jlpt-?(n\d)", re.I)


def normalize_jlpt(raw: str) -> Optional[str]:
    m = _JLPT_RE.search(raw)
    return m.group(1).lower() if m else None


class JishoCache:
    """Persist Jisho look-ups to a local JSON file so re-runs are instant."""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, dict] = {}
        if path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
                log.info("Loaded %d cached entries from %s", len(self.data), path)
            except Exception:
                log.warning("Could not read cache – starting fresh.")

    def get(self, word: str) -> Optional[dict]:
        return self.data.get(word)

    def set(self, word: str, info: dict) -> None:
        self.data[word] = info
        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def query_jisho(word: str) -> dict:
    """
    Look up *word* on Jisho.

    Returns
    -------
    dict with keys:
        jlpt      : 'n1' … 'n5' or None
        is_common : bool
        matched   : str – the japanese[].word or japanese[].reading that
                    triggered the match, or 'fallback:<slug>' when no exact
                    match was found. Useful for debugging.

    Matching priority
    -----------------
    1. Entry where japanese[].word   == word  (exact kanji match)
    2. Entry where japanese[].reading == word  (kana-only words)
    3. First result returned by Jisho (broad fallback)
    """
    try:
        resp = requests.get(JISHO_URL, params={"keyword": word}, timeout=10)
        resp.raise_for_status()
        entries = resp.json().get("data", [])
    except Exception as exc:
        log.warning("Jisho request failed for %r: %s", word, exc)
        return {"jlpt": None, "is_common": False, "matched": "error"}

    if not entries:
        return {"jlpt": None, "is_common": False, "matched": "not_found"}

    def extract(entry: dict, matched_form: str) -> dict:
        raw_jlpt_list = entry.get("jlpt") or []
        
        # 1. Extract all level numbers found (e.g., ["n1", "n5"] -> [1, 5])
        found_levels = []
        for item in raw_jlpt_list:
            level_str = normalize_jlpt(item) # returns 'n1', 'n5', etc.
            if level_str:
                found_levels.append(int(level_str[1])) # gets the '1' or '5'

        # 2. Pick the highest number (N5 is "easier/lower" than N1)
        jlpt = f"n{max(found_levels)}" if found_levels else None
        
        is_common = bool(entry.get("is_common"))
        return {"jlpt": jlpt, "is_common": is_common, "matched": matched_form}

    # Priority 1 – exact kanji match
    for entry in entries:
        for form in entry.get("japanese", []):
            if form.get("word") == word:
                return extract(entry, matched_form=form["word"])

    # Priority 2 – exact reading match (kana-only words like する、ある)
    for entry in entries:
        for form in entry.get("japanese", []):
            if form.get("reading") == word:
                return extract(entry, matched_form=form["reading"])

    # Priority 3 – fallback to first result; log what it actually matched
    first = entries[0]
    first_forms = first.get("japanese", [{}])
    fallback_form = (
        first_forms[0].get("word") or first_forms[0].get("reading") or "?"
    )
    slug = first.get("slug", "?")
    return extract(first, matched_form=f"fallback:{fallback_form}(slug={slug})")


# ──────────────────────────────────────────────────────────────────────────────
# Tag management
# ──────────────────────────────────────────────────────────────────────────────

ALL_JLPT_TAGS = {f"jlpt::n{i}" for i in range(1, 6)}
ALL_MANAGED   = ALL_JLPT_TAGS | {"common_word"}


def compute_new_tags(
    current_tags: list[str], jlpt: Optional[str], is_common: bool
) -> tuple[list[str], list[str]]:
    """Return (tags_to_add, tags_to_remove)."""
    existing = set(current_tags)
    desired: set[str] = set()
    if jlpt:
        desired.add(f"jlpt::{jlpt}")
    if is_common:
        desired.add("common_word")
    return list(desired - existing), list((existing & ALL_MANAGED) - desired)


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def run(deck: str, word_field: str, dry_run: bool) -> None:
    cache = JishoCache(CACHE_FILE)

    log.info("Fetching note IDs from deck: %r", deck)
    note_ids = get_note_ids(deck)
    if not note_ids:
        log.warning("No notes found. Check the deck name.")
        return
    log.info("Found %d notes.", len(note_ids))

    BATCH = 500
    all_notes: list[dict] = []
    for i in range(0, len(note_ids), BATCH):
        all_notes.extend(get_notes_info(note_ids[i : i + BATCH]))

    stats = {"updated": 0, "skipped": 0, "not_found": 0, "errors": 0}

    for idx, note in enumerate(all_notes, 1):
        note_id = note["noteId"]
        fields  = note.get("fields", {})

        # ── resolve the word field ──────────────────────────────────────────
        word_raw = ""
        if word_field in fields:
            word_raw = fields[word_field]["value"]
        else:
            first_key = next(iter(fields), None)
            if first_key:
                word_raw = fields[first_key]["value"]

        word = parse_word_field(word_raw)
        if not word:
            log.debug("Note %d: no word found, skipping.", note_id)
            stats["skipped"] += 1
            continue

        # ── Jisho look-up (cached) ──────────────────────────────────────────
        info = cache.get(word)
        if info is None:
            time.sleep(RATE_LIMIT_S)
            info = query_jisho(word)
            cache.set(word, info)
            freshness = "API"
        else:
            freshness = "cache"

        jlpt      = info["jlpt"]
        is_common = info["is_common"]
        matched   = info.get("matched", "?")

        # ── compute diff ────────────────────────────────────────────────────
        current_tags      = note.get("tags", [])
        to_add, to_remove = compute_new_tags(current_tags, jlpt, is_common)

        status_parts = []
        if jlpt:      status_parts.append(f"jlpt::{jlpt}")
        if is_common: status_parts.append("common_word")
        label = ", ".join(status_parts) if status_parts else "—no JLPT data—"

        log.info(
            "[%d/%d] %-14s →  %-30s  matched=%-20s  (%s)%s",
            idx, len(all_notes),
            word,
            label,
            matched,
            freshness,
            "  [DRY RUN]" if dry_run else "",
        )

        if not to_add and not to_remove:
            stats["skipped"] += 1
            continue

        if not jlpt and not is_common:
            stats["not_found"] += 1

        if not dry_run:
            try:
                if to_add:
                    add_tags(note_id, to_add)
                if to_remove:
                    remove_tags(note_id, to_remove)
                stats["updated"] += 1
            except Exception as exc:
                log.error("Failed to update note %d: %s", note_id, exc)
                stats["errors"] += 1
        else:
            stats["updated"] += 1

    # ── summary ──────────────────────────────────────────────────────────────
    print("\n" + "─" * 60)
    print(f"  Notes processed : {len(all_notes)}")
    print(f"  Tags updated    : {stats['updated']}" + ("  (dry run)" if dry_run else ""))
    print(f"  Already correct : {stats['skipped']}")
    print(f"  Not in Jisho    : {stats['not_found']}")
    print(f"  Errors          : {stats['errors']}")
    print("─" * 60)
    if dry_run:
        print("\n  ⚠  DRY RUN – no changes were written to Anki.")
    else:
        print("\n  ✓  Done. Reopen the Browse window in Anki to see the new tags.")


# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Tag Anki Japanese cards with JLPT level and common-word status."
    )
    parser.add_argument(
        "--deck", default=DEFAULT_DECK,
        help=f'Anki deck name (default: "{DEFAULT_DECK}"). Supports wildcards.',
    )
    parser.add_argument(
        "--field", default=DEFAULT_FIELD,
        help=f'Note field containing the Japanese word (default: "{DEFAULT_FIELD}").',
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview changes without writing anything to Anki.",
    )
    parser.add_argument(
        "--list-decks", action="store_true",
        help="Print all deck names and exit.",
    )
    parser.add_argument(
        "--list-fields", action="store_true",
        help="Print all field names for the first note in --deck and exit.",
    )
    args = parser.parse_args()

    if args.list_decks:
        for d in sorted(anki("deckNames")):
            print(f"  {d}")
        return

    if args.list_fields:
        ids = get_note_ids(args.deck)
        if not ids:
            print("No notes in that deck.")
            return
        info = get_notes_info(ids[:1])[0]
        print(f"\nFields in '{args.deck}':")
        for name in info["fields"]:
            sample = strip_html(info["fields"][name]["value"])[:60]
            print(f"  {name!r:25}  sample: {sample!r}")
        return

    run(deck=args.deck, word_field=args.field, dry_run=args.dry_run)


if __name__ == "__main__":
    main()