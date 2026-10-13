#!/usr/bin/env python3
"""
Clash Royale battle log image generator (Stats Royale-style layout).

    pip install requests pillow
    export CR_API_KEY="your_key"

    python cr_battlelog_image.py                      # last battle
    python cr_battlelog_image.py --war --count 3      # last 3 clan war battles only
    python cr_battlelog_image.py --only duel          # last war duel
    python cr_battlelog_image.py --list               # show the battle log (type/mode/time)
    python cr_battlelog_image.py --bg arena.jpg       # custom background image (path or URL)
    python cr_battlelog_image.py --debug --dump       # diagnose art/badge/hero problems

    offline layout tests: --demo | --demo-duel | --demo-emblems

Handles normal battles and clan war duels (one block per round).
Clan emblem = the clan's hexagon badge, frame recoloured by war league
(Bronze = original brown, Silver, Gold, Legendary). League comes from clan war trophies
(see WAR_LEAGUES - the thresholds are an approximation; edit or pass --leagues).
"""
import argparse, glob, io, json, os, re, sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

API_BASE = "https://api.clashroyale.com/v1"
DEFAULT_TAG = "#JG8GGLR9"
CACHE = Path(".card_cache")
FONT_DIR = Path("fonts")

# ---- runtime options (set from CLI) ----------------------------------------
OFFLINE = False
RARITY_FRAMES = False
SHOW_TIER = True
SHOW_EMBLEM = False       # clan badge emblem (off by default; --clan-emblem turns it on)
SHOW_TOWER_TROOP = True
CREDIT = "made with 🫶🏻 by ja"   # small line at the very bottom ("" to hide)
SHOW_CROWNS = False       # crown score in the header duplicates 'TOWERS TAKEN'; --show-crowns brings it back
ASSETS_DIR = None        # folder with official art (Fan Kit / cloned asset repos); see find/asset_img()
CARD_STRETCH = 1.0       # >1 makes cards taller, <1 flatter
BG_PATH, BG_DIM, BG_BLUR = None, 0.40, 0
BADGE_DIR = None         # folder with <badgeId>.png images (manual override)
PLAYS = {}               # optional: {player tag or name: {card name: times played}}
CLAN_CACHE, WARNED = {}, set()
CLAN_LEAGUES = {}        # "#TAG" -> (league, tier)  (explicit, always trusted)
ESTIMATE_LEAGUE = True   # colour the frame from clan war trophies when the league isn't set explicitly

# ------------------------------------------------------------------ layout --
W = 1600
HEAD_H = 190
PANEL_GAP = 60
PANEL_H, IDENT_H, CARDS_OFF, NAME_Y = 672, 242, 232, 56   # set by apply_layout()
DECK_H = 470
BLOCK_GAP = 24
FOOT_H = 84
TR_ROW = 42           # height of the trophy row (removed when nobody has trophies to show)
PL_X, PR_X, PW = 40, 980, 580
CEN_X, CEN_W = 640, 320
CARD_W, CARD_H = 118, 142
CARD_GAP = 24

BLUE_TOP, BLUE_BOT = (36, 128, 245), (14, 84, 200)
RED_TOP, RED_BOT = (222, 72, 58), (170, 36, 34)
DARK = (22, 24, 56)
RARITY = {"common": (92, 100, 118), "rare": (240, 140, 40), "epic": (178, 86, 235),
          "legendary": (110, 225, 255), "champion": (245, 195, 55)}
EVO = (255, 70, 225)
HERO = (255, 205, 50)
GOLD = (252, 196, 45)

# The API gives a clan's war TROPHIES but not its war league, and a clan only changes league
# after a full 4-5 week war season - so the league can't be derived reliably from trophies.
# Set the real ones with --clan-league "#TAG=Legendary II" or in clan_leagues.json
# ({"#TAG": "Legendary II"}). Without that, the frame colour is a rough estimate from this
# UNVERIFIED trophy table and the tier (I/II/III) is NOT shown.
WAR_LEAGUES = [(0, "Bronze", "I"), (200, "Bronze", "II"), (400, "Bronze", "III"),
               (600, "Silver", "I"), (900, "Silver", "II"), (1200, "Silver", "III"),
               (1500, "Gold", "I"), (2000, "Gold", "II"), (3000, "Gold", "III"),
               (4000, "Legendary", "I"), (5000, "Legendary", "II"), (6000, "Legendary", "III")]
# frame colour ramps (dark, mid, light). Bronze keeps the badge's original brown frame.
FRAME_RAMPS = {"Silver": ((58, 66, 88), (168, 178, 200), (246, 249, 255)),
               "Gold": ((158, 110, 10), (238, 172, 28), (255, 244, 160)),
               "Legendary": ((84, 36, 160), (156, 76, 238), (240, 200, 255))}
TIER_PLATE = {"Bronze": (120, 70, 34), "Silver": (86, 96, 120), "Gold": (170, 112, 8),
              "Legendary": (74, 30, 140)}

def apply_layout():
    global PANEL_H, IDENT_H, CARDS_OFF, NAME_Y
    if SHOW_EMBLEM:
        PANEL_H, IDENT_H, CARDS_OFF, NAME_Y = 700, 282, 268, 100
    else:
        PANEL_H, IDENT_H, CARDS_OFF, NAME_Y = 672, 242, 232, 56


BOAT_HEAD, BOAT_PANEL_H = 215, 632

# ------------------------------------------------------------------- fonts --
LILITA = "https://github.com/google/fonts/raw/main/ofl/lilitaone/LilitaOne-Regular.ttf"
FALLBACK_FONTS = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Bold.ttc", "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc", "C:/Windows/Fonts/msyhbd.ttc", "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/malgunbd.ttf", "C:/Windows/Fonts/YuGothB.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf", "/Library/Fonts/Arial Unicode.ttf", "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/seguisym.ttf", "C:/Windows/Fonts/segoeuib.ttf",
]
_main_path = None
_chain_cache, _glyph_cache, _notdef_cache = {}, {}, {}


def warn(msg):
    if msg not in WARNED:
        WARNED.add(msg)
        print("[warn]", msg, file=sys.stderr)


def setup_fonts(custom=None):
    global _main_path
    if custom and os.path.exists(custom):
        _main_path = custom; return
    for p in [FONT_DIR / "LilitaOne-Regular.ttf", Path("Supercell-Magic.ttf")]:
        if p.exists():
            _main_path = str(p); return
    try:
        FONT_DIR.mkdir(exist_ok=True)
        r = requests.get(LILITA, timeout=20); r.raise_for_status()
        (FONT_DIR / "LilitaOne-Regular.ttf").write_bytes(r.content)
        _main_path = str(FONT_DIR / "LilitaOne-Regular.ttf"); return
    except Exception:
        warn("couldn't download Lilita One; using a system font (pass --font for a better one)")


FONT_PRIORITY = ["pingfang", "hiragino", "stheiti", "songti", "arialunicode", "notosanscjk", "notosanssc",
                 "notosansjp", "notosanskr", "notosanstc", "msyh", "simsun", "simhei", "malgun", "yugoth", "meiryo",
                 "applesdgothic", "wqy", "notosansarabic", "notosansthai", "notosanshebrew", "notosansdevanagari",
                 "notosanssymbols", "segoeuisymbol", "seguisym", "segoeui", "dejavusansbold", "dejavusans",
                 "freesansbold", "arialbold", "notosansbold", "notosans"]
CJK_URL = "https://github.com/google/fonts/raw/main/ofl/notosanssc/NotoSansSC%5Bwght%5D.ttf"
_font_scan = None
_cjk_tried = False


def discover_fonts():
    """Find installed fonts that cover other scripts (CJK, Arabic, symbols...), most useful first."""
    global _font_scan
    if _font_scan is not None:
        return _font_scan
    home = Path.home()
    dirs = ["/System/Library/Fonts", "/System/Library/Fonts/Supplemental", "/Library/Fonts", str(home / "Library/Fonts"),
            "C:/Windows/Fonts", "/usr/share/fonts", "/usr/local/share/fonts", str(home / ".fonts"),
            str(home / ".local/share/fonts")]
    found = []

    def consider(path):
        stem = re.sub(r"[^a-z0-9]", "", Path(path).stem.lower())
        if any(b in stem for b in ("emoji", "italic", "oblique", "light", "thin", "mono", "condensed", "narrow",
                                   "black", "extra")):
            return
        for i, pat in enumerate(FONT_PRIORITY):
            if pat in stem:
                found.append((i, 0 if "bold" in stem else 1, path)); return

    for base in dirs:
        if os.path.isdir(base):
            for root, _, files in os.walk(base):
                for fn in files:
                    if fn.lower().endswith((".ttf", ".ttc", ".otf")):
                        consider(os.path.join(root, fn))
    for pat in ("/System/Library/AssetsV2/**/PingFang*.ttc",):      # newer macOS keeps PingFang here
        for pth in glob.glob(pat, recursive=True):
            consider(pth)
    found.sort()
    out, seen = [], set()
    for _, _, pth in found:
        if pth not in seen:
            seen.add(pth); out.append(pth)
    _font_scan = out[:30]
    return _font_scan


def try_download_cjk():
    """Last resort when no installed font has a CJK character (needs internet, ~10 MB, once)."""
    global _cjk_tried
    if _cjk_tried or OFFLINE:
        return False
    _cjk_tried = True
    try:
        FONT_DIR.mkdir(exist_ok=True)
        fn = FONT_DIR / "NotoSansSC.ttf"
        if not fn.exists():
            r = requests.get(CJK_URL, timeout=90); r.raise_for_status()
            fn.write_bytes(r.content)
        _chain_cache.clear()
        return True
    except Exception:
        warn("a player name has CJK characters but no installed font covers them (download of Noto Sans SC failed); "
             "install a CJK font or put NotoSansSC.ttf in ./fonts")
        return False


def chain(size):
    """Main font first, then fallbacks (CJK, symbols...). Each char uses the first font that has it."""
    if size not in _chain_cache:
        fonts, seen = [], set()
        extra = [str(q) for q in FONT_DIR.glob("NotoSansSC*.ttf")] if FONT_DIR.exists() else []
        for pth in [_main_path] + discover_fonts() + FALLBACK_FONTS + extra:
            if pth and pth not in seen and os.path.exists(pth):
                seen.add(pth)
                try:
                    fonts.append(ImageFont.truetype(pth, size))
                except Exception:
                    pass
        _chain_cache[size] = fonts or [ImageFont.load_default(size)]
    return _chain_cache[size]


def font(size):
    return chain(size)[0]


def _sig(f, ch):
    im = Image.new("L", (f.size * 3, f.size * 3))
    ImageDraw.Draw(im).text((4, 4), ch, font=f, fill=255)
    return im.tobytes()


def has_glyph(f, ch):
    if ch.isspace() or ord(ch) < 128:
        return True
    key = (getattr(f, "path", id(f)), f.size, ch)
    if key not in _glyph_cache:
        try:
            nk = (key[0], f.size)
            if nk not in _notdef_cache:
                _notdef_cache[nk] = _sig(f, "\U0010FFFF")
            _glyph_cache[key] = _sig(f, ch) != _notdef_cache[nk]
        except Exception:
            _glyph_cache[key] = True
    return _glyph_cache[key]


# Emoji: matched as whole sequences (ZWJ families, flags, skin tones, keycaps) and drawn as COLOUR images
# from a local colour-emoji font when there is one, else Twemoji PNGs (cached). No image -> drawn as text/skipped.
_PICT = r"[\U0001F000-\U0001FAFF]"
_DEF = (r"[\u231A\u231B\u23E9-\u23EC\u23F0\u23F3\u25FD\u25FE\u2614\u2615\u2648-\u2653\u267F\u2693\u26A1\u26AA\u26AB"
        r"\u26BD\u26BE\u26C4\u26C5\u26CE\u26D4\u26EA\u26F2\u26F3\u26F5\u26FA\u26FD\u2705\u270A\u270B\u2728\u274C\u274E"
        r"\u2753-\u2755\u2757\u2795-\u2797\u27B0\u27BF\u2B1B\u2B1C\u2B50\u2B55]")
