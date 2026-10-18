#!/usr/bin/env python3
"""
ja's watcher - posts Clash Royale battle images to Discord. Free to run (see README.md).

config.json is just a list of things to watch:

    { "watch": [
        { "clan":   "#CLANTAG",   "webhook": "DISCORD_WEBHOOK_1" },
        { "player": "#PLAYERTAG", "webhook": "DISCORD_WEBHOOK_2", "modes": ["ladder", "ranked"], "results": "win" }
    ] }

  clan / player : whose battles (a clan = all its current members)
  webhook       : the NAME of a GitHub secret holding the Discord webhook URL
  modes         : war duel boat ladder ranked challenge event friendly tournament 2v2 special all
                  mode:<text> (e.g. "mode:triple elixir")   type:<text>      (default: clan -> war, player -> all)
  results       : all | win | loss                                              (default: all)
  days          : all | battle | training    war battles only                   (default: all)

    python cr_war_discord.py                          # one pass (what GitHub Actions runs)
    python cr_war_discord.py --loop 300               # keep running on a PC / Raspberry Pi
    python cr_war_discord.py --backfill 4             # post 4 DIFFERENT layouts to every watch: boat, 1v1, duel 2-0, duel 2-1
    python cr_war_discord.py --dry-run --backfill 2   # write them to ./dry_run instead of posting

Training day vs battle day: the battle log does not say. The clan's live /currentriverrace does
(periodType + periodIndex), so the bot keeps a small "clock" per clan: it notices when the day changes and, because
days last 24 h, can name the day of any battle that is not close to a day change. See README.md.
"""
import argparse, json, os, re, sys, tempfile, time
from datetime import datetime, timezone
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import cr_battlelog_image as crimg  # noqa: E402

DEFAULT_BASE = "https://proxy.royaleapi.dev/v1"
DEFAULT_BOT = "ja's watcher"
KEEP_KEYS = 2500                      # remembered battle keys per watch
VALID_MODES = {"all", "war", "duel", "boat", "ladder", "ranked", "challenge", "event", "friendly",
               "tournament", "2v2", "special"}
WAR_MODES = {"war", "duel", "boat"}
DAY_S = 86400


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def norm_tag(t):
    return "#" + str(t).strip().lstrip("#").upper()


def enc(tag):
    return requests.utils.quote(norm_tag(tag), safe="")


def parse_bt(bt):
    return datetime.strptime(bt, "%Y%m%dT%H%M%S.%fZ").replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- API
class Api:
    def __init__(self, key, base):
        self.key, self.base, self.s = key, base.rstrip("/"), requests.Session()

    def get(self, path):
        """(status, json|None). Retries 429/5xx. Raises on 403 (bad key / IP not whitelisted)."""
        status = 0
        for attempt in range(4):
            try:
                r = self.s.get(self.base + path, headers={"Authorization": f"Bearer {self.key}"}, timeout=25)
            except requests.RequestException as e:
                log("network error:", e); time.sleep(2 * (attempt + 1)); continue
            status = r.status_code
            if status == 200:
                return 200, r.json()
            if status == 403:
                raise RuntimeError("API answered 403: invalid key, or this machine's IP is not whitelisted for it. "
                                   "With the RoyaleAPI proxy the key must be created with IP 45.79.218.79.")
            if status in (429, 500, 502, 503, 504):
                time.sleep(min(3 * 2 ** attempt, 30)); continue
            return status, None
        return status, None


# ------------------------------------------------------------------------- state
def load_state(path):
    try:
        st = json.loads(Path(path).read_text())
    except Exception:
        st = {}
    st.setdefault("watches", {})
    st.setdefault("clocks", {})
    return st


def save_state(path, st):
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(st, indent=1, sort_keys=True))
    tmp.replace(path)


# ------------------------------------------------------------------------ config
def strip_comments(text):
    """Lets config.json contain  # comments  (and // comments, and trailing commas). '#' inside "quotes" is kept,
    so tags like "#ABC123" are fine."""
    out, i, n, in_str, esc = [], 0, len(text), False, False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True; out.append(c); i += 1; continue
        if c == "#" or (c == "/" and text[i + 1:i + 2] == "/"):
            while i < n and text[i] != "\n":
                i += 1
            continue
        out.append(c); i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))



