#!/usr/bin/env python3
"""
Post clan-war battle images to Discord: one webhook (= one channel) per clan. Free to run.

    export CR_API_KEY="..."                 # key whitelisted for 45.79.218.79 when using the RoyaleAPI proxy
    export DISCORD_WEBHOOK_CLAN1="https://discord.com/api/webhooks/..."
    export DISCORD_WEBHOOK_CLAN2="https://discord.com/api/webhooks/..."

    python cr_war_discord.py                          # one pass (what GitHub Actions runs)
    python cr_war_discord.py --loop 300               # keep running on a PC / Raspberry Pi, every 5 min
    python cr_war_discord.py --dry-run --backfill 2   # render the 2 latest war battles per clan to ./dry_run

How it works (per clan, per pass)
  1. GET /clans/{tag}/members and /currentriverrace   (2 calls)
  2. only players whose war counters (decks used / fame / boat attacks) or lastSeen changed get their battle log
     fetched - so a pass costs a handful of API calls, not 50
  3. every NEW war battle (riverRacePvP, riverRaceDuel[Colosseum], boatBattle) is rendered as an image and posted
  4. first pass for a clan only records what already exists (no spam); --backfill N posts the N newest instead

Training day vs battle day: the battle log does not say. The clan's live /currentriverrace `periodType`
(training / warDay / colosseum) does, so each battle is labelled with the period seen on this pass - but only when
the period did not change since the previous pass and the battle is recent. Otherwise it is left unlabelled
rather than guessed.
"""
import argparse, json, os, sys, tempfile, time
from datetime import datetime, timezone
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import cr_battlelog_image as crimg  # noqa: E402

DEFAULT_BASE = "https://proxy.royaleapi.dev/v1"
KEEP_KEYS = 2500                 # remembered battle keys per clan
LABEL_MAX_AGE_S = 3 * 3600       # only label a battle's day if it is this recent


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
        return json.loads(Path(path).read_text())
    except Exception:
        return {"clans": {}}


def save_state(path, st):
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(st, indent=1, sort_keys=True))
    tmp.replace(path)