_SYM = (r"[\u00A9\u00AE\u203C\u2049\u2122\u2139\u2194-\u21AA\u231A-\u23FF\u24C2\u25AA-\u25FE\u2600-\u27BF\u2934\u2935"
        r"\u2B05-\u2B55\u3030\u303D\u3297\u3299]")
_MOD = r"[\U0001F3FB-\U0001F3FF]"
_ELEM = r"(?:" + _PICT + "|" + _DEF + "|" + _SYM + r"\uFE0F)\uFE0F?" + _MOD + "?"
EMOJI_RE = re.compile(r"[\U0001F1E6-\U0001F1FF]{2}|[#*0-9]\uFE0F?\u20E3|" + _ELEM +
                      r"(?:\u200D(?:" + _PICT + "|" + _SYM + r")\uFE0F?" + _MOD + "?)*")
_EMOJI_FONTS = ["/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf", "/usr/share/fonts/noto/NotoColorEmoji.ttf",
                "/usr/share/fonts/google-noto-emoji/NotoColorEmoji.ttf", "/System/Library/Fonts/Apple Color Emoji.ttc",
                "C:/Windows/Fonts/seguiemj.ttf"]
TWEMOJI_BASES = ["https://cdn.jsdelivr.net/gh/twitter/twemoji@14.0.2/assets/72x72/",
                 "https://cdn.jsdelivr.net/gh/jdecked/twemoji@15.1.0/assets/72x72/"]
_emoji_font_obj = "unset"
_emoji_cache, _emoji_scaled_cache = {}, {}


def _get_emoji_font():
    global _emoji_font_obj
    if _emoji_font_obj == "unset":
        _emoji_font_obj = None
        for pth in [str(FONT_DIR / "NotoColorEmoji.ttf")] + _EMOJI_FONTS:
            if not os.path.exists(pth):
                continue
            for fs in (109, 160, 128, 96, 72):          # bitmap emoji fonts only allow certain sizes
                try:
                    f = ImageFont.truetype(pth, fs)
                    im = Image.new("RGBA", (fs * 3, fs * 2), (0, 0, 0, 0))
                    ImageDraw.Draw(im).text((8, 8), "\U0001F600", font=f, embedded_color=True)
                    if im.getbbox():
                        _emoji_font_obj = f
                        break
                except Exception:
                    continue
            if _emoji_font_obj:
                break
    return _emoji_font_obj


def _emoji_from_font(seq):
    f = _get_emoji_font()
    if f is None:
        return None
    fs = f.size

    def render(t):
        im = Image.new("RGBA", (fs * 4, fs * 2), (0, 0, 0, 0))
        ImageDraw.Draw(im).text((10, 10), t, font=f, embedded_color=True)
        return im
    try:
        im = render(seq)
        bb = im.getbbox()
        if not bb or im.tobytes() == render("\U0010FFFF").tobytes():     # empty or .notdef -> unsupported
            return None
        return im.crop(bb)
    except Exception:
        return None


def _emoji_from_twemoji(seq):
    if OFFLINE:
        return None
    cps = [f"{ord(c):x}" for c in seq]
    names = ["-".join(c for c in cps if not (c == "fe0f" and "200d" not in cps)), "-".join(cps)]
    for base in TWEMOJI_BASES:
        for nm in dict.fromkeys(names):
            img = fetch_url(base + nm + ".png", quiet=True)
            if img is not None:
                return img
    return None


def emoji_scaled(seq, size):
    key = (seq, size)
    if key not in _emoji_scaled_cache:
        if seq not in _emoji_cache:
            _emoji_cache[seq] = _emoji_from_font(seq) or _emoji_from_twemoji(seq)
        base = _emoji_cache[seq]
        if base is None:
            _emoji_scaled_cache[key] = None
        else:
            k = size * 1.05 / max(base.size)
            _emoji_scaled_cache[key] = base.resize((max(1, int(base.width * k)), max(1, int(base.height * k))),
                                                   Image.LANCZOS)
    return _emoji_scaled_cache[key]


def clean(s):
    return (s or "").strip()


def paste_rgba(canvas, im, x, y):
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(canvas.width, x + im.width), min(canvas.height, y + im.height)
    if x1 <= x0 or y1 <= y0:
        return
    canvas.alpha_composite(im.crop((x0 - x, y0 - y, x1 - x, y1 - y)), (x0, y0))


def layout(s, size):
    """Items are ('c', char, x, font) or ('e', emoji_image, x). Per-character font fallback; characters no font
    has are dropped (never drawn as boxes)."""
    items, state = [], {"x": 0.0, "prev": None, "pf": None}

    def add_char(ch):
        fonts = chain(size)
        f = next((f for f in fonts if has_glyph(f, ch)), None)
        if f is None and ord(ch) >= 0x2E80 and try_download_cjk():
            f = next((f for f in chain(size) if has_glyph(f, ch)), None)
        if f is None:
            return
        if state["prev"] is not None and state["pf"] is f:
            state["x"] += f.getlength(state["prev"] + ch) - f.getlength(state["prev"]) - f.getlength(ch)
        items.append(("c", ch, state["x"], f))
        state["x"] += f.getlength(ch)
        state["prev"], state["pf"] = ch, f

    pos = 0
    for m in EMOJI_RE.finditer(s):
        for ch in s[pos:m.start()]:
            add_char(ch)
        em = emoji_scaled(m.group(), size)
        if em is not None:
            items.append(("e", em, state["x"]))
            state["x"] += em.width + size * 0.06
            state["prev"] = state["pf"] = None
        else:
            for ch in m.group():
                add_char(ch)
        pos = m.end()
    for ch in s[pos:]:
        add_char(ch)
    return items, state["x"]


def text_width(s, size):
    return layout(clean(s), size)[1]


def fit(s, max_w, size, min_size=18):
    while size > min_size and text_width(s, size) > max_w:
        size -= 2
    return size