def load_config(path):
    try:
        cfg = json.loads(strip_comments(Path(path).read_text(encoding="utf-8")))
    except json.JSONDecodeError as e:
        sys.exit(f"config.json isn't valid ({e}). Check commas and quotes near line {e.lineno}.")
    raw = list(cfg.get("watch") or cfg.get("watches") or [])
    for c in cfg.get("clans", []):                       # old format: {"clans": [{"name","tag","webhook_env"}]}
        raw.append({"clan": c["tag"], "webhook": c.get("webhook_env") or c.get("webhook"), "name": c.get("name")})
    if not raw:
        sys.exit('config.json needs a "watch" list, e.g. {"watch": [{"clan": "#TAG", "webhook": "DISCORD_WEBHOOK_1"}]}')
    watches, errors = [], []
    for i, w in enumerate(raw, 1):
        where = f"watch #{i}"
        if ("clan" in w) == ("player" in w):
            errors.append(f'{where}: give exactly one of "clan" or "player"'); continue
        kind = "clan" if "clan" in w else "player"
        tag = norm_tag(w[kind])
        if not re.fullmatch(r"#[0-9A-Z]{3,15}", tag):
            errors.append(f"{where}: '{w[kind]}' doesn't look like a tag"); continue
        if not w.get("webhook"):
            errors.append(f'{where}: missing "webhook" (the name of the GitHub secret with the Discord webhook URL)'); continue
        modes = w.get("modes", ["war"] if kind == "clan" else ["all"])
        modes = [modes] if isinstance(modes, str) else list(modes)
        modes = [str(m).strip().lower() for m in modes]
        bad = [m for m in modes if m not in VALID_MODES and not m.startswith(("mode:", "type:"))]
        if bad:
            errors.append(f"{where}: unknown mode(s) {bad}. Allowed: {sorted(VALID_MODES)} or mode:<text> / type:<text>")
        results = str(w.get("results", "all")).lower()
        if results not in ("all", "win", "loss"):
            errors.append(f'{where}: "results" must be all, win or loss')
        days = str(w.get("days", "all")).lower()
        if days not in ("all", "battle", "training"):
            errors.append(f'{where}: "days" must be all, battle or training')
        watches.append({"kind": kind, "tag": tag, "webhook": str(w["webhook"]), "modes": modes, "results": results,
                        "days": days, "label": w.get("name") or tag, "bot_name": w.get("bot_name")})
    if str(cfg.get("duel_images", "combined")).lower() not in ("split", "combined"):
        errors.append('"duel_images" must be split (one image per round) or combined (one tall image)')
    if cfg.get("warnings_repeat_days") is not None:
        try:
            float(cfg["warnings_repeat_days"])
        except (TypeError, ValueError):
            errors.append('"warnings_repeat_days" must be a number (0 = mention each missing file only once)')
    if str(cfg.get("embed_time", "discord")).lower() == "utc":
        log('NOTE: "embed_time": "utc" prints a FIXED UTC time that is the same for everyone. For a time every '
            'viewer sees in their own time zone, set "embed_time": "discord" (or delete the line).')
    if str(cfg.get("embed_time", "discord")).lower() not in ("discord", "utc", "none"):
        errors.append('"embed_time" must be discord, utc or none')
    if str(cfg.get("embed_time_style", "F")) not in list("tTdDfFR"):
        errors.append('"embed_time_style" must be one of t T d D f F R (Discord timestamp styles)')
    if str(cfg.get("embed_time_position", "below")).lower() not in ("below", "inside"):
        errors.append('"embed_time_position" must be below or inside')
    for key in ("credit_color", "credit_name_color"):
        if cfg.get(key):
            try:
                crimg.parse_color(cfg[key])
            except ValueError as e:
                errors.append(f'"{key}": {e}')
    if errors:
        sys.exit("config.json problems:\n  - " + "\n  - ".join(errors))
    return cfg, watches


def resolve_webhook(ref):
    if ref.startswith("https://"):
        log("WARNING: a full webhook URL is in config.json - if your repo is public anyone can read it. "
            "Put it in a GitHub secret and use the secret's name instead.")
        return ref
    v = os.environ.get(ref)
    if v:
        return v
    try:
        return (json.loads(os.environ.get("SECRETS_JSON") or "{}") or {}).get(ref)
    except Exception:
        return None


# --------------------------------------------------------------- what is a battle
def categories(b):
    t = str(b.get("type", "")).lower()
    mode = str((b.get("gameMode") or {}).get("name", ""))
    c = {"all"}
    if t == "pvp":
        c.add("ladder")
    if t == "pathoflegend":
        c.add("ranked")
    if crimg.is_war(b):
        c.add("war")
    if t.startswith("riverraceduel"):
        c.add("duel")
    if t == "boatbattle":
        c.add("boat")
    if t == "trail":
        c.add("challenge" if "challenge" in mode.lower() else "event")
    if t in ("friendly", "clanmate", "clanmate2v2"):
        c.add("friendly")
    if t == "tournament":
        c.add("tournament")
    if len(b.get("team") or []) == 2:
        c.add("2v2")
    if mode and re.sub(r"[^a-z0-9]", "", mode.lower()) not in crimg.STANDARD_MODES:
        c.add("special")
    return c