# ----------------------------------------------------------------------- discord
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
        self.state = {"clans": {}} if dry else load_state(state_path)
        self.meta = None
        self.tmp = Path(tempfile.mkdtemp(prefix="crwar_"))

    def cards_meta(self):
        if self.meta is None:
            try:
                self.meta = crimg.get_cards_meta(self.api.key)
            except SystemExit as e:
                raise RuntimeError(f"card catalog request failed: {e}")
        return self.meta

    def render(self, battle, label_period):
        crimg.enrich_cards(battle, self.cards_meta())
        if label_period:
            battle["_period"] = label_period
        out = self.tmp / "battle.png"
        if not crimg.render_battle(battle, out, self.cfg.get("timezone", "America/New_York")):
            return None
        return out.read_bytes()

    def process_clan(self, clan):
        tag = norm_tag(clan["tag"])
        name = clan.get("name") or tag
        cs = self.state["clans"].setdefault(tag, {"initialized": False, "sig": {}, "seen": {}, "posted": [],
                                                  "recheck": [], "period_index": None})
        posted = set(cs["posted"])
        st, mem = self.api.get(f"/clans/{enc(tag)}/members?limit=60")
        if not mem:
            log(f"[{name}] members request failed ({st}); skipping this pass"); return
        st, rr = self.api.get(f"/clans/{enc(tag)}/currentriverrace")      # 404 between seasons is normal
        period = ({"type": rr.get("periodType"), "index": rr.get("periodIndex")} if rr else None)
        parts = {p["tag"]: p for p in ((rr or {}).get("clan") or {}).get("participants", [])}
        first = not cs["initialized"]
        recheck = set(cs.get("recheck", []))
        fetched, moved_now, found = [], [], []
        skipped_other = 0

        for m in mem.get("items", []):
            ptag = m["tag"]
            p = parts.get(ptag, {})
            sig = [p.get("decksUsed"), p.get("decksUsedToday"), p.get("fame"), p.get("boatAttacks")]
            moved = first or sig != cs["sig"].get(ptag) or m.get("lastSeen") != cs["seen"].get(ptag)
            cs["sig"][ptag], cs["seen"][ptag] = sig, m.get("lastSeen")
            if not (moved or ptag in recheck):
                continue
            time.sleep(0.2)
            st, blog = self.api.get(f"/players/{enc(ptag)}/battlelog")
            if not blog:
                continue
            fetched.append(ptag)
            if moved:
                moved_now.append(ptag)      # only real changes earn a second look next pass
            for b in blog:
                if not crimg.is_war(b):
                    continue
                played_for = (((b.get("team") or [{}])[0]).get("clan") or {}).get("tag")
                if played_for and norm_tag(played_for) != tag:
                    skipped_other += 1          # e.g. a new member's war battles from his PREVIOUS clan
                    continue
                found.append((f"{ptag}|{b['battleTime']}", b))

        new = sorted([x for x in found if x[0] not in posted], key=lambda x: x[1]["battleTime"])
        to_post = new
        if first:                                   # baseline: remember everything, post only --backfill N
            to_post = new[-self.backfill:] if self.backfill > 0 else []
            for key, _ in new:
                posted.add(key)
            log(f"[{name}] first pass: {len(new)} existing war battles recorded, posting {len(to_post)}")

        same_period = bool(period) and cs.get("period_index") == period["index"]
        sent = 0
        for key, b in to_post:
            age = (datetime.now(timezone.utc) - parse_bt(b["battleTime"])).total_seconds()
            label = period if (same_period and age <= LABEL_MAX_AGE_S) else None
            try:
                png = self.render(b, label)
            except Exception as e:
                log(f"[{name}] render failed for {key}: {e!r}"); posted.add(key); continue
            if png is None:
                posted.add(key); continue
            fname = f"war_{b['battleTime']}.png"
            if self.dry:
                d = HERE / "dry_run"; d.mkdir(exist_ok=True); (d / f"{name}_{fname}".replace(" ", "_")).write_bytes(png)
                log(f"[{name}] dry-run image written: {fname}"); continue
            try:
                post_webhook(clan["_webhook"], png, fname, self.embed(b), name)
            except Exception as e:
                log(f"[{name}] Discord post failed for {key}: {e!r} - will retry next pass"); continue
            posted.add(key); sent += 1
            cs["posted"] = list(dict.fromkeys(cs["posted"] + [key]))[-KEEP_KEYS:]
            save_state(self.state_path, self.state)
            time.sleep(1.2)

        # keep memory bounded, remember what happened this pass
        merged = list(dict.fromkeys(cs["posted"] + sorted(posted)))
        cs["posted"] = merged[-KEEP_KEYS:]
        cs["initialized"] = True
        cs["recheck"] = moved_now                    # the API can lag a few seconds: look at them once more next pass
        if period:
            cs["period_index"] = period["index"]
        log(f"[{name}] pass done: checked {len(fetched)} players, new war battles {len(new)}, posted {sent}"
            + (f", ignored {skipped_other} played for another clan" if skipped_other else "")
            + (f", period {period['type']}#{period['index']}" if period else ", no active race"))

    def embed(self, b):
        me = (b.get("team") or [{}])[0]
        op = (b.get("opponent") or [{}])[0]
        res = {1: "✅ Victory", -1: "❌ Defeat", 0: "➖ Draw"}.get(b.get("_outcome", 0))
        n = max(len(b.get("team", [])), 1)
        return {"title": f"{me.get('name', '?')} vs {op.get('name', '?')}"[:256],
                "description": f"**{res}** · {crimg.mode_label(b, n)}"[:2000],
                "color": {1: 0x2ECC71, -1: 0xE74C3C, 0: 0x95A5A6}.get(b.get("_outcome", 0)),
                "timestamp": parse_bt(b["battleTime"]).isoformat()}


def main():
    ap = argparse.ArgumentParser(description="Post clan war battle images to Discord")
    ap.add_argument("--config", default=str(HERE / "config.json"))
    ap.add_argument("--state", default=str(HERE / "state.json"))
    ap.add_argument("--key", default=os.environ.get("CR_API_KEY"))
    ap.add_argument("--loop", type=int, default=0, help="seconds between passes (0 = single pass)")
    ap.add_argument("--dry-run", action="store_true", help="write images to ./dry_run, post nothing, save no state")
    ap.add_argument("--backfill", type=int, default=0, help="on a clan's first pass, post its N newest war battles")
    a = ap.parse_args()
    if not a.key:
        sys.exit("Set CR_API_KEY")
    cfg = json.loads(Path(a.config).read_text(encoding="utf-8"))
    clans = cfg["clans"]
    for c in clans:
        c["_webhook"] = os.environ.get(c.get("webhook_env", "")) or c.get("webhook")
        if not c["_webhook"] and not a.dry_run:
            sys.exit(f"No webhook for clan {c.get('name') or c['tag']}: set the environment variable "
                     f"{c.get('webhook_env', '<webhook_env missing in config>')}")

    base = os.environ.get("CR_API_BASE") or cfg.get("api_base") or DEFAULT_BASE
    crimg.API_BASE = base
    crimg.setup_fonts()
    crimg.apply_layout()
    if (HERE / "assets").is_dir():
        crimg.ASSETS_DIR = str(HERE / "assets")
    run = Run(cfg, Api(a.key, base), a.dry_run, a.backfill, a.state)

    while True:
        for c in clans:
            try:
                run.process_clan(c)
            except RuntimeError as e:        # 403 etc: tell the user loudly, other clans would fail the same way
                log("ERROR:", e); sys.exit(1)
            except Exception as e:
                log(f"[{c.get('name') or c['tag']}] unexpected error: {e!r}")
        if not a.dry_run:
            save_state(a.state, run.state)
        if not a.loop:
            break
        time.sleep(a.loop)


if __name__ == "__main__":
    main()