def text(d, xy, s, size, fill=(255, 255, 255), anchor="mm", stroke=None, stroke_fill=DARK, **_):
    s = clean(s)
    items, w = layout(s, size)
    if not items:
        return
    x, y = xy
    x0 = x if anchor[0] == "l" else (x - w / 2 if anchor[0] == "m" else x - w)
    asc, desc = chain(size)[0].getmetrics()
    base = y + (asc - desc) / 2
    sw = max(2, size // 12) if stroke is None else stroke
    chars = [it for it in items if it[0] == "c"]
    if sw > 0:
        for _k, ch, ox, f in chars:
            d.text((x0 + ox, base), ch, font=f, fill=stroke_fill, anchor="ls", stroke_width=sw, stroke_fill=stroke_fill)
    for _k, ch, ox, f in chars:
        d.text((x0 + ox, base), ch, font=f, fill=fill, anchor="ls")
    img = getattr(d, "_image", None)
    if img is not None:
        for it in items:
            if it[0] == "e":
                paste_rgba(img, it[1], int(x0 + it[2]), int(base - 0.34 * size - it[1].height / 2))


# --------------------------------------------------------------------- API --
def api_get(path, key, soft=False):
    r = requests.get(API_BASE + path, headers={"Authorization": f"Bearer {key}"}, timeout=20)
    if soft:
        return r.json() if r.status_code == 200 else None
    if r.status_code == 403:
        sys.exit("403: invalid API key, or this machine's IP isn't whitelisted for the key.")
    if r.status_code == 404:
        sys.exit("404: player not found.")
    r.raise_for_status()
    return r.json()


def enc(tag):
    return requests.utils.quote("#" + tag.strip().lstrip("#").upper(), safe="")


def norm(name):
    n = re.sub(r"[^a-z0-9]", "", (name or "").lower())
    return n[4:] if n.startswith("hero") and len(n) > 4 else n


def get_cards_meta(key):
    CACHE.mkdir(exist_ok=True)
    f = CACHE / "cards.json"
    try:
        data = api_get("/cards", key)
        f.write_text(json.dumps(data))
    except SystemExit:
        raise
    except Exception:
        data = json.loads(f.read_text()) if f.exists() else {}
    items = list(data.get("items", [])) + list(data.get("supportItems", []))
    return {"id": {c["id"]: c for c in items}, "name": {norm(c["name"]): c for c in items}}


def enrich_cards(battle, meta):
    def fix(c):
        m = meta["id"].get(c.get("id")) or meta["name"].get(norm(c.get("name"))) or {}
        if c.get("elixirCost") is None and m.get("elixirCost"):
            c["elixirCost"] = m["elixirCost"]
        if not c.get("rarity") and m.get("rarity"):
            c["rarity"] = m["rarity"]
        icons = dict(m.get("iconUrls") or {})   # e.g. heroMedium missing from the battle-log card
        icons.update(c.get("iconUrls") or {})
        c["iconUrls"] = icons
    for side in ("team", "opponent"):
        for p in battle.get(side, []):
            for c in p.get("cards", []):
                fix(c)
            for r in p.get("rounds", []) or []:
                for c in r.get("cards", []):
                    fix(c)


def enrich_clans(battle, key):
    """Attach clan war trophies + badge info (needed for the league-coloured emblem)."""
    for side in ("team", "opponent"):
        for p in battle.get(side, []):
            clan = p.get("clan") or {}
            tag = clan.get("tag")
            if not tag or "clanWarTrophies" in clan:
                continue
            if tag not in CLAN_CACHE:
                try:
                    CLAN_CACHE[tag] = api_get(f"/clans/{enc(tag)}", key, soft=True) or {}
                except Exception:
                    CLAN_CACHE[tag] = {}
            info = CLAN_CACHE[tag]
            urls = info.get("badgeUrls") or clan.get("badgeUrls") or {}
            clan["badgeUrl"] = urls.get("large") or urls.get("medium") or urls.get("small")
            clan["badgeId"] = clan.get("badgeId") or info.get("badgeId")
            clan["clanWarTrophies"] = info.get("clanWarTrophies")


# -------------------------------------------------------------- image fetch --
FETCH_ERRORS = {}


def fetch_url(url, quiet=False):
    if not url or OFFLINE:
        return None
    CACHE.mkdir(exist_ok=True)
    fn = CACHE / re.sub(r"[^A-Za-z0-9._-]", "_", url.split("//", 1)[-1])
    try:
        if fn.exists():
            return Image.open(fn).convert("RGBA")
        r = requests.get(url, timeout=20); r.raise_for_status()
        fn.write_bytes(r.content)
        return Image.open(io.BytesIO(r.content)).convert("RGBA")
    except Exception as e:
        FETCH_ERRORS[url] = str(e)
        if not quiet:
            warn(f"download failed: {url} ({e})")
        return None


def card_mode(card):
    """'normal' | 'evo' | 'hero'. In the battle log `evolutionLevel` is a BIT FIELD meaning "played as this
    mode in this battle" (1 = Evo, 2 = Hero) - so the mode comes from it, not from which icons exist."""
    lvl = card.get("evolutionLevel", 0) or 0
    if lvl & 2 or card.get("heroLevel") or (card.get("name") or "").lower().startswith("hero "):
        return "hero"
    return "evo" if lvl & 1 else "normal"


def card_variant(card):
    mode = card_mode(card)
    icons = card.get("iconUrls") or {}
    key = {"hero": "heroMedium", "evo": "evolutionMedium"}.get(mode)
    return mode, (icons.get(key) if key else None) or icons.get("medium")


# ---- local official art: Supercell Fan Kit downloads, a clone of a community asset repo, your own PNGs ----
# Point --assets-dir (or create ./assets) at any folder; files are found by NAME, recursively:
#   icons : names containing  crown+blue, crown+red, elixir (or drop), trophy, cycle, boat
#   cards : "<Card Name>.png" / "<Card Name> Hero.png" / "<Card Name> Evo.png"  (any case, any separators)
_asset_index = None
_asset_img_cache = {}


def _assets():
    global _asset_index
    if _asset_index is None:
        _asset_index = []
        if ASSETS_DIR and os.path.isdir(ASSETS_DIR):
            for root, _, files in os.walk(ASSETS_DIR):
                for fn in files:
                    if fn.lower().endswith((".png", ".webp", ".jpg", ".jpeg")):
                        _asset_index.append((re.sub(r"[^a-z0-9]", "", Path(fn).stem.lower()), os.path.join(root, fn)))
    return _asset_index


def _load_asset(path):
    if path not in _asset_img_cache:
        try:
            _asset_img_cache[path] = crop_content(Image.open(path).convert("RGBA"))
        except Exception:
            _asset_img_cache[path] = None
    return _asset_img_cache[path]


def asset_img(*keywords, exclude=()):
    for stem, path in _assets():
        if all(k in stem for k in keywords) and not any(e in stem for e in exclude):
            img = _load_asset(path)
            if img is not None:
                return img
    return None


def local_card_art(card, mode):
    want = norm(card.get("name"))
    for stem, path in _assets():
        has_h, has_e = "hero" in stem, ("evo" in stem or "evolution" in stem)
        base = re.sub(r"hero|evolution|evo|card|medium|icon|\d+", "", stem)
        if base != want:
            continue
        if (mode == "hero" and has_h) or (mode == "evo" and has_e) or (mode == "normal" and not has_h and not has_e):
            img = _load_asset(path)
            if img is not None:
                return img
    return None


def card_art(card, plain=False):
    """(image|None, mode, exact). exact=False: only normal art was found for an evo/hero card.
    plain=True ignores evo/hero (used for boat-defense lists, whose evolutionLevel means 'owned')."""
    mode = "normal" if plain else card_mode(card)
    icons = card.get("iconUrls") or {}
    key = {"hero": "heroMedium", "evo": "evolutionMedium"}.get(mode)
    if OFFLINE:
        return None, mode, (key is None or bool(icons.get(key)))
    url = icons.get(key) if key else None
    if key:
        img = fetch_url(url, quiet=True) if url else None
        if img is None:
            img = local_card_art(card, mode)
        if img is not None:
            return img, mode, True
    for u in [icons.get("medium")] + [v for v in icons.values() if isinstance(v, str)]:
        img = fetch_url(u, quiet=True)
        if img is not None:
            if key:
                why = (f"the API lists a {key} image but it failed to download ({FETCH_ERRORS.get(url, 'unknown error')})"
                       if url else f"the API has no {key} for it")
                warn(f"{mode.upper()} art for '{card.get('name')}' unavailable: {why}. Showing the normal art with a "
                     f"{mode} marker - it will switch to the real art once it exists. Or put '{card.get('name')} "
                     f"{mode.title()}.png' in --assets-dir.")
            return img, mode, key is None
    img = local_card_art(card, "normal")
    if img is not None:
        return img, mode, key is None
    warn(f"no art for card '{card.get('name')}' (id {card.get('id')}, icons: {list(icons) or 'none'}) - "
         f"run with --dump and send me that JSON")
    return None, mode, False


def crop_content(img):
    """Trim transparent margins so the art fills its slot (no floating / squashed cards)."""
    bbox = img.split()[3].point(lambda v: 255 if v > 110 else 0).getbbox()
    return img.crop(bbox) if bbox else img


def placeholder(name=""):
    im = Image.new("RGBA", (302, 363), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([16, 60, 286, 330], 36, fill=(52, 58, 92, 255), outline=(120, 128, 160, 255), width=14)
    d.text((151, 195), (name or "?")[:2], font=font(100), fill=(230, 235, 255), anchor="mm")
    return im


def display_level(c):
    return c.get("level", 1) + (16 - c.get("maxLevel", 16))


# ---------------------------------------------------------- AA shape helper --
def aa(canvas, box, painter, k=4):
    x0, y0, x1, y1 = [int(v) for v in box]
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(canvas.width, x1), min(canvas.height, y1)
    w, h = x1 - x0, y1 - y0
    if w <= 0 or h <= 0:
        return
    layer = Image.new("RGBA", (w * k, h * k), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    T = lambda pts: [((x - x0) * k, (y - y0) * k) for x, y in pts]
    painter(d, T, k)
    canvas.alpha_composite(layer.resize((w, h), Image.LANCZOS), (x0, y0))


def rrect(canvas, box, r, fill=None, outline=None, width=0):
    x0, y0, x1, y1 = box
    def p(d, T, k):
        (a, b), (c, e) = T([(x0, y0), (x1, y1)])
        d.rounded_rectangle([a, b, c, e], r * k, fill=fill, outline=outline, width=width * k)
    aa(canvas, (x0 - 2, y0 - 2, x1 + 2, y1 + 2), p)


def vgrad(w, h, top, bot):
    mask = Image.linear_gradient("L").resize((w, h))
    return Image.composite(Image.new("RGB", (w, h), bot), Image.new("RGB", (w, h), top), mask)


def _silhouette_layer(img, pad, radius, color, strength=None, opacity=None):
    mask = Image.new("L", (img.width + 2 * pad, img.height + 2 * pad), 0)
    mask.paste(img.split()[3], (pad, pad))
    mask = mask.filter(ImageFilter.GaussianBlur(radius))
    if strength:
        mask = mask.point(lambda v: min(255, int(v * strength)))
    if opacity:
        mask = mask.point(lambda v: int(v * opacity))
    layer = Image.new("RGBA", mask.size, color + (0,))
    layer.putalpha(mask)
    return layer


def alpha_glow(canvas, img, x, y, color, radius=8, strength=2.0):
    pad = radius * 3
    canvas.alpha_composite(_silhouette_layer(img, pad, radius, color, strength=strength), (int(x - pad), int(y - pad)))


def alpha_shadow(canvas, img, x, y, dy=6, radius=5, opacity=0.5):
    pad = radius * 3
    canvas.alpha_composite(_silhouette_layer(img, pad, radius, (0, 0, 0), opacity=opacity),
                           (int(x - pad), int(y - pad + dy)))


# ------------------------------------------------------------------- icons --
def _asset_icon(canvas, keyword_sets, cx, cy, height):
    for kw in keyword_sets:
        img = asset_img(*kw)
        if img is not None:
            img = img.resize((max(1, int(img.width * height / img.height)), int(height)), Image.LANCZOS)
            x, y = int(cx - img.width / 2), int(cy - img.height / 2)
            alpha_shadow(canvas, img, x, y, dy=3, radius=3, opacity=0.4)
            paste_rgba(canvas, img, x, y)
            return True
    return False


def crown(canvas, cx, cy, color, s=1.0):
    blue = color[2] > color[0]
    if _asset_icon(canvas, [("crown", "blue" if blue else "red"), ("crown",)], cx, cy, 72 * s):
        return
    band, band_hi = ((46, 128, 246), (140, 196, 255)) if blue else ((238, 60, 124), (255, 160, 196))
    gold, gold_hi, outline = (255, 192, 46), (255, 232, 128), (96, 52, 10)
    sil = [(-36, 20), (-42, -22), (-17, 2), (0, -32), (17, 2), (42, -22), (36, 20)]
    top = [(-31, 8), (-36, -14), (-15, 4), (0, -24), (15, 4), (36, -14), (31, 8)]

    def p(d, T, k):
        P = lambda pts: T([(cx + x * s, cy + y * s) for x, y in pts])
        d.polygon(P(sil), fill=gold + (255,))
        d.polygon(P(top), fill=gold_hi + (255,))
        d.line(P(sil + [sil[0]]), fill=outline + (255,), width=int(2.4 * s * k), joint="curve")
        (a, b), (c, e) = T([(cx - 36 * s, cy + 12 * s), (cx + 36 * s, cy + 30 * s)])
        d.rounded_rectangle([a, b, c, e], 5 * k, fill=band + (255,), outline=outline + (255,), width=int(2.4 * s * k))
        (a, b), (c, e) = T([(cx - 31 * s, cy + 15 * s), (cx + 31 * s, cy + 20 * s)])
        d.rectangle([a, b, c, e], fill=band_hi + (255,))
        d.polygon(P([(0, 14), (9, 21), (0, 29), (-9, 21)]), fill=(255, 255, 255, 255), outline=outline + (255,))
        for gx, gy in ((-42, -22), (0, -32), (42, -22)):
            (qx, qy), = P([(gx, gy)])
            d.ellipse([qx - 6 * s * k, qy - 6 * s * k, qx + 6 * s * k, qy + 6 * s * k], fill=band_hi + (255,),
                      outline=outline + (255,), width=int(1.6 * s * k))
    aa(canvas, (cx - 56 * s, cy - 46 * s, cx + 56 * s, cy + 40 * s), p)


def drop(canvas, cx, cy, s=1.0):
    if _asset_icon(canvas, [("elixir",), ("drop",)], cx, cy, 46 * s):
        return

    def p(d, T, k):
        (a, b), = T([(cx, cy + 3 * s)])
        r = 15 * s * k
        d.ellipse([a - r, b - r, a + r, b + r], fill=(222, 50, 200), outline=(90, 20, 90), width=k)
        d.polygon(T([(cx - 13 * s, cy - 4 * s), (cx, cy - 26 * s), (cx + 13 * s, cy - 4 * s)]),
                  fill=(222, 50, 200))
        d.line(T([(cx - 13 * s, cy - 4 * s), (cx, cy - 26 * s), (cx + 13 * s, cy - 4 * s)]),
               fill=(90, 20, 90), width=k)
        (e, f), = T([(cx - 6 * s, cy + 1 * s)])
        d.ellipse([e - 3 * k, f - 5 * k, e + 3 * k, f + 5 * k], fill=(255, 190, 240))
    aa(canvas, (cx - 25 * s, cy - 35 * s, cx + 25 * s, cy + 25 * s), p)


def cycle_icon(canvas, cx, cy, s=1.0):
    if _asset_icon(canvas, [("cycle",), ("recycle",)], cx, cy, 44 * s):
        return

    def p(d, T, k):
        (a, b), = T([(cx, cy)])
        r = 15 * s * k
        d.arc([a - r, b - r, a + r, b + r], 20, 170, fill=(110, 205, 255), width=int(6 * k))
        d.arc([a - r, b - r, a + r, b + r], 200, 350, fill=(110, 205, 255), width=int(6 * k))
        d.polygon(T([(cx + 18 * s, cy - 3 * s), (cx + 4 * s, cy - 4 * s), (cx + 15 * s, cy - 16 * s)]),
                  fill=(110, 205, 255))
        d.polygon(T([(cx - 18 * s, cy + 3 * s), (cx - 4 * s, cy + 4 * s), (cx - 15 * s, cy + 16 * s)]),
                  fill=(110, 205, 255))
    aa(canvas, (cx - 28 * s, cy - 28 * s, cx + 28 * s, cy + 28 * s), p)


def trophy(canvas, cx, cy, s=1.0):
    if _asset_icon(canvas, [("trophy",)], cx, cy, 42 * s):
        return

    def p(d, T, k):
        o = (120, 70, 10)
        d.polygon(T([(cx - 13 * s, cy - 14 * s), (cx + 13 * s, cy - 14 * s), (cx + 9 * s, cy + 4 * s),
                     (cx - 9 * s, cy + 4 * s)]), fill=GOLD, outline=o)
        (a, b), (c, e) = T([(cx - 3 * s, cy + 4 * s), (cx + 3 * s, cy + 11 * s)])
        d.rectangle([a, b, c, e], fill=GOLD)
        (a, b), (c, e) = T([(cx - 9 * s, cy + 10 * s), (cx + 9 * s, cy + 16 * s)])
        d.rectangle([a, b, c, e], fill=GOLD, outline=o)
        for sx in (-1, 1):
            (a, b), = T([(cx + sx * 15 * s, cy - 6 * s)])
            d.arc([a - 6 * k * s, b - 7 * k * s, a + 6 * k * s, b + 7 * k * s], 0, 360, fill=GOLD,
                  width=int(3 * k))
    aa(canvas, (cx - 26 * s, cy - 24 * s, cx + 26 * s, cy + 24 * s), p)


def check_icon(canvas, cx, cy, s=1.0):
    def p(d, T, k):
        (a, b), = T([(cx, cy)])
        r = 15 * s * k
        d.ellipse([a - r, b - r, a + r, b + r], fill=(52, 190, 104), outline=(16, 90, 44), width=k)
        d.line(T([(cx - 7 * s, cy), (cx - 2 * s, cy + 6 * s), (cx + 8 * s, cy - 6 * s)]),
               fill=(255, 255, 255), width=int(4 * k), joint="curve")
    aa(canvas, (cx - 24 * s, cy - 24 * s, cx + 24 * s, cy + 24 * s), p)


# ------------------------------------------------------------ clan emblem --
def norm_tag(t):
    return "#" + str(t).strip().lstrip("#").upper() if t else None


def parse_league(txt):
    m = re.match(r"\s*(bronze|silver|gold|legendary)\s*(iii|ii|i|[1-3])?\s*$", txt, re.I)
    if not m:
        raise ValueError(f"can't read league '{txt}' (use e.g. 'Legendary II')")
    tier = {"1": "I", "2": "II", "3": "III"}.get((m.group(2) or "").lower(), (m.group(2) or "").upper()) or None
    return m.group(1).capitalize(), tier


def clan_league(clan):
    """(league, tier, known). tier only when set explicitly."""
    tag = norm_tag(clan.get("tag"))
    if tag in CLAN_LEAGUES:
        name, tier = CLAN_LEAGUES[tag]
        return name, tier, True
    if not ESTIMATE_LEAGUE:
        return None
    est = league_for(clan.get("clanWarTrophies"))
    if est:
        warn(f"war league of '{clan.get('name')}' ({tag}) is only ESTIMATED from {clan.get('clanWarTrophies'):,} war "
             f"trophies (tier hidden). Set the real one: --clan-league \"{tag}=Legendary II\" or clan_leagues.json")
        return est[0], None, False
    return None


def league_for(trophies):
    if trophies is None:
        return None
    cur = None
    for mn, name, tier in WAR_LEAGUES:
        if trophies >= mn:
            cur = (name, tier)
    return cur


def default_badge(name=""):
    """Generic hexagon clan badge (bronze frame) used when the real badge image is unavailable."""
    S = 2
    w, h = 256 * S, 290 * S
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    base = [(0.5, 0.0), (1.0, 0.2), (1.0, 0.8), (0.5, 1.0), (0.0, 0.8), (0.0, 0.2)]
    def hexpts(sc, dy=0.0):
        return [(w / 2 + (x - 0.5) * w * sc, h / 2 + (y - 0.5) * h * sc + dy * h) for x, y in base]
    d.polygon(hexpts(1.0), fill=(58, 32, 18, 255))
    d.polygon(hexpts(0.95), fill=(150, 94, 54, 255))
    d.polygon(hexpts(0.95, -0.004), fill=(176, 118, 70, 255))
    d.polygon(hexpts(0.86), fill=(104, 62, 36, 255))
    d.polygon(hexpts(0.76), fill=(38, 92, 206, 255))
    d.polygon([hexpts(0.76)[i] for i in (0, 1, 5)], fill=(60, 120, 232, 255))
    crown(im, w // 2, int(h * 0.50), (255, 226, 110), s=2.3 * S)
    return im.resize((w // S, h // S), Image.LANCZOS)


def recolor_frame(img, league_name):
    """Re-tint just the badge's outer frame ring with the league colour (keeps the bevel shading)."""
    ramp = FRAME_RAMPS.get(league_name)
    if not ramp:
        return img
    w, h = img.size
    a = img.split()[3]
    k = max(3, int(min(w, h) * 0.125))
    pad = k + 2   # pad first: the badge touches the image border, which would stop the erosion there
    eroded = ImageOps.expand(a, pad, fill=0).filter(ImageFilter.MinFilter(2 * k + 1)).crop((pad, pad, pad + w, pad + h))
    ring = ImageChops.subtract(a, eroded)
    ring = ring.filter(ImageFilter.GaussianBlur(1.0)).point(lambda v: min(255, int(v * 1.6)))
    gray = ImageOps.grayscale(img.convert("RGB")).point(lambda v: min(255, int(v * 1.7)))
    dark, mid, light = ramp
    tint = ImageOps.colorize(gray, black=dark, mid=mid, white=light, blackpoint=20, midpoint=120, whitepoint=235)
    tint = tint.convert("RGBA")
    tint.putalpha(a)
    return Image.composite(tint, img, ring)


def resolve_badge(clan):
    bid = clan.get("badgeId")
    if BADGE_DIR and bid is not None:
        for ext in ("png", "webp", "jpg"):
            p = Path(BADGE_DIR) / f"{bid}.{ext}"
            if p.exists():
                return Image.open(p).convert("RGBA")
    img = fetch_url(clan.get("badgeUrl"), quiet=True)
    if img is None and not OFFLINE:
        warn(f"no badge image for clan '{clan.get('name')}' (badgeId {bid}); using a generic hexagon. "
             f"Drop {bid}.png into a folder and pass --badge-dir to use the real one.")
    return img


def draw_emblem(canvas, cx, cy, height, clan):
    """The clan's hexagon badge; the frame colour shows the war league (Bronze/Silver/Gold/Legendary)."""
    league = clan_league(clan)
    badge = resolve_badge(clan) or default_badge(clan.get("name", ""))
    badge = crop_content(badge)
    badge = badge.resize((max(1, int(height * badge.width / badge.height)), height), Image.LANCZOS)
    if league:
        badge = recolor_frame(badge, league[0])
    x, y = int(cx - badge.width / 2), int(cy - badge.height / 2)
    if league and league[0] in ("Legendary", "Gold"):
        alpha_glow(canvas, badge, x, y, FRAME_RAMPS[league[0]][1], radius=9, strength=1.1)
    alpha_shadow(canvas, badge, x, y, dy=6, radius=5, opacity=0.55)
    canvas.alpha_composite(badge, (x, y))
    if league and SHOW_TIER and league[2] and league[1]:
        label = league[1]
        pw = 20 + 11 * len(label)
        px, py = int(cx - pw / 2), int(y + badge.height - 14)
        rrect(canvas, (px, py, px + pw, py + 24), 8, fill=TIER_PLATE[league[0]] + (255,),
              outline=(255, 255, 255, 190), width=2)
        text(ImageDraw.Draw(canvas), (cx, py + 13), label, 17, stroke=2)


# -------------------------------------------------------------------- cards --
def plays_for(player, card):
    """Times played, or None when unknown (official API has no per-card play counts)."""
    d = PLAYS.get(player.get("tag")) or PLAYS.get(player.get("name"))
    return None if d is None else int(d.get(card.get("name"), 0))


def draw_gem(canvas, cx, y, color, inner):
    def p(d, T, k):
        d.polygon(T([(cx - 20, y + 8), (cx + 20, y + 8), (cx + 14, y - 12), (cx - 14, y - 12)]),
                  fill=color + (255,), outline=(255, 255, 255, 255))
        d.polygon(T([(cx, y - 10), (cx + 10, y - 2), (cx, y + 6), (cx - 10, y - 2)]),
                  fill=inner + (255,), outline=(255, 255, 255, 255))
    aa(canvas, (cx - 30, y - 24, cx + 30, y + 16), p)


def draw_card(canvas, card, x, y, plays=None, w=None, h=None, plain=False, small=False):
    w, h = w or CARD_W, h or CARD_H
    img, variant, exact = card_art(card, plain)
    img = crop_content(img or placeholder(card.get("name")))
    scale = w / img.width                            # same scale for every card (uniform, no squashing)
    nw, nh = w, int(img.height * scale * CARD_STRETCH)
    room = h + 26 * h / CARD_H
    if nh > room:
        s2 = room / nh
        nw, nh = int(nw * s2), int(nh * s2)
    img = img.resize((nw, nh), Image.LANCZOS)
    ax, ay = x + (w - nw) // 2, y + (h - nh) // 2

    unplayed = plays == 0 or card.get("used") is False   # duel rounds tell us which cards were used
    if unplayed:  # black & white for cards that were never played
        g = ImageOps.grayscale(img.convert("RGB")).point(lambda v: int(v * 0.8))
        img = Image.merge("RGBA", (g, g, g, img.split()[3]))
    elif variant == "evo":
        alpha_glow(canvas, img, ax, ay, EVO)
    elif variant == "hero":
        alpha_glow(canvas, img, ax, ay, HERO)
    alpha_shadow(canvas, img, ax, ay)
    if RARITY_FRAMES:
        col = (110, 112, 122) if unplayed else RARITY.get((card.get("rarity") or "common").lower(), RARITY["common"])
        rrect(canvas, (ax, ay, ax + nw, ay + nh), 13, outline=col + (255,), width=4)
    canvas.alpha_composite(img, (ax, ay))
    if variant != "normal" and not exact:    # no real evo/hero art available: mark the card ourselves
        col, inner = (HERO, (255, 185, 20)) if variant == "hero" else (EVO, (140, 55, 215))
        if unplayed:
            col, inner = (130, 130, 140), (96, 98, 108)
        draw_gem(canvas, ax + nw // 2, ay - 2, col, inner)

    d = ImageDraw.Draw(canvas)
    bw, bh, fs = (28, 32, 20) if small else (34, 38, 26)
    bx, by = ax - 8, ay - 8
    rrect(canvas, (bx, by, bx + bw, by + bh), 8, fill=GOLD + (255,), outline=(60, 35, 5, 255), width=3)
    if plays is None:
        text(d, (bx + bw / 2, by + bh / 2 + 1), str(display_level(card)), fs, stroke=3, stroke_fill=(70, 40, 5))
    else:  # Stats Royale style: badge = times played, level chip bottom-right
        text(d, (bx + bw / 2, by + bh / 2 + 1), str(plays), fs, stroke=3, stroke_fill=(70, 40, 5))
        lx, ly = ax + nw - 40, ay + nh - 24
        rrect(canvas, (lx, ly, lx + 44, ly + 28), 8, fill=(15, 18, 40, 220))
        text(ImageDraw.Draw(canvas), (lx + 22, ly + 15), f"L{display_level(card)}", 18, stroke=2)


# ----------------------------------------------------------------- panels ---
def avg_elixir(cards):
    c = [k["elixirCost"] for k in cards if k.get("elixirCost")]
    return (sum(c) / len(c)) if c else None


def cycle_cost(cards):
    c = sorted(k["elixirCost"] for k in cards if k.get("elixirCost"))
    return sum(c[:4]) if len(c) >= 4 else None


def panel_bg(canvas, x, top, h, is_blue):
    c1, c2 = (BLUE_TOP, BLUE_BOT) if is_blue else (RED_TOP, RED_BOT)
    panel = vgrad(PW, h, c1, c2).convert("RGBA")
    m = Image.new("L", (PW * 4, h * 4), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, PW * 4 - 1, h * 4 - 1], 36 * 4, fill=255)
    canvas.paste(panel, (x, top), m.resize((PW, h), Image.LANCZOS))
    rrect(canvas, (x, top, x + PW, top + h), 36, outline=(255, 255, 255, 70), width=3)


def draw_tower_troop(canvas, player, x, top):
    st = (player.get("supportCards") or [None])[0]
    if not st:
        return
    icons = st.get("iconUrls") or {}
    img = crop_content(fetch_url(icons.get("medium"), quiet=True) or placeholder(st.get("name")))
    w = 72
    img = img.resize((w, int(img.height * w / img.width)), Image.LANCZOS)
    ax, ay = x + 26, top + 22
    alpha_shadow(canvas, img, ax, ay, dy=4, radius=4, opacity=0.5)
    canvas.alpha_composite(img, (ax, ay))
    rrect(canvas, (ax + w - 30, ay + img.height - 20, ax + w + 6, ay + img.height + 4), 7,
          fill=GOLD + (255,), outline=(60, 35, 5, 255), width=2)
    d = ImageDraw.Draw(canvas)
    text(d, (ax + w - 12, ay + img.height - 8), str(display_level(st)), 17, stroke=2, stroke_fill=(70, 40, 5))
    nm = clean(st.get("name", ""))
    text(d, (ax + w / 2, ay + img.height + 20), nm, fit(nm, 110, 15, 11), fill=(225, 232, 250), stroke=2)


def draw_identity(canvas, player, x, top, is_blue):
    cx = x + PW // 2
    clan = player.get("clan") or {}
    clan_name = clean(clan.get("name", ""))
    if SHOW_EMBLEM and (clan.get("tag") or clan_name):
        draw_emblem(canvas, cx, top + 4, 118, clan)
    troop = SHOW_TOWER_TROOP and bool(player.get("supportCards"))
    if troop:
        draw_tower_troop(canvas, player, x, top)
    d = ImageDraw.Draw(canvas)
    has_tr = player.get("startingTrophies") is not None or player.get("trophyChange")

    y = top + NAME_Y
    name = clean(player.get("name", "?")) or "?"
    text(d, (cx, y), name, fit(name, PW - (240 if troop else 70), 54, 22), stroke=5)
    tagline = str(player.get("tag") or "").upper()
    if player.get("globalRank"):
        tagline += f"  ·  GLOBAL #{player['globalRank']:,}"
    if tagline.strip():
        y += 44
        text(d, (cx, y), tagline.strip(), 22, fill=(205, 218, 245), stroke=2)
    if clan_name:
        y += 34
        text(d, (cx, y), clan_name, fit(clan_name, PW - (260 if troop else 120), 30, 18), fill=(225, 232, 250), stroke=3)
    if has_tr:
        y += 42
        tr, ch = player.get("startingTrophies"), player.get("trophyChange")
        label = f"{tr:,}" if tr is not None else ""
        extra = f"{ch:+d}" if ch else ""
        w1 = text_width(label, 36) if label else 0
        w2 = text_width(extra, 30) if extra else 0
        total = 36 + w1 + (14 if label and extra else 0) + w2
        sx = cx - total / 2
        trophy(canvas, int(sx + 16), y, 1.1)
        d = ImageDraw.Draw(canvas)
        if label:
            text(d, (sx + 40, y), label, 36, anchor="lm")
        if extra:
            col = (120, 255, 140) if ch > 0 else (255, 150, 150)
            text(d, (sx + 40 + w1 + (14 if label else 0), y + 2), extra, 30, fill=col, anchor="lm")


def draw_deck(canvas, player, cards, x, top, cards_off, panel_h):
    cx = x + PW // 2
    cards = cards[:8]
    x0 = x + (PW - (4 * CARD_W + 3 * CARD_GAP)) // 2
    for i, card in enumerate(cards):
        draw_card(canvas, card, x0 + (i % 4) * (CARD_W + CARD_GAP),
                  top + cards_off + (i // 4) * (CARD_H + 36), plays_for(player, card))
    by = top + panel_h - 62
    a, cyc = avg_elixir(cards), cycle_cost(cards)
    items = [("drop", f"{a:.1f}" if a else "-", "AVG ELIXIR"), ("cycle", str(cyc) if cyc else "-", "4-CARD CYCLE")]
    if any("used" in c for c in cards):          # duel rounds: the API says which cards were actually played
        items.append(("check", f"{sum(1 for c in cards if c.get('used'))}/{len(cards)}", "CARDS PLAYED"))
    centers = [cx - 100, cx + 100] if len(items) == 2 else [cx - 190, cx, cx + 190]
    for (kind, val, label), ic in zip(items, centers):
        {"drop": lambda: drop(canvas, ic - 34, by, 1.15), "cycle": lambda: cycle_icon(canvas, ic - 34, by, 1.2),
         "check": lambda: check_icon(canvas, ic - 34, by, 1.2)}[kind]()
        d = ImageDraw.Draw(canvas)
        text(d, (ic - 8, by - 3), val, 38, anchor="lm")
        text(d, (ic, by + 36), label, 17, fill=(225, 235, 255), stroke=2)


# ------------------------------------------------------------------- recap --
def fmt(v):
    if v is None:
        return "-"
    return f"{v:,.2f}" if isinstance(v, float) and v % 1 else f"{int(round(v)):,}"


def side_stats(players):
    def hp(p):
        vals = [p.get("kingTowerHitPoints")] + list(p.get("princessTowersHitPoints") or [])
        vals = [v for v in vals if v is not None]
        return sum(vals) if vals else None
    def tot(f):
        v = [f(p) for p in players if f(p) is not None]
        return sum(v) if v else None
    return {"crowns": tot(lambda p: p.get("crowns")),
            "leak": tot(lambda p: p.get("elixirLeaked")),
            "king": tot(lambda p: p.get("kingTowerHitPoints")),
            "prin": tot(lambda p: sum(p["princessTowersHitPoints"])
                        if p.get("princessTowersHitPoints") is not None else None),
            "hp": tot(hp)}


def draw_recap(canvas, team, opp, top, height, title="GAME RECAP"):
    rrect(canvas, (CEN_X, top, CEN_X + CEN_W, top + height), 30, fill=(250, 251, 255, 255))
    big = height >= 560
    title_h = 100 if big else 66
    d = ImageDraw.Draw(canvas)
    text(d, (CEN_X + CEN_W // 2, top + title_h * 0.5), title, 38 if big else 32, fill=(30, 34, 70), stroke=0)
    a, b = side_stats(team), side_stats(opp)
    rows = [("TOWERS TAKEN", "crowns"), ("ELIXIR LEAKED", "leak"), ("KING TOWER HP", "king"),
            ("PRINCESS TOWERS HP", "prin"), ("FINAL HITPOINTS", "hp")]
    step = (height - title_h - 14) / len(rows)
    L, R = CEN_X + 24, CEN_X + CEN_W - 24
    base = 40 if big else 32
    for i, (label, k) in enumerate(rows):
        y = top + title_h + i * step
        d = ImageDraw.Draw(canvas)
        text(d, (CEN_X + CEN_W // 2, y + step * 0.13), label, 21 if big else 17, fill=(95, 102, 140), stroke=0)
        text(d, (L, y + step * 0.50), fmt(a[k]), base if len(fmt(a[k])) < 7 else base - 8,
             fill=(28, 80, 190), anchor="lm", stroke=0)
        text(d, (R, y + step * 0.50), fmt(b[k]), base if len(fmt(b[k])) < 7 else base - 8,
             fill=(200, 50, 40), anchor="rm", stroke=0)
        va, vb = a[k] or 0, b[k] or 0
        tot_w, gap = R - L, 8
        frac = 0.5 if va + vb == 0 else va / (va + vb)
        wa = (tot_w - gap) * frac
        by0 = y + step * 0.77
        by1 = by0 + (10 if big else 8)
        if wa > 1:
            rrect(canvas, (L, by0, L + wa, by1), 5, fill=(36, 118, 235, 255))
        if (tot_w - gap) - wa > 1:
            rrect(canvas, (L + wa + gap, by0, R, by1), 5, fill=(215, 60, 50, 255))


# -------------------------------------------------------------- background --
def load_bg_image(path):
    try:
        if re.match(r"https?://", path):
            r = requests.get(path, timeout=25); r.raise_for_status()
            return Image.open(io.BytesIO(r.content)).convert("RGB")
        return Image.open(path).convert("RGB")
    except Exception as e:
        warn(f"couldn't load --bg '{path}' ({e}); using the default background")
        return None


def background(w, h):
    img = load_bg_image(BG_PATH) if BG_PATH else None
    if img is not None:
        bg = ImageOps.fit(img, (w, h), Image.LANCZOS).convert("RGBA")   # cover + centre crop
        if BG_BLUR:
            bg = bg.filter(ImageFilter.GaussianBlur(BG_BLUR))
        # blue (left) -> red (right) tint keeps the two-team feel and panel readability
        grad = Image.linear_gradient("L").rotate(90, expand=True).resize((w, h))
        tint = Image.composite(Image.new("RGBA", (w, h), (205, 30, 40, 120)),
                               Image.new("RGBA", (w, h), (30, 60, 220, 120)), grad)
        bg.alpha_composite(tint)
        bg.alpha_composite(Image.new("RGBA", (w, h), (8, 8, 24, int(255 * BG_DIM))))
        return bg
    bg = vgrad(w, h, (16, 20, 52), (44, 22, 74)).convert("RGBA")
    for cx, col in ((0, (40, 110, 255)), (w, (255, 60, 60)), (w // 2, (150, 60, 220))):
        layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        ImageDraw.Draw(layer).ellipse([cx - 520, -260, cx + 520, 620], fill=col + (90,))
        bg.alpha_composite(layer.filter(ImageFilter.GaussianBlur(140)))
    st = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    sd = ImageDraw.Draw(st)
    for i in range(-h, w, 46):
        sd.line([(i, 0), (i + h, h)], fill=(255, 255, 255, 8), width=14)
    bg.alpha_composite(st)
    return bg


def fmt_time(bt, tz):
    dt = datetime.strptime(bt, "%Y%m%dT%H%M%S.%fZ").replace(tzinfo=timezone.utc)
    if ZoneInfo:
        dt = dt.astimezone(ZoneInfo(tz))
    return dt.strftime("%m/%d/%Y, %I:%M %p ") + (dt.tzname() or "UTC")


def draw_header(canvas, res, col, left, right, sub=None, score=True):
    """score=False: just the result (+ subtitle). Crown icons only with --show-crowns; the crown count is the same
    number as 'TOWERS TAKEN' in the recap, so it isn't repeated by default."""
    d = ImageDraw.Draw(canvas)
    text(d, (W // 2, 52), res, 64, fill=col, stroke=6)
    if not score:
        if sub:
            text(d, (W // 2, 106), sub, 22, fill=(225, 230, 245), stroke=2)
        return
    if SHOW_CROWNS:
        crown(canvas, W // 2 - 140, 128, (70, 160, 255), 1.0)
        crown(canvas, W // 2 + 140, 128, (255, 90, 130), 1.0)
        y, sy = 128, 172
    else:
        y, sy = 122, 164
    d = ImageDraw.Draw(canvas)
    text(d, (W // 2 - 60, y), str(left), 66, stroke=6)
    text(d, (W // 2, y), "-", 60, stroke=5)
    text(d, (W // 2 + 60, y), str(right), 66, stroke=6)
    if sub:
        text(d, (W // 2, sy), sub, 20, fill=(215, 220, 240), stroke=2)


def period_label(battle):
    """'Training Day 2' / 'Battle Day 3' / 'Colosseum Day 1' - only when the caller attached battle['_period']
    ({'type': periodType, 'index': periodIndex} from the clan's /currentriverrace). The battle log itself has no such
    field. Per the API docs: periodIndex % 7 is 0-2 on training days and 3-6 on battle days 1-4."""
    p = battle.get("_period")
    if not p or p.get("index") is None or not p.get("type"):
        return None
    day = int(p["index"]) % 7
    t = str(p["type"]).lower()
    if t == "training":
        return f"Training Day {day + 1}"
    if t == "colosseum":
        return f"Colosseum Day {max(day - 2, 1)}"
    if t == "warday":
        return f"Battle Day {max(day - 2, 1)}"
    return None


def header_sub(battle):
    parts = [x for x in ((period_label(battle) or "").upper(), ranked_sub(battle) or special_sub(battle)) if x]
    return "  ·  ".join(parts) or None


DECK_SELECTION = {"draft": "Draft", "draftCompetitive": "Triple Draft", "pick": "Pick mode",
                  "predefined": "Preset decks", "eventDeck": "Event deck", "quadDeckPick": "4-deck duel"}
STANDARD_MODES = {"ladder", "ranked1v1newarena", "ranked1v1newarena2", "cwbattle1v1", "cwduel1v1",
                  "clanwarboatbattle", "friendly", "teamvsteam", "tournament"}


def pretty_mode(name):
    mode = re.sub(r"_?New ?Arena\d*", "", name or "").replace("_", " ")
    return re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[a-uw-z])(?=\d)", " ", mode).strip()   # never split "1v1"


def special_sub(battle):
    """Header subtitle naming a rule variant (Triple Elixir, Sudden Death, 7x Elixir ...)."""
    raw = (battle.get("gameMode") or {}).get("name", "")
    if not raw or re.sub(r"[^a-z0-9]", "", raw.lower()) in STANDARD_MODES:
        return None
    return pretty_mode(raw).upper()


def mode_label(battle, n):
    t = str(battle.get("type", "")).lower()
    if t == "boatbattle":
        return " · ".join(x for x in ("Clan War", "Boat Battle", period_label(battle)) if x)
    mode = pretty_mode((battle.get("gameMode") or {}).get("name", ""))
    parts = [mode or "Battle"]
    if is_war(battle) and not re.sub(r"[^a-z]", "", mode.lower()).startswith(("cw", "clanwar")):
        # special rules inside a clan war (Triple Elixir, Sudden Death, ...): keep the CW part
        kind = "Duel" if "duel" in t else "Battle"
        parts = [f"CW {kind} {n}v{n}"] + ([mode] if mode else [])
    elif f"{n}v{n}" not in mode.replace(" ", "").lower():
        parts.insert(0, f"{n}v{n}")
    if DECK_SELECTION.get(battle.get("deckSelection")):
        parts.append(DECK_SELECTION[battle["deckSelection"]])
    if not is_war(battle):                      # the arena is meaningless for clan war battles
        parts.append((battle.get("arena") or {}).get("name", ""))
    if battle.get("tournamentTag"):
        parts.append(f"Tournament {battle['tournamentTag']}")
    if period_label(battle):
        parts.append(period_label(battle))
    return " · ".join(x for x in parts if x)


def draw_footer(canvas, battle, H, tz, n):
    """mode label (left) | credit (centre) | time (right), one row, one size. If they cannot fit side by side even
    at the smallest size (very long mode names) the credit moves to a second row."""
    d = ImageDraw.Draw(canvas)
    left, right = mode_label(battle, n), fmt_time(battle["battleTime"], tz)
    col = (225, 230, 245)
    for size in (28, 26, 24, 22, 20):
        wl, wr = text_width(left, size), text_width(right, size)
        wc = text_width(CREDIT, size) if CREDIT else 0
        if not CREDIT or (50 + wl + 30 <= W / 2 - wc / 2 and W / 2 + wc / 2 + 30 <= W - 50 - wr):
            y = H - FOOT_H / 2
            text(d, (50, y), left, size, fill=col, anchor="lm", stroke=2)
            text(d, (W - 50, y), right, size, fill=col, anchor="rm", stroke=2)
            if CREDIT:
                text(d, (W // 2, y), CREDIT, size, fill=col, stroke=2)
            return
    size = 24                                                        # fallback: two rows, same size
    text(d, (50, H - 58), left, size, fill=col, anchor="lm", stroke=2)
    text(d, (W - 50, H - 58), right, size, fill=col, anchor="rm", stroke=2)
    text(d, (W // 2, H - 24), CREDIT, size, fill=col, stroke=2)


# --------------------------------------------------------------- rounds ----
def get_rounds(player):
    """Duel rounds: API `rounds` list, else split a >8 card list into decks of 8."""
    rounds = player.get("rounds") or []
    if rounds:
        return rounds
    cards = player.get("cards", [])
    if len(cards) > 8:
        return [{"cards": cards[i:i + 8]} for i in range(0, len(cards), 8) if len(cards[i:i + 8]) == 8]
    return []


def round_view(player, r):
    base = {k: player.get(k) for k in ("name", "tag", "clan", "startingTrophies", "trophyChange",
                                       "supportCards", "globalRank")}
    base.update({k: r.get(k) for k in ("crowns", "kingTowerHitPoints", "princessTowersHitPoints", "elixirLeaked")})
    base["cards"] = r.get("cards", [])
    return base


def hp_total(p):
    v = [p.get("kingTowerHitPoints")] + list(p.get("princessTowersHitPoints") or [])
    return sum(x for x in v if x is not None)


def fix_king_hp(players, opp_players):
    """Per the API docs: a destroyed king tower leaves kingTowerHitPoints out, and princessTowersHitPoints is
    null when no princess tower survives. Both mean 0, not 'unknown'."""
    oc = max([(o.get("crowns") or 0) for o in opp_players] or [0])
    for p in players:
        if p.get("kingTowerHitPoints") is None and oc >= 3:
            p["kingTowerHitPoints"] = 0
        if p.get("princessTowersHitPoints") is None and (p.get("kingTowerHitPoints") is not None or oc >= 2):
            p["princessTowersHitPoints"] = []


def round_winner(tv, ov):
    tc, oc = tv.get("crowns"), ov.get("crowns")
    if tc is None or oc is None:
        return 0
    if tc != oc:
        return 1 if tc > oc else -1
    ht, ho = hp_total(tv), hp_total(ov)
    return 0 if ht == ho else (1 if ht > ho else -1)


def battle_outcome(b):
    """Winner per the API docs' order: boatBattleWon -> sign of trophyChange -> crowns -> unresolved."""
    if "boatBattleWon" in b:
        return 1 if b["boatBattleWon"] else -1
    team, opp = (b.get("team") or [{}]), (b.get("opponent") or [{}])
    tc = team[0].get("trophyChange")
    if tc:
        return 1 if tc > 0 else -1
    a, c = team[0].get("crowns"), opp[0].get("crowns")
    if a is not None and c is not None:
        return (a > c) - (a < c)
    return 0


def result_style(win, lose):
    if win > lose:
        return "WIN", (120, 255, 140)
    if win < lose:
        return "LOSE", (255, 110, 110)
    return "DRAW", (220, 225, 240)


# ------------------------------------------------------------------ render --
def render_battle(battle, out_path, tz="America/New_York", debug=False):
    team, opp = battle.get("team", []), battle.get("opponent", [])
    if not team or not opp or not (team[0].get("cards") or team[0].get("rounds")):
        print(f"  skipped ({battle.get('type')}): no deck data in this battle"); return False
    battle["_outcome"] = battle_outcome(battle)     # decide first: prep_display() may hide trophyChange
    prep_display(battle)
    if str(battle.get("type", "")).lower() == "boatbattle":
        render_boat(battle, out_path, tz, debug)
        return True
    n = max(len(team), len(opp), 1)
    rt = get_rounds(team[0]) if len(team) == 1 else []
    ro = get_rounds(opp[0]) if len(opp) == 1 else []
    rc = min(len(rt), len(ro))
    if rc >= 1 and (str(battle.get("type", "")).lower().startswith("riverraceduel") or rc > 1):
        render_duel(battle, team[0], opp[0], rt[:rc], ro[:rc], out_path, tz, debug)
    else:
        render_single(battle, team, opp, n, out_path, tz, debug)
    return True


def render_single(battle, team, opp, n, out_path, tz, debug):
    sub = header_sub(battle)
    head = HEAD_H if SHOW_CROWNS else (128 if sub else 100)
    if SHOW_EMBLEM:
        head = max(head, HEAD_H)
    has_tr = any(q.get("startingTrophies") is not None or q.get("trophyChange") for q in team[:n] + opp[:n])
    poff = 0 if has_tr else TR_ROW                 # no trophy row: tighten instead of leaving a gap
    ph, co = PANEL_H - poff, CARDS_OFF - poff
    body = n * ph + (n - 1) * PANEL_GAP
    H = head + body + FOOT_H
    canvas = background(W, H)
    fix_king_hp(team, opp); fix_king_hp(opp, team)
    tc = team[0].get("crowns", 0) or 0
    oc = opp[0].get("crowns", 0) or 0
    res, col = result_style(battle.get("_outcome", 0), 0)
    draw_header(canvas, res, col, tc, oc, sub=sub, score=SHOW_CROWNS)
    for i in range(n):
        top = head + i * (ph + PANEL_GAP)
        for players, x, blue in ((team, PL_X, True), (opp, PR_X, False)):
            if i < len(players):
                p = players[i]
                panel_bg(canvas, x, top, ph, blue)
                draw_identity(canvas, p, x, top, blue)
                draw_deck(canvas, p, p.get("cards", []), x, top, co, ph)
                if debug:
                    debug_print(p, p.get("cards", []))
    draw_recap(canvas, team, opp, head, body)
    draw_footer(canvas, battle, H, tz, n)
    canvas.convert("RGB").save(out_path, quality=95)


def render_duel(battle, tp, op, rt, ro, out_path, tz, debug):
    rc = len(rt)
    has_tr = any(q.get("startingTrophies") is not None or q.get("trophyChange") for q in (tp, op))
    ih = IDENT_H if has_tr else IDENT_H - TR_ROW
    per = period_label(battle)
    head = HEAD_H if SHOW_CROWNS else (128 if per else 100)       # no score row -> compact header
    if SHOW_EMBLEM:
        head = max(head, HEAD_H)
    y0 = head + ih + BLOCK_GAP
    H = y0 + rc * (DECK_H + BLOCK_GAP) - BLOCK_GAP + FOOT_H
    canvas = background(W, H)

    views = [(round_view(tp, a), round_view(op, b)) for a, b in zip(rt, ro)]
    for a, b in views:
        fix_king_hp([a], [b]); fix_king_hp([b], [a])
    winners = [round_winner(a, b) for a, b in views]
    tw, ow = winners.count(1), winners.count(-1)
    if tw == ow == 0:
        tw, ow = tp.get("crowns", 0) or 0, op.get("crowns", 0) or 0
    tcrowns = sum(v[0].get("crowns") or 0 for v in views) or tp.get("crowns") or 0
    ocrowns = sum(v[1].get("crowns") or 0 for v in views) or op.get("crowns") or 0
    res, col = result_style(tw, ow)
    if SHOW_CROWNS:
        sub = ("ROUNDS WON" + f"  ·  CROWNS {tcrowns} - {ocrowns}" + (f"  ·  {per.upper()}" if per else ""))
        draw_header(canvas, res, col, tw, ow, sub=sub)
    else:
        draw_header(canvas, res, col, tw, ow, sub=(per.upper() if per else None), score=False)

    for x, p, blue in ((PL_X, tp, True), (PR_X, op, False)):
        panel_bg(canvas, x, head, ih, blue)
        draw_identity(canvas, p, x, head, blue)

    rrect(canvas, (CEN_X, head, CEN_X + CEN_W, head + ih), 30, fill=(250, 251, 255, 255))
    d = ImageDraw.Draw(canvas)
    text(d, (CEN_X + CEN_W // 2, head + 36), "DUEL", 38, fill=(30, 34, 70), stroke=0)
    rs = min(rc, 3)
    step = 58
    y_start = head + 74 + ((ih - 74 - 24) - (rs - 1) * step) / 2
    for i in range(rs):
        a, b = views[i]
        y = y_start + i * step
        text(d, (CEN_X + 30, y), f"R{i + 1}", 24, fill=(95, 102, 140), anchor="lm", stroke=0)
        cl = (28, 80, 190) if winners[i] >= 0 else (150, 160, 190)
        cr = (200, 50, 40) if winners[i] <= 0 else (190, 170, 170)
        mx = CEN_X + CEN_W // 2 - 8
        text(d, (mx - 14, y), fmt(a.get("crowns")), 34, fill=cl, anchor="rm", stroke=0)
        text(d, (mx, y), "-", 34, fill=(120, 126, 160), stroke=0)
        text(d, (mx + 14, y), fmt(b.get("crowns")), 34, fill=cr, anchor="lm", stroke=0)
        letter, colr = (("W", (40, 170, 80)) if winners[i] > 0 else ("L", (210, 60, 50)) if winners[i] < 0
                        else ("D", (130, 136, 160)))
        rrect(canvas, (CEN_X + CEN_W - 62, y - 15, CEN_X + CEN_W - 30, y + 15), 8, fill=colr + (255,))
        text(ImageDraw.Draw(canvas), (CEN_X + CEN_W - 46, y), letter, 20, stroke=0)

    for i, (a, b) in enumerate(views):
        top = y0 + i * (DECK_H + BLOCK_GAP)
        for x, p, blue in ((PL_X, a, True), (PR_X, b, False)):
            panel_bg(canvas, x, top, DECK_H, blue)
            draw_deck(canvas, p, p.get("cards", []), x, top, 34, DECK_H)
            if debug:
                print(f"Round {i + 1}"); debug_print(p, p.get("cards", []))
        draw_recap(canvas, [a], [b], top, DECK_H, title=f"ROUND {i + 1}")
    draw_footer(canvas, battle, H, tz, 1)
    canvas.convert("RGB").save(out_path, quality=95)


# ---------------------------------------------------------------- boat battle ---
def hex_pts(cx, cy, r):
    return [(cx, cy - r), (cx + .866 * r, cy - .5 * r), (cx + .866 * r, cy + .5 * r), (cx, cy + r),
            (cx - .866 * r, cy + .5 * r), (cx - .866 * r, cy - .5 * r)]


def hex_badge(canvas, cx, cy, r, kind, number):
    """Tower-shaped hexagon badge: 'wood' (towers at start) / 'red' (destroyed) / 'steel' (remaining)."""
    pal = {"wood": ((60, 62, 76), (196, 202, 214), (180, 110, 52)),
           "red": ((92, 10, 10), (226, 120, 110), (200, 38, 38)),
           "steel": ((40, 46, 66), (170, 180, 205), (92, 104, 140))}[kind]

    def p(d, T, k):
        d.polygon(T(hex_pts(cx, cy + 5, r)), fill=(0, 0, 0, 90))
        d.polygon(T(hex_pts(cx, cy, r)), fill=pal[0] + (255,))
        d.polygon(T(hex_pts(cx, cy, r * 0.94)), fill=pal[1] + (255,))
        d.polygon(T(hex_pts(cx, cy, r * 0.80)), fill=pal[2] + (255,))
        if kind == "wood":                    # planks
            r2 = r * 0.80
            for dx in (-0.46, 0.0, 0.46):
                x = dx * r2
                lim = r2 - abs(x) * 0.577 - 3
                d.line(T([(cx + x, cy - lim), (cx + x, cy + lim)]), fill=(120, 70, 30, 255), width=int(3 * k))
    aa(canvas, (cx - r - 8, cy - r - 8, cx + r + 8, cy + r + 14), p)
    d = ImageDraw.Draw(canvas)
    text(d, (cx, cy + 2), str(number), int(r * 1.1), fill=(255, 214, 70) if kind == "wood" else (255, 255, 255),
         stroke=max(3, int(r / 10)),
         stroke_fill={"wood": (70, 30, 8), "red": (60, 10, 10), "steel": (20, 26, 52)}[kind])


def boat_icon(canvas, cx, cy, s=1.0):
    if _asset_icon(canvas, [("boat",), ("clanboat",)], cx, cy, 78 * s):
        return
    hex_badge(canvas, cx, cy, 36 * s, "wood", "")

    def p(d, T, k):                              # white anchor on the wooden badge
        col = (245, 245, 250, 255)
        d.line(T([(cx, cy - 14 * s), (cx, cy + 16 * s)]), fill=col, width=int(4 * s * k))
        d.line(T([(cx - 9 * s, cy - 6 * s), (cx + 9 * s, cy - 6 * s)]), fill=col, width=int(4 * s * k))
        (a, b), = T([(cx, cy - 18 * s)])
        d.ellipse([a - 4.5 * s * k, b - 4.5 * s * k, a + 4.5 * s * k, b + 4.5 * s * k], outline=col, width=int(3 * s * k))
        (a, b), (c, e) = T([(cx - 14 * s, cy - 4 * s), (cx + 14 * s, cy + 24 * s)])
        d.arc([a, b, c, e], 20, 160, fill=col, width=int(4 * s * k))
    aa(canvas, (cx - 40 * s, cy - 40 * s, cx + 40 * s, cy + 40 * s), p)


def role_pill(canvas, cx, cy, label, fill):
    w = 24 + 15 * len(label)
    rrect(canvas, (cx - w / 2, cy - 15, cx + w / 2, cy + 15), 15, fill=fill + (255,), outline=(255, 255, 255, 190), width=2)
    text(ImageDraw.Draw(canvas), (cx, cy + 1), label, 19, stroke=2)


def draw_defenses(canvas, cards, x, top, panel_h):
    """Boat-defense list (usually 12 cards, plain art: evolutionLevel there means 'owned', not 'played as')."""
    cards = cards[:12]
    w, h, gap, cols = 82, 98, 12, 6
    rows = (len(cards) + cols - 1) // cols
    total_h = rows * h + (rows - 1) * 32
    y0 = top + 200 + ((panel_h - 70 - 200) - total_h) / 2
    text(ImageDraw.Draw(canvas), (x + PW // 2, y0 - 26), "BOAT DEFENSES", 20, fill=(225, 235, 255), stroke=2)
    x0 = x + (PW - (cols * w + (cols - 1) * gap)) // 2
    for i, c in enumerate(cards):
        draw_card(canvas, c, int(x0 + (i % cols) * (w + gap)), int(y0 + (i // cols) * (h + 32)), w=w, h=h,
                  plain=True, small=True)


def render_boat(battle, out_path, tz, debug):
    me, op = battle["team"][0], battle["opponent"][0]
    side = str(battle.get("boatBattleSide", "")).lower()
    if side == "attacker":
        left, right, you_left = op, me, False
    else:
        left, right, you_left = me, op, True
    lrole, rrole = ("DEFENDER", "ATTACKER") if side in ("attacker", "defender") else (None, None)
    if side == "attacker":
        lrole, rrole = "DEFENDER", "ATTACKER"
    H = BOAT_HEAD + BOAT_PANEL_H + FOOT_H
    canvas = background(W, H)
    res, col = result_style(battle.get("_outcome", 0), 0)
    d = ImageDraw.Draw(canvas)
    text(d, (W // 2, 50), res, 64, fill=col, stroke=6)
    boat_icon(canvas, W // 2, 126, 1.0)
    per = period_label(battle)
    text(ImageDraw.Draw(canvas), (W // 2, 182), "BOAT BATTLE" + (f"  ·  {per.upper()}" if per else ""), 28,
         fill=(225, 230, 245), stroke=3)

    for x, pl, role, blue, you in ((PL_X, left, lrole, True, you_left), (PR_X, right, rrole, False, not you_left)):
        panel_bg(canvas, x, BOAT_HEAD, BOAT_PANEL_H, blue)
        if role:
            role_pill(canvas, x + PW // 2, BOAT_HEAD + 28, role, (40, 70, 160) if blue else (160, 40, 40))
        if you:
            role_pill(canvas, x + PW - 52, BOAT_HEAD + 28, "YOU", (30, 30, 50))
        fix_king_hp([pl], [])
        draw_identity(canvas, pl, x, BOAT_HEAD + 22, blue)
        cards = pl.get("cards", [])
        if len(cards) > 8:
            draw_defenses(canvas, cards, x, BOAT_HEAD, BOAT_PANEL_H)
        elif cards:
            draw_deck(canvas, pl, cards, x, BOAT_HEAD, 208, BOAT_PANEL_H)
        if debug:
            debug_print(pl, cards)

    # centre: towers
    rrect(canvas, (CEN_X, BOAT_HEAD, CEN_X + CEN_W, BOAT_HEAD + BOAT_PANEL_H), 30, fill=(250, 251, 255, 255))
    text(ImageDraw.Draw(canvas), (CEN_X + CEN_W // 2, BOAT_HEAD + 44), "TOWERS", 38, fill=(30, 34, 70), stroke=0)
    new, rem = battle.get("newTowersDestroyed"), battle.get("remainingTowers")
    start = (rem + new) if (rem is not None and new is not None) else None
    rows = [("TOWERS AT START", "wood", start), ("TOWERS DESTROYED", "red", new), ("TOWERS LEFT", "steel", rem)]
    rows = [r for r in rows if r[2] is not None] or [("TOWERS DESTROYED", "red", "-")]
    step = (BOAT_PANEL_H - 90) / len(rows)
    for i, (label, kind, val) in enumerate(rows):
        y = BOAT_HEAD + 90 + i * step
        text(ImageDraw.Draw(canvas), (CEN_X + CEN_W // 2, y + 18), label, 20, fill=(95, 102, 140), stroke=0)
        hex_badge(canvas, CEN_X + CEN_W // 2, int(y + step / 2 + 14), min(62, step * 0.30), kind, val)
    draw_footer(canvas, battle, H, tz, 1)
    canvas.convert("RGB").save(out_path, quality=95)


def debug_print(player, cards):
    print("--", player.get("name"), "| clan:", (player.get("clan") or {}).get("name"),
          "| war trophies:", (player.get("clan") or {}).get("clanWarTrophies"),
          "| badge:", (player.get("clan") or {}).get("badgeId"))
    for c in cards:
        v, u = card_variant(c)
        print(f"   {c['name']:<22} {v:<6} evo={c.get('evolutionLevel')} hero={c.get('heroLevel')} "
              f"icons={list((c.get('iconUrls') or {}).keys())}")


# ------------------------------------------------------------------ ranked ---
# Every battle carries its own `leagueNumber` (Path of Legend league 1-7; 7 = the rated top league).
# Per the API docs: in leagues 1-6 `trophyChange` is a fixed +30 on every win (a step marker, not real points)
# and `startingTrophies` is absent; only in league 7 are both real rating numbers. So points are shown
# for league 7 only, and hidden otherwise. League NAMES for 1-6 aren't documented - add them if you know them.
PATH_LEAGUE_NAMES = {7: "Ultimate Champion"}


def prep_display(battle):
    """Hide numbers that would mislead (display only; the outcome was already decided)."""
    t = str(battle.get("type", "")).lower()
    hide = (t == "pathoflegend" and battle.get("leagueNumber") != 7) or t in ("tournament", "boatbattle")
    if hide:
        for p in battle.get("team", []) + battle.get("opponent", []):
            p["startingTrophies"] = None
            p["trophyChange"] = None


def ranked_sub(battle):
    if str(battle.get("type", "")).lower() != "pathoflegend":
        return None
    n = battle.get("leagueNumber")
    parts = ["RANKED"]
    if n:
        parts.append(PATH_LEAGUE_NAMES.get(n, f"LEAGUE {n}").upper())
    if n == 7:
        me = (battle.get("team") or [{}])[0]
        if me.get("startingTrophies") is not None:
            parts.append(f"{me['startingTrophies']:,} PTS")
        if me.get("trophyChange"):
            parts.append(f"{me['trophyChange']:+d} POINTS")
    return "  ·  ".join(parts)


# ----------------------------------------------------------------- filters --
def is_war(b):
    t = str(b.get("type", "")).lower()
    return t.startswith("riverrace") or t == "boatbattle" or t.startswith("clanwar")


def matches(b, only):
    t = str(b.get("type", "")).lower()
    mode = str((b.get("gameMode") or {}).get("name", "")).lower()
    if only in (None, "all"):
        return True
    return {
        "war": is_war(b),
        "duel": "duel" in t or "duel" in mode,
        "ranked": t == "pathoflegend" or "ranked" in mode,
        "ladder": t == "pvp" and "ladder" in mode,
        "friendly": "friendly" in t,
        "2v2": len(b.get("team", [])) == 2,
        "challenge": "challenge" in t or "tournament" in t,
    }.get(only, False)


# -------------------------------------------------------------------- demo --
def _card(name, rarity, cost, evo=0, hero=0, lvl=16, maxl=16, used=None, art=True):
    icons = {"medium": "x"}
    if art and evo: icons["evolutionMedium"] = "x"
    if art and hero: icons["heroMedium"] = "x"
    c = {"name": name, "rarity": rarity, "elixirCost": cost, "level": lvl, "maxLevel": maxl,
         "evolutionLevel": (1 if evo else 0) | (2 if hero else 0), "iconUrls": icons}
    if used is not None:
        c["used"] = used
    return c


def _deck(seed, used=None):
    names = [("Cannon", "common", 3), ("Hero Mage", "champion", 5), ("Skeleton King", "champion", 4),
             ("Ice Spirit", "common", 1), ("Hog Rider", "rare", 4), ("Fireball", "rare", 4),
             ("Log", "legendary", 2), ("Ice Golem", "rare", 2), ("Wizard", "rare", 5), ("Bomber", "common", 2),
             ("Goblin", "epic", 3), ("Valkyrie", "rare", 4), ("Bat", "common", 2), ("Royal Hogs", "rare", 5),
             ("Dart", "common", 3), ("Tornado", "epic", 3)]
    out = []
    for i in range(8):
        nm, ra, co = names[(seed + i) % len(names)]
        u = None if used is None else (used if isinstance(used, bool) else (i % 3 != 0))
        if i == 2 and seed == 5:     # a brand-new hero whose hero art isn't in the catalog yet
            out.append(_card("Electro Wizard", "rare", 4, hero=1, used=u, art=False)); continue
        out.append(_card(nm, ra, co, evo=1 if i == 0 else 0, hero=1 if i == 1 else 0, used=u))
    return out


def demo_battle(duel=False):
    clanA = {"tag": "#A", "name": "Afterglow", "clanWarTrophies": 5200}
    clanB = {"tag": "#B", "name": "Poderão Andaluz", "clanWarTrophies": 700}
    def rnd(c, kh, ph, leak, seed, used=None):
        return {"crowns": c, "kingTowerHitPoints": kh, "princessTowersHitPoints": ph,
                "elixirLeaked": leak, "cards": _deck(seed, used=used)}
    team = {"name": "TAO | Noppi❤️", "tag": "#X", "clan": clanA, "crowns": 3,
            "elixirLeaked": 4.84, "kingTowerHitPoints": 6000, "princessTowersHitPoints": [3200, 2800],
            "cards": _deck(0), "trophyChange": 30,
            "supportCards": [{"name": "Tower Princess", "level": 11, "maxLevel": 11, "iconUrls": {"medium": "x"}}]}
    opp = {"name": "TEF丨Soudy✨Kun 👨‍👩‍👧 🇧🇩", "tag": "#Y", "clan": clanB, "crowns": 0,
           "elixirLeaked": 14.84, "kingTowerHitPoints": 0, "princessTowersHitPoints": [0, 0], "cards": _deck(5), "globalRank": 412,
           "supportCards": [{"name": "Cannoneer", "level": 14, "maxLevel": 14, "iconUrls": {"medium": "x"}}]}
    b = {"battleTime": "20261010T192308.000Z", "type": "pathOfLegend", "gameMode": {"name": "Ranked1v1_NewArena"},
         "arena": {"name": "Legendary Arena"}, "team": [team], "opponent": [opp],
         "leagueNumber": 4}
    if duel:
        team["rounds"] = [rnd(0, None, [3052], 7.3, 0, used=False), rnd(3, 5800, [3000, 2400], 1.2, 8, used=1)]
        opp["rounds"] = [rnd(3, 7728, [4800, 4900], 0.4, 5, used=1), rnd(0, 0, [0, 0], 14.8, 3, used=1)]
        b.update({"type": "riverRaceDuel", "gameMode": {"name": "CW_Duel_1v1"}, "arena": {"name": "Valkalla"}})
    return b


def demo_boat():
    attacker = {"name": "lm57800", "tag": "#ATT", "clan": {"tag": "#C", "name": "1FranceGDC"}, "startingTrophies": 13562,
                "cards": _deck(3), "supportCards": [{"name": "Tower Princess", "level": 16, "maxLevel": 16,
                                                     "iconUrls": {"medium": "x"}}]}
    names = ["Cannon", "Tesla", "Mortar", "Inferno Tower", "Bomb Tower", "Tombstone", "Goblin Hut", "Furnace",
             "X-Bow", "Barbarian Hut", "Elixir Collector", "Zappies"]
    defender = {"name": "TAO | Noppi❤️", "tag": "#DEF", "clan": {"tag": "#D", "name": "TheAddictedOnes"},
                "startingTrophies": 0, "supportCards": [{"name": "Cannoneer", "level": 14, "maxLevel": 14,
                                                         "iconUrls": {"medium": "x"}}],
                "cards": [_card(n, "common", 3, evo=3 if i == 0 else 0) for i, n in enumerate(names)]}
    return {"type": "boatBattle", "battleTime": "20261008T165725.000Z", "gameMode": {"name": "ClanWar_BoatBattle"},
            "arena": {"name": "Arena_Clanboat"}, "team": [defender], "opponent": [attacker],
            "boatBattleSide": "defender", "boatBattleWon": True, "newTowersDestroyed": 0,
            "prevTowersDestroyed": 0, "remainingTowers": 3}


def demo_emblems(path):
    canvas = background(1000, 260)
    for i, tro in enumerate([100, 700, 2500, 5200]):
        draw_emblem(canvas, 130 + i * 250, 120, 130, {"name": "X", "tag": "#X", "clanWarTrophies": tro})
    canvas.convert("RGB").save(path)


# -------------------------------------------------------------------- main --
def main():
    global OFFLINE, CREDIT, SHOW_CROWNS, ASSETS_DIR, SHOW_EMBLEM, SHOW_TOWER_TROOP, RARITY_FRAMES, WAR_LEAGUES, SHOW_TIER, CARD_STRETCH, BG_PATH, BG_DIM, BG_BLUR, BADGE_DIR, ESTIMATE_LEAGUE
    ap = argparse.ArgumentParser(description="Clash Royale battle log image generator")
    ap.add_argument("--tag", default=DEFAULT_TAG)
    ap.add_argument("--key", default=os.environ.get("CR_API_KEY"))
    ap.add_argument("--count", type=int, default=1, help="how many (matching) battles to render")
    ap.add_argument("--only", default="all",
                    choices=["all", "war", "duel", "ranked", "ladder", "friendly", "2v2", "challenge"],
                    help="only render this kind of battle")
    ap.add_argument("--war", action="store_true", help="shortcut for --only war")
    ap.add_argument("--list", action="store_true", help="list the battle log and exit")
    ap.add_argument("--out", default="battlelogs")
    ap.add_argument("--bg", default=None, help="background image (file path or URL)")
    ap.add_argument("--bg-dim", type=float, default=0.40, help="0-1 darkening over the background image")
    ap.add_argument("--bg-blur", type=float, default=0, help="blur radius for the background image")
    ap.add_argument("--card-stretch", type=float, default=1.0, help="card height factor (e.g. 1.15 = taller)")
    ap.add_argument("--rarity-frames", action="store_true", help="draw a rarity-coloured frame behind cards")
    ap.add_argument("--clan-emblem", action="store_true", help="draw the clan badge emblem (off by default)")
    ap.add_argument("--no-tower-troop", action="store_true", help="hide the tower troop card")
    ap.add_argument("--credit", default=None, help='footer credit text (default: "made with 🫶🏻 by ja")')
    ap.add_argument("--no-credit", action="store_true", help="hide the footer credit")
    ap.add_argument("--show-crowns", action="store_true",
                    help="show the crown score in the header (same number as 'TOWERS TAKEN', so off by default)")
    ap.add_argument("--no-tier", action="store_true", help="hide the league tier plate on clan emblems")
    ap.add_argument("--badge-dir", default=None, help="folder with <badgeId>.png clan badge images")
    ap.add_argument("--clan-league", action="append", default=[], metavar='"#TAG=Legendary II"',
                    help="the real war league of a clan (repeatable). Also read from clan_leagues.json")
    ap.add_argument("--known-leagues-only", action="store_true",
                    help="don't estimate leagues from war trophies; only colour clans you set with --clan-league")
    ap.add_argument("--leagues", default=None, help='JSON: [[min_trophies,"Gold","II"], ...]')
    ap.add_argument("--plays", default=None, help='JSON {"<player tag or name>": {"Card Name": times_played}}')
    ap.add_argument("--font", default=None)
    ap.add_argument("--tz", default="America/New_York")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--dump", action="store_true", help="save raw battle JSON next to each image")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--demo-duel", action="store_true")
    ap.add_argument("--demo-emblems", action="store_true")
    ap.add_argument("--demo-boat", action="store_true")
    ap.add_argument("--demo-war", action="store_true")
    ap.add_argument("--assets-dir", default=None,
                    help="folder with official art (Supercell Fan Kit downloads etc.): icons + card art, found by file name")
    a = ap.parse_args()

    setup_fonts(a.font)
    RARITY_FRAMES, SHOW_TIER, CARD_STRETCH = a.rarity_frames, not a.no_tier, a.card_stretch
    BG_PATH, BG_DIM, BG_BLUR, BADGE_DIR = a.bg, a.bg_dim, a.bg_blur, a.badge_dir
    SHOW_EMBLEM, SHOW_TOWER_TROOP, SHOW_CROWNS = a.clan_emblem, not a.no_tower_troop, a.show_crowns
    if a.no_credit:
        CREDIT = ""
    elif a.credit is not None:
        CREDIT = a.credit
    for cand in (a.assets_dir, "assets", str(Path(__file__).with_name("assets"))):
        if cand and os.path.isdir(cand):
            ASSETS_DIR = cand
            break
    apply_layout()
    ESTIMATE_LEAGUE = not a.known_leagues_only
    for cfg in (Path("clan_leagues.json"), Path(__file__).with_name("clan_leagues.json")):
        if cfg.exists():
            for t, v in json.loads(cfg.read_text(encoding="utf-8")).items():
                CLAN_LEAGUES[norm_tag(t)] = parse_league(v)
    for item in a.clan_league:
        t, _, v = item.partition("=")
        CLAN_LEAGUES[norm_tag(t)] = parse_league(v)
    if a.leagues:
        WAR_LEAGUES = [tuple(x) for x in json.loads(Path(a.leagues).read_text())]
    if a.plays:
        PLAYS.update(json.loads(Path(a.plays).read_text(encoding="utf-8")))
    Path(a.out).mkdir(exist_ok=True)

    if a.demo_emblems:
        OFFLINE = True
        demo_emblems(Path(a.out) / "demo_emblems.png"); print("Saved demo_emblems.png"); return
    if a.demo_boat or a.demo_war:
        OFFLINE = True
        if a.demo_boat:
            b, name = demo_boat(), "demo_boat.png"
        else:
            b, name = demo_battle(False), "demo_war.png"
            b.update({"type": "riverRacePvP", "gameMode": {"name": "TripleElixir_Ladder"}, "arena": {"name": "Valkalla"}})
            b.pop("leagueNumber", None)
        render_battle(b, Path(a.out) / name, a.tz, a.debug)
        print("Saved", Path(a.out) / name); return
    if a.demo or a.demo_duel:
        OFFLINE = True
        if a.demo:
            PLAYS.update({"ja II": {"Cannon": 3, "Hero Mage": 4, "Skeleton King": 2, "Ice Spirit": 6,
                                    "Hog Rider": 5, "Fireball": 0, "Log": 4, "Ice Golem": 0}})
        CLAN_LEAGUES["#A"] = ("Legendary", "II")   # demo: one known league, one estimated
        path = Path(a.out) / ("demo_duel.png" if a.demo_duel else "demo.png")
        render_battle(demo_battle(a.demo_duel), path, a.tz, a.debug)
        print("Saved", path); return

    if not a.key:
        sys.exit("Set CR_API_KEY or pass --key")
    log = api_get(f"/players/{enc(a.tag)}/battlelog", a.key)
    if not log:
        sys.exit("Battle log is empty.")
    if a.list:
        for i, b in enumerate(log):
            print(f"{i + 1:>2}. {fmt_time(b['battleTime'], a.tz):<26} {b.get('type', '?'):<22} "
                  f"{(b.get('gameMode') or {}).get('name', '?')}")
        return
    only = "war" if a.war else a.only
    chosen = [b for b in log if matches(b, only)][:a.count]
    if not chosen:
        sys.exit(f"No '{only}' battles in the last {len(log)} battles (the API only keeps the latest ~25). "
                 f"Try --list.")
    meta = get_cards_meta(a.key)
    for i, b in enumerate(chosen):
        enrich_cards(b, meta)
        if SHOW_EMBLEM:
            enrich_clans(b, a.key)
        kind = re.sub(r"[^a-z0-9]+", "", str(b.get("type", "battle")).lower())
        path = Path(a.out) / f"battle_{i + 1}_{kind}_{b['battleTime']}.png"
        if a.dump:
            b["_clans"] = {t: CLAN_CACHE.get(t) for p in (b.get("team", []) + b.get("opponent", []))
                          for t in [(p.get("clan") or {}).get("tag")] if t}
            path.with_suffix(".json").write_text(json.dumps(b, indent=2, ensure_ascii=False), encoding="utf-8")
        if render_battle(b, path, a.tz, a.debug):
            print("Saved", path)


if __name__ == "__main__":
    main()