def matches_modes(b, modes):
    cats = categories(b)
    mode = re.sub(r"[^a-z0-9]", "", str((b.get("gameMode") or {}).get("name", "")).lower())
    typ = str(b.get("type", "")).lower()
    for m in modes:
        if m in cats:
            return True
        if m.startswith("mode:") and re.sub(r"[^a-z0-9]", "", m[5:]) in mode:
            return True
        if m.startswith("type:") and m[5:].strip() in typ:
            return True
    return False


def duel_score(b):
    """(rounds won, rounds lost) for a war duel, else None."""
    team, opp = b.get("team") or [], b.get("opponent") or []
    if len(team) == 1 and len(opp) == 1:
        rt, ro = crimg.get_rounds(team[0]), crimg.get_rounds(opp[0])
        rc = min(len(rt), len(ro))
        if rc >= 1 and (str(b.get("type", "")).lower().startswith("riverraceduel") or rc > 1):
            views = [(crimg.round_view(team[0], x), crimg.round_view(opp[0], y)) for x, y in zip(rt[:rc], ro[:rc])]
            for a, c in views:
                crimg.fix_king_hp([a], [c]); crimg.fix_king_hp([c], [a])
            w = [crimg.round_winner(a, c) for a, c in views]
            return w.count(1), w.count(-1)
    return None


def result_of(b):
    """+1 win / -1 loss / 0 draw-or-unknown. Duels are decided by rounds won, like the image shows."""
    sc = duel_score(b)
    if sc:
        return (sc[0] > sc[1]) - (sc[0] < sc[1])
    return crimg.battle_outcome(b)


KIND_ORDER = ["boat", "single", "duel_sweep", "duel_decider", "2v2", "duel_other"]
KIND_LABEL = {"boat": "boat battle", "single": "1v1", "duel_sweep": "duel 2-0/0-2", "duel_decider": "duel 2-1/1-2",
              "2v2": "2v2", "duel_other": "duel (other score)"}


def layout_kind(b):
    """Which image layout / result shape this battle produces."""
    if str(b.get("type", "")).lower() == "boatbattle":
        return "boat"
    sc = duel_score(b)
    if sc:
        hi, lo = max(sc), min(sc)
        return "duel_sweep" if (hi, lo) == (2, 0) else "duel_decider" if (hi, lo) == (2, 1) else "duel_other"
    return "2v2" if len(b.get("team") or []) == 2 else "single"


def pick_variety(matching, n):
    """backfill N: one battle per layout - boat battle, 1v1, duel 2-0/0-2, duel 2-1/1-2 (newest of each) - so every
    image style gets tested. N > 4 goes round again; a missing layout is skipped."""
    by = {k: [] for k in KIND_ORDER}
    for item in sorted(matching, key=lambda x: x[1]["battleTime"], reverse=True):
        by[layout_kind(item[1])].append(item)
    chosen = []
    while len(chosen) < n and any(by.values()):
        for k in KIND_ORDER:
            if by[k] and len(chosen) < n:
                chosen.append(by[k].pop(0))
    return chosen


# ---------------------------------------------------- the clan "clock" (training / battle day)
def update_clock(clocks, tag, period, now):
    """Remember when each river-race day starts. A day change is seen between two passes, so its time is known to
    within one polling interval; days last 24 h, so that pins down every other day boundary too."""
    idx, typ = int(period["index"]), period.get("type")
    ck = clocks.get(tag)
    if ck is None:
        clocks[tag] = {"index": idx, "type": typ, "last_pass": now, "since": now, "anchor": None}
    elif idx != ck["index"]:
        lo, hi = ck["last_pass"], now
        anchor = {"index": idx, "lo": lo, "hi": hi} if (hi - lo) <= 3 * 3600 else None
        old = ck.get("anchor")
        if anchor and old:
            expect = (old["lo"] + old["hi"]) / 2 + DAY_S * (idx - old["index"])
            if abs(expect - (lo + hi) / 2) > 1800 and idx != 0:
                log(f"[{tag}] the day change moved by more than 30 min - using the new time")
        clocks[tag] = {"index": idx, "type": typ, "last_pass": now, "since": now, "anchor": anchor}
    else:
        ck["last_pass"], ck["type"] = now, typ


