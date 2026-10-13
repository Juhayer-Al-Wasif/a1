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
    python cr_war_discord.py --dry-run --backfill 2   # write the 2 newest matching battles to ./dry_run

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
def load_config(path):
    cfg = json.loads(Path(path).read_text(encoding="utf-8"))
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


def result_of(b):
    """+1 win / -1 loss / 0 draw-or-unknown. Duels are decided by rounds won, like the image shows."""
    team, opp = b.get("team") or [], b.get("opponent") or []
    if len(team) == 1 and len(opp) == 1:
        rt, ro = crimg.get_rounds(team[0]), crimg.get_rounds(opp[0])
        rc = min(len(rt), len(ro))
        if rc >= 1 and (str(b.get("type", "")).lower().startswith("riverraceduel") or rc > 1):
            views = [(crimg.round_view(team[0], x), crimg.round_view(opp[0], y)) for x, y in zip(rt[:rc], ro[:rc])]
            for a, c in views:
                crimg.fix_king_hp([a], [c]); crimg.fix_king_hp([c], [a])
            w = [crimg.round_winner(a, c) for a, c in views]
            return (w.count(1) > w.count(-1)) - (w.count(1) < w.count(-1))
    return crimg.battle_outcome(b)


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


def post_webhook(url, png, filename, embed, username):
    embed = dict(embed, image={"url": f"attachment://{filename}"})
    payload = {"username": username[:80], "embeds": [embed], "allowed_mentions": {"parse": []}}
    for attempt in range(3):
        r = requests.post(url + ("&" if "?" in url else "?") + "wait=true",
                          data={"payload_json": json.dumps(payload)},
                          files={"files[0]": (filename, png, "image/png")}, timeout=60)
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
        self.tmp = Path(tempfile.mkdtemp(prefix="crwatch_"))

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
        crimg.enrich_cards(battle, self.cards_meta())
        if period:
            battle["_period"] = period
        out = self.tmp / "battle.png"
        if not crimg.render_battle(battle, out, self.cfg.get("timezone", "America/New_York")):
            return None
        return out.read_bytes()

    def embed(self, b):
        res = {1: ("✅ Victory", 0x2ECC71), -1: ("❌ Defeat", 0xE74C3C), 0: ("➖ Draw", 0x95A5A6)}[result_of(b)]
        n = max(len(b.get("team") or []), 1)
        label = crimg.mode_label(b, n)
        return {"title": f"{res[0]} · {label}"[:256],
                "description": (side_text(b.get("team"), "🟦") + "\n" + side_text(b.get("opponent"), "🟥"))[:4000],
                "color": res[1], "timestamp": parse_bt(b["battleTime"]).isoformat()}

    # ---- collecting battles
    def collect_player(self, w):
        st, blog = self.api.get(f"/players/{enc(w['tag'])}/battlelog")
        if blog is None:
            log(f"[{w['label']}] battle log request failed ({st})"); return None
        return [(f"{w['tag']}|{b['battleTime']}", b) for b in blog]

    def collect_clan(self, w, ws, first):
        tag = w["tag"]
        st, mem = self.api.get(f"/clans/{enc(tag)}/members?limit=60")
        if not mem:
            log(f"[{w['label']}] members request failed ({st}); skipping this pass"); return None
        rr = self.rr.get(tag) or {}
        parts = {p["tag"]: p for p in (rr.get("clan") or {}).get("participants", [])}
        war_only = all(m in WAR_MODES for m in w["modes"])
        recheck = set(ws.get("recheck", []))
        found, moved_now, fetched, skipped = [], [], 0, 0
        for m in mem.get("items", []):
            ptag = m["tag"]
            p = parts.get(ptag, {})
            sig = [p.get("decksUsed"), p.get("decksUsedToday"), p.get("fame"), p.get("boatAttacks")]
            moved = first or sig != ws["sig"].get(ptag) or m.get("lastSeen") != ws["seen"].get(ptag)
            ws["sig"][ptag], ws["seen"][ptag] = sig, m.get("lastSeen")
            # war-only watches fetch just the players whose war counters moved; other modes need everyone
            if war_only and not (moved or ptag in recheck):
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
                if played_for and norm_tag(played_for) != tag:
                    skipped += 1                    # e.g. a new member's battles from his PREVIOUS clan
                    continue
                found.append((f"{ptag}|{b['battleTime']}", b))
        ws["recheck"] = moved_now if war_only else []
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
            return
        if w["kind"] == "player":
            w["_stats"] = "1 battle log"
        posted = set(ws["posted"])

        def wanted(b):
            if not matches_modes(b, w["modes"]):
                return False
            r = result_of(b)
            return w["results"] == "all" or (w["results"] == "win" and r == 1) or (w["results"] == "loss" and r == -1)

        candidates = sorted([x for x in found if wanted(x[1]) and x[0] not in posted],
                            key=lambda x: x[1]["battleTime"])
        if first:                                   # baseline: remember everything, post only --backfill N
            for key, _ in found:
                posted.add(key)
            candidates = candidates[-self.backfill:] if self.backfill > 0 else []
            log(f"[{w['label']}] first pass: {len(found)} existing battles recorded, posting {len(candidates)}")

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
                png = self.render(b, period)
            except Exception as e:
                log(f"[{w['label']}] render failed for {key}: {e!r}"); posted.add(key); continue
            if png is None:
                posted.add(key); continue
            fname = f"battle_{b['battleTime']}.png"
            if self.dry:
                d = HERE / "dry_run"; d.mkdir(exist_ok=True)
                (d / re.sub(r"[^A-Za-z0-9_.-]", "_", f"{w['label']}_{fname}")).write_bytes(png)
                log(f"[{w['label']}] dry-run image written: {fname}"); continue
            try:
                post_webhook(hook, png, fname, self.embed(b), w.get("bot_name") or self.cfg.get("bot_name") or DEFAULT_BOT)
            except Exception as e:
                log(f"[{w['label']}] Discord post failed for {key}: {e!r} - will retry next pass"); continue
            posted.add(key); sent += 1
            ws["posted"] = list(dict.fromkeys(ws["posted"] + [key]))[-KEEP_KEYS:]
            save_state(self.state_path, self.state)
            time.sleep(1.2)

        ws["posted"] = list(dict.fromkeys(ws["posted"] + sorted(posted)))[-KEEP_KEYS:]
        ws["initialized"] = True
        log(f"[{w['label']}] pass done: {w.pop('_stats', '')}, new matching battles {len(candidates)}, posted {sent}"
            + (f", held back {held} (day unknown yet)" if held else ""))


def main():
    ap = argparse.ArgumentParser(description="ja's watcher - battle images to Discord")
    ap.add_argument("--config", default=str(HERE / "config.json"))
    ap.add_argument("--state", default=str(HERE / "state.json"))
    ap.add_argument("--key", default=os.environ.get("CR_API_KEY"))
    ap.add_argument("--loop", type=int, default=0, help="seconds between passes (0 = single pass)")
    ap.add_argument("--dry-run", action="store_true", help="write images to ./dry_run, post nothing, save no state")
    ap.add_argument("--backfill", type=int, default=0, help="on a watch's first pass, post its N newest matching battles")
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
    crimg.setup_fonts()
    crimg.apply_layout()
    if cfg.get("credit") is not None:
        crimg.CREDIT = cfg["credit"]
    if (HERE / "assets").is_dir():
        crimg.ASSETS_DIR = str(HERE / "assets")
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
                log(f"[{w['label']}] unexpected error: {e!r}")
        if not a.dry_run:
            save_state(a.state, run.state)
        if not a.loop:
            break
        time.sleep(a.loop)


if __name__ == "__main__":
    main()
