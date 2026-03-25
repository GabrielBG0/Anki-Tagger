# Anki JLPT Tagger

Automatically tags your Japanese Anki cards with **JLPT level** and **common word**
status using the [Jisho.org](https://jisho.org) public API.

## Tags applied

| Tag | Meaning |
|---|---|
| `jlpt::n1` – `jlpt::n5` | JLPT level from Jisho |
| `common_word` | Marked as a common word on Jisho |

---

## Setup

### 1. Install AnkiConnect
1. Open Anki → **Tools → Add-ons → Get Add-ons**
2. Enter code **`2055492159`** and click OK
3. Restart Anki
4. AnkiConnect now listens at `http://localhost:8765`

### 2. Install Python dependency

```bash
pip install requests
```

### 3. Configure defaults (optional)

Edit the `CONFIG` block at the top of `tagger.py`:

```python
DEFAULT_DECK  = "Japanese"   # your deck name
DEFAULT_FIELD = "Word"       # the field that holds the Japanese word
```

---

## Usage

```bash
# Discover your deck names
python tagger.py --list-decks

# See which fields a deck uses (helps pick --field)
python tagger.py --deck "日本語" --list-fields

# Dry run — preview changes, nothing written
python tagger.py --deck "日本語" --field "Expression" --dry-run

# Live run
python tagger.py --deck "日本語" --field "Expression"

# Wildcard: tag ALL decks whose names start with "Japanese"
python tagger.py --deck "Japanese*"
```

---

## How it works

1. **AnkiConnect** — the script calls Anki's local REST API to read notes and
   write tags. Anki must be open.
2. **Jisho.org API** (`https://jisho.org/api/v1/search/words?keyword=<word>`) —
   returns `jlpt` array and `is_common` flag for every query.
3. **Local cache** — results are saved to `jisho_cache.json` so re-runs are
   instant and the API is not hammered.

### Re-run safety

The script **replaces** stale JLPT tags instead of accumulating them.
If a card previously had `jlpt::n2` but Jisho now says `n1`, the old tag is
removed and the new one added.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `Cannot reach AnkiConnect` | Make sure Anki is open and the add-on is installed |
| `No notes found` | Run `--list-decks` to see exact deck names |
| Wrong field parsed | Run `--list-fields`, then pass the correct name with `--field` |
| Word not found in Jisho | The card is tagged with nothing; this is expected for rare words |