def period_for(ck, t):
    """{'type','index'} for a battle at epoch t, or None when it can't be known safely."""
    if not ck:
        return None
    cur = ck["index"]
    a = ck.get("anchor")
    if t >= ck["since"]:
        idx = cur                                             # played after we saw this day start: certain
    elif a:
        mid = (a["lo"] + a["hi"]) / 2
        unc = (a["hi"] - a["lo"]) / 2 + 900                  # + 15 min safety
        off = t - mid
        d = off % DAY_S
        if min(d, DAY_S - d) < unc:
            return None                                       # too close to a day change
        idx = a["index"] + int(off // DAY_S)
    else:
        return None
    if idx < 0 or idx > cur:
        return None
    day = idx % 7
    if day < 3:
        typ = "training"
    elif ck.get("type") == "colosseum" and idx // 7 == cur // 7:
        typ = "colosseum"
    else:
        typ = "warDay"
    return {"type": typ, "index": idx}


# ----------------------------------------------------------------------- discord
_MD = re.compile(r"([\\*_~`|>\[\]()])")


def md(s):
    return _MD.sub(r"\\\1", str(s or "?").replace("\n", " "))


def link_player(p):
    tag = p.get("tag")
    name = md(p.get("name"))
    return f"[{name}](https://royaleapi.com/player/{str(tag).lstrip('#')})" if tag else name


def link_clan(c):
    if not c or not c.get("name"):
        return ""
    return (f"[{md(c['name'])}](https://royaleapi.com/clan/{str(c['tag']).lstrip('#')})" if c.get("tag")
            else md(c["name"]))


def side_text(players, dot):
    lines = []
    for p in players or []:
        s = f"{dot} **{link_player(p)}**"
        c = link_clan(p.get("clan"))
        lines.append(s + (f" · {c}" if c else ""))
    return "\n".join(lines) or f"{dot} ?"


def post_webhook(url, files, embeds, username):
    """files = [(filename, png_bytes), ...]; the first len(files) embeds each show one of them."""
    embeds = [dict(e, image={"url": f"attachment://{files[i][0]}"}) if i < len(files) else e
              for i, e in enumerate(embeds)]
    payload = {"username": username[:80], "embeds": embeds, "allowed_mentions": {"parse": []}}
    for attempt in range(3):
        r = requests.post(url + ("&" if "?" in url else "?") + "wait=true",
                          data={"payload_json": json.dumps(payload)},
                          files={f"files[{i}]": (n, data, "image/png") for i, (n, data) in enumerate(files)},
                          timeout=90)
        if r.status_code == 429:
            time.sleep(float(r.json().get("retry_after", 2)) + 0.5); continue
        r.raise_for_status()
        return
    raise RuntimeError("Discord kept rate-limiting the webhook")


# ------------------------------------------------------------------------- core
class Run:
    def __init__(self, cfg, api, dry, backfill, state_path):
        self.cfg, self.api, self.dry, self.backfill, self.state_path = cfg, api, dry, backfill, state_path
        self.state = {"watches": {}, "clocks": {}} if dry else load_state(state_path)
        self.meta = None
        self.rr, self.clock_checked = {}, set()
        self.problems = 0
        self.summary = []
        self.tmp = Path(tempfile.mkdtemp(prefix="crwatch_"))

    # ---- missing-file warnings
    def flush_missing(self):
        """Post what the renderer could not find (with the EXACT file name to add) to the warnings webhook.
        Outstanding items are re-checked against the assets folder every pass and repeated every N days."""
        now = time.time()
        every = float(self.cfg.get("warnings_repeat_days", 7))
        seen = self.state.setdefault("missing", {})
        pending = self.state.setdefault("missing_info", {})
        pending.update(dict(crimg.MISSING))              # what this pass could not find ...
        crimg.MISSING.clear()
        for k in list(pending):                          # ... plus what is still outstanding from before,
            if crimg.asset_exists(pending[k]["file"]):   # minus whatever you have added since
                pending.pop(k); seen.pop(k, None)
        fresh = {k: v for k, v in pending.items()
                 if seen.get(k) is None or (every > 0 and now - seen[k] >= every * 86400)}
        if not fresh:
            return
        groups = {"Card images": [], "Icons and plates": [], "Fonts": []}
        for key, info in sorted(fresh.items()):
            if key.startswith("card:"):
                groups["Card images"].append(f"• `assets/cards/{info['file']}` - {info['what']}")
            elif key.startswith("font:"):
                groups["Fonts"].append(f"• `{info['file']}` - {info['what']}")
            else:
                groups["Icons and plates"].append(f"• `assets/icons/{info['file']}` - {info['what']}")
        text_blocks = [f"**{title}**\n" + "\n".join(lines) for title, lines in groups.items() if lines]
        for tb in text_blocks:
            log("MISSING FILES:\n" + tb)
        ref = self.cfg.get("warnings_webhook")
        hook = None if self.dry else (resolve_webhook(ref) if ref else None)
        if not hook:
            return                                   # no warnings channel configured (or dry run): console only
        footer = (f"Add the file(s) to the assets/ folder of your repo - any sub-folder, .png or .webp, capitals and "
                  f"_ - don't matter. Each one is mentioned again every {int(every)} days until it exists."
                  if every else "Add the file(s) to the assets/ folder of your repo; each one is only mentioned once.")
        embeds, cur = [], ""
        for tb in text_blocks:
            for chunk in [tb[i:i + 3500] for i in range(0, len(tb), 3500)]:
                if len(cur) + len(chunk) > 3500:
                    embeds.append(cur); cur = ""
                cur += chunk + "\n\n"
        if cur.strip():
            embeds.append(cur)
        payload = {"username": (self.cfg.get("bot_name") or DEFAULT_BOT)[:80], "allowed_mentions": {"parse": []},
                   "embeds": [{"title": "⚠️ Files to add" + (f" ({i + 1}/{len(embeds)})" if len(embeds) > 1 else ""),
                               "description": e.strip(), "color": 0xF1C40F,
                               **({"footer": {"text": footer}} if i == len(embeds) - 1 else {})}
                              for i, e in enumerate(embeds)]}
        try:
            r = requests.post(hook, json=payload, timeout=30)
            if r.status_code == 429:
                time.sleep(float(r.json().get("retry_after", 2)) + 0.5)
                r = requests.post(hook, json=payload, timeout=30)
            r.raise_for_status()
            for k in fresh:
                seen[k] = now
        except Exception as e:
            log(f"warnings webhook failed: {e!r}")
            self.problems += 1

    # ---- shared helpers
    def cards_meta(self):
        if self.meta is None:
            try:
                self.meta = crimg.get_cards_meta(self.api.key)
            except SystemExit as e:
                raise RuntimeError(f"card catalog request failed: {e}")
        return self.meta

    def start_pass(self, watches):
        self.rr, self.clock_checked = {}, set()
        tags = {w["tag"] for w in watches if w["kind"] == "clan"} | set(self.state["clocks"])
        for t in sorted(tags):                       # one call per tracked clan keeps every clock fresh
            self.touch_clock(t)

    def touch_clock(self, tag):
        tag = norm_tag(tag)
        if tag not in self.clock_checked:
            self.clock_checked.add(tag)
            st, rr = self.api.get(f"/clans/{enc(tag)}/currentriverrace")     # 404 between seasons is normal
            self.rr[tag] = rr
            if rr and rr.get("periodIndex") is not None:
                update_clock(self.state["clocks"], tag, {"type": rr.get("periodType"), "index": rr["periodIndex"]},
                             time.time())
        return self.state["clocks"].get(tag)

    def render(self, battle, period):
        """-> [(filename, png bytes)]: one image, or one per round for a split duel; [] if it can't be drawn."""
        crimg.enrich_cards(battle, self.cards_meta())
        if crimg.SHOW_EMBLEM or crimg.wants_war_trophies(battle):
            crimg.enrich_clans(battle, self.api.key)          # clanWarTrophies (cached per clan for the run)
        if crimg.wants_player_lookup(battle):
            crimg.enrich_players(battle, self.api.key)        # Ultimate Champion rating; boat-battle trophies (cached)
        if period:
            battle["_period"] = period
        files = crimg.render_battle_files(battle, self.tmp / "battle.png", self.cfg.get("timezone", "UTC"))
        return [(f"battle_{battle['battleTime']}" + (f"_r{i}" if len(files) > 1 else "") + ".png",
                 Path(f).read_bytes()) for i, f in enumerate(files, 1)]

    def embeds(self, b, n_images=1):
        """Box 1: names (RoyaleAPI links) + the image. Box 2 (under the image): the battle time as a Discord timestamp,
        which each viewer sees in their own language/time zone, e.g. <t:1791882480:F> = "Tuesday, October 13, 2026 at
        3:08 PM". (Discord can't render timestamps inside an embed footer, hence the thin second box.)
        Side colour: CR blue = win, red = loss, white for friendly / clanmate battles."""
        r = result_of(b)
        if "friendly" in categories(b):
            color = 0xFFFFFF                      # friendly battles: not a win or a loss
        else:
            color = {1: 0x2480F5, -1: 0xDE483A, 0: 0x95A5A6}[r]          # exactly the blue / red king colours of the image
        main = {"description": (side_text(b.get("team"), "🟦") + "\n" + side_text(b.get("opponent"), "🟥"))[:4000],
                "color": color}
        when = parse_bt(b["battleTime"])
        mode = str(self.cfg.get("embed_time", "discord")).lower()
        style = str(self.cfg.get("embed_time_style", "F"))
        if mode == "discord":
            line = f"<t:{int(when.timestamp())}:{style}>"
        elif mode == "utc":                       # the same plain text for everybody
            line = f"{when:%A}, {when:%B} {when.day}, {when.year} at {int(when.strftime('%I'))}:{when:%M} {when:%p} UTC"
        else:
            line = None
        out = [main] + [{"color": color} for _ in range(max(n_images, 1) - 1)]   # extra images: one box each
        if line:
            if str(self.cfg.get("embed_time_position", "below")).lower() == "inside":
                main["description"] += "\n" + line
            else:
                out.append({"description": line, "color": color})
        return out

    # ---- collecting battles
    def collect_player(self, w):
        st, blog = self.api.get(f"/players/{enc(w['tag'])}/battlelog")
        if blog is None:
            log(f"[{w['label']}] battle log request failed ({st}) - is the player tag right?"); self.problems += 1; return None
        return [(f"{w['tag']}|{b['battleTime']}", b) for b in blog]

    def collect_clan(self, w, ws, first):
        tag = w["tag"]
        st, mem = self.api.get(f"/clans/{enc(tag)}/members?limit=60")
        if not mem:
            log(f"[{w['label']}] members request failed ({st}) - is the clan tag right?"); self.problems += 1; return None
        rr = self.rr.get(tag) or {}
        parts = {p["tag"]: p for p in (rr.get("clan") or {}).get("participants", [])}
        war_only = all(m in WAR_MODES for m in w["modes"])
        scan_all = first or self.backfill > 0
        recheck = set(ws.get("recheck", []))
        found, moved_now, fetched, skipped = [], [], 0, 0
        war_by_clan, n_members = {}, len(mem.get("items", []))
        for m in mem.get("items", []):
            ptag = m["tag"]
            p = parts.get(ptag, {})
            sig = [p.get("decksUsed"), p.get("decksUsedToday"), p.get("fame"), p.get("boatAttacks")]
            moved = first or sig != ws["sig"].get(ptag) or m.get("lastSeen") != ws["seen"].get(ptag)
            ws["sig"][ptag], ws["seen"][ptag] = sig, m.get("lastSeen")
            # war-only watches fetch just the players whose war counters moved; other modes (and backfill) need everyone
            if war_only and not scan_all and not (moved or ptag in recheck):
                continue
            time.sleep(0.2)
            st, blog = self.api.get(f"/players/{enc(ptag)}/battlelog")
            if not blog:
                continue
            fetched += 1
            if moved:
                moved_now.append(ptag)
            for b in blog:
                played_for = (((b.get("team") or [{}])[0]).get("clan") or {}).get("tag")
                if crimg.is_war(b):
                    k = norm_tag(played_for) if played_for else "(no clan)"
                    war_by_clan[k] = war_by_clan.get(k, 0) + 1
                if played_for and norm_tag(played_for) != tag:
                    skipped += 1                    # e.g. a new member's battles from his PREVIOUS clan
                    continue
                found.append((f"{ptag}|{b['battleTime']}", b))
        ws["recheck"] = moved_now if war_only else []
        w["_diag"] = {"members": n_members, "fetched": fetched, "war_by_clan": war_by_clan}
        w["_stats"] = f"checked {fetched} players" + (f", ignored {skipped} played for another clan" if skipped else "")
        return found

    # ---- one watch
    def process(self, w):
        wid = f"{w['kind']}:{w['tag']}>{w['webhook']}"
        ws = self.state["watches"].setdefault(wid, {"initialized": False, "posted": [], "sig": {}, "seen": {},
                                                    "recheck": []})
        first = not ws["initialized"]
        found = self.collect_clan(w, ws, first) if w["kind"] == "clan" else self.collect_player(w)
        if found is None:
            self.summary.append({"watch": w["label"], "read": 0, "matching": 0, "posted": 0,
                                 "note": "lookup failed - check the tag"})
            return
        if w["kind"] == "player":
            w["_stats"] = "1 battle log"
        posted = set(ws["posted"])

        def wanted(b):
            if not matches_modes(b, w["modes"]):
                return False
            r = result_of(b)
            return w["results"] == "all" or (w["results"] == "win" and r == 1) or (w["results"] == "loss" and r == -1)

        matching = sorted([x for x in found if wanted(x[1])], key=lambda x: x[1]["battleTime"])
        if first:                                   # baseline: remember everything that already exists
            for key, _ in found:
                posted.add(key)
        note = ""
        if self.backfill > 0:                       # backfill N: N different layouts, for EVERY watch, even ones
            candidates = pick_variety(matching, self.backfill)  # posted before (testing; leave it 0 normally)
            if candidates:
                log(f"[{w['label']}] backfill {self.backfill}: posting "
                    + ", ".join(KIND_LABEL[layout_kind(b)] for _, b in candidates))
            else:
                d = w.get("_diag") or {}
                note = (f"read {len(found)} battles ({sum(1 for _, b in found if crimg.is_war(b))} war) from "
                        f"{d.get('fetched', '?')} of {d.get('members', '?')} members; none match modes {w['modes']} / "
                        f"results {w['results']}.")
                if d.get("war_by_clan"):
                    note += (f" War battles in their logs were played for clans {d['war_by_clan']} - this watch is "
                             f"{w['tag']}, so battles from other clans are ignored.")
                elif w["kind"] == "clan":
                    note += " No war battles at all in their recent logs (is the clan taking part in the river race?)."
                log(f"[{w['label']}] NOTHING TO POST: {note}")
        else:
            candidates = [] if first else [x for x in matching if x[0] not in posted]
        if first and self.backfill == 0:
            log(f"[{w['label']}] first pass: {len(found)} existing battles recorded, nothing posted")

        hook = None if self.dry else resolve_webhook(w["webhook"])
        if not self.dry and not hook:
            raise RuntimeError(f"no webhook found for '{w['webhook']}' (watch {w['label']}): add a GitHub secret "
                               f"with exactly that name")
        sent = held = 0
        for key, b in candidates:
            period = None
            if crimg.is_war(b):
                ctag = w["tag"] if w["kind"] == "clan" else (((b.get("team") or [{}])[0]).get("clan") or {}).get("tag")
                if ctag:
                    period = period_for(self.touch_clock(ctag), parse_bt(b["battleTime"]).timestamp())
                if w["days"] != "all":
                    if period is None:
                        held += 1; continue         # can't tell the day yet - try again next pass
                    is_battle = period["type"] in ("warDay", "colosseum")
                    if (w["days"] == "battle") != is_battle:
                        posted.add(key); continue   # the wrong kind of day: never post it
            try:
                files = self.render(b, period)
            except Exception as e:
                log(f"[{w['label']}] render failed for {key}: {e!r}"); posted.add(key); self.problems += 1; continue
            if not files:
                posted.add(key); continue
            if self.dry:
                d = HERE / "dry_run"; d.mkdir(exist_ok=True)
                for fname, data in files:
                    (d / re.sub(r"[^A-Za-z0-9_.-]", "_", f"{w['label']}_{fname}")).write_bytes(data)
                log(f"[{w['label']}] dry-run: wrote {len(files)} image(s) for {b['battleTime']}"); continue
            try:
                post_webhook(hook, files, self.embeds(b, len(files)),
                             w.get("bot_name") or self.cfg.get("bot_name") or DEFAULT_BOT)
            except Exception as e:
                log(f"[{w['label']}] Discord post failed for {key}: {e!r} - will retry next pass")
                self.problems += 1; continue
            posted.add(key); sent += 1
            ws["posted"] = list(dict.fromkeys(ws["posted"] + [key]))[-KEEP_KEYS:]
            save_state(self.state_path, self.state)
            time.sleep(1.2)

        ws["posted"] = list(dict.fromkeys(ws["posted"] + sorted(posted)))[-KEEP_KEYS:]
        ws["initialized"] = True
        log(f"[{w['label']}] pass done: {w.pop('_stats', '')}, new matching battles {len(candidates)}, posted {sent}"
            + (f", held back {held} (day unknown yet)" if held else ""))
        self.summary.append({"watch": w["label"], "read": len(found), "matching": len(matching), "posted": sent,
                             "note": note or (f"held back {held}" if held else "")})


def apply_image_options(im):
    """The "image" block of config.json -> the renderer's settings."""
    known = {"background", "background_dim", "background_blur", "card_stretch", "rarity_frames", "tower_troop",
             "clan_emblem", "show_crowns", "war_trophies", "level_advantage", "ultimate_champion",
             "ultimate_champion_league"}
    unknown = sorted(set(im) - known)
    if unknown:
        sys.exit(f'config.json "image" has unknown option(s) {unknown}. Allowed: {sorted(known)}')
    if im.get("background"):
        crimg.BG_PATH = str(im["background"])
    if "background_dim" in im:
        crimg.BG_DIM = float(im["background_dim"])
    if "background_blur" in im:
        crimg.BG_BLUR = float(im["background_blur"])
    if "card_stretch" in im:
        crimg.CARD_STRETCH = float(im["card_stretch"])
    if "rarity_frames" in im:
        crimg.RARITY_FRAMES = bool(im["rarity_frames"])
    if "tower_troop" in im:
        crimg.SHOW_TOWER_TROOP = bool(im["tower_troop"])
    if "clan_emblem" in im:
        crimg.SHOW_EMBLEM = bool(im["clan_emblem"])
    if "show_crowns" in im:
        crimg.SHOW_CROWNS = bool(im["show_crowns"])
    if "level_advantage" in im:
        crimg.SHOW_LEVEL_ADVANTAGE = bool(im["level_advantage"])
    if "ultimate_champion" in im:
        crimg.SHOW_ULTIMATE = bool(im["ultimate_champion"])
    if "ultimate_champion_league" in im:
        crimg.UC_LEAGUE = int(im["ultimate_champion_league"])
    if "war_trophies" in im:
        wt = str(im["war_trophies"]).lower()
        if wt not in ("war", "always", "never"):
            sys.exit('"image.war_trophies" must be war, always or never')
        crimg.SHOW_WAR_TROPHIES = wt


def write_summary(run):
    """Shows a per-watch table on the GitHub Actions run page (no need to open the log)."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path or not run.summary:
        return
    rows = ["### ja's watcher", "", "| watch | battles read | matching | posted | note |", "|---|---|---|---|---|"]
    for r in run.summary:
        rows.append(f"| {r['watch']} | {r['read']} | {r['matching']} | {r['posted']} | {str(r['note']).replace('|', '/')} |")
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(rows) + "\n")


def main():
    ap = argparse.ArgumentParser(description="ja's watcher - battle images to Discord")
    ap.add_argument("--config", default=str(HERE / "config.json"))
    ap.add_argument("--state", default=str(HERE / "state.json"))
    ap.add_argument("--key", default=os.environ.get("CR_API_KEY"))
    ap.add_argument("--loop", type=int, default=0, help="seconds between passes (0 = single pass)")
    ap.add_argument("--dry-run", action="store_true", help="write images to ./dry_run, post nothing, save no state")
    ap.add_argument("--backfill", type=int, default=0,
                    help="post N different layouts to EVERY watch now (boat, 1v1, duel 2-0, duel 2-1...), even if posted before")
    a = ap.parse_args()
    if not a.key:
        sys.exit("Set CR_API_KEY")
    cfg, watches = load_config(a.config)
    if not a.dry_run:
        missing = [w["webhook"] for w in watches if not resolve_webhook(w["webhook"])]
        if missing:
            sys.exit("These webhook secret names have no value: " + ", ".join(sorted(set(missing))) +
                     " - add them under Settings -> Secrets and variables -> Actions.")

    base = os.environ.get("CR_API_BASE") or cfg.get("api_base") or DEFAULT_BASE
    crimg.API_BASE = base
    crimg.setup_fonts(cfg.get("font"))
    crimg.DUEL_SPLIT = str(cfg.get("duel_images", "combined")).lower() == "split"
    crimg.apply_layout()
    if cfg.get("credit") is not None:
        crimg.CREDIT = cfg["credit"]
    if cfg.get("credit_color"):
        crimg.CREDIT_COLOR = crimg.parse_color(cfg["credit_color"])
    if cfg.get("credit_name_color"):
        crimg.CREDIT_NAME_COLOR = crimg.parse_color(cfg["credit_name_color"])
    apply_image_options(cfg.get("image") or {})
    assets = HERE / str(cfg.get("assets_dir") or "assets")
    if assets.is_dir():
        crimg.ASSETS_DIR = str(assets)
    run = Run(cfg, Api(a.key, base), a.dry_run, a.backfill, a.state)

    while True:
        try:
            run.start_pass(watches)
        except RuntimeError as e:
            log("ERROR:", e); sys.exit(1)
        for w in watches:
            try:
                run.process(w)
            except RuntimeError as e:
                log("ERROR:", e); sys.exit(1)
            except Exception as e:
                log(f"[{w['label']}] unexpected error: {e!r}"); run.problems += 1
        run.flush_missing()
        if not a.dry_run:
            save_state(a.state, run.state)
        write_summary(run)
        if not a.loop:
            break
        run.problems = 0
        run.summary = []
        time.sleep(a.loop)
    if run.problems:
        sys.exit(1)                      # makes the GitHub Actions run show red when something went wrong


if __name__ == "__main__":
    main()
