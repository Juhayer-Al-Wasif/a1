# Clan war logs -> Discord (free)

Posts an image of every new clan-war battle (1v1, duels, boat battles) from **two clans into two Discord channels**.
Nothing here costs money: Discord webhooks, the Clash Royale API, the RoyaleAPI proxy and GitHub Actions are all free.

## One-time setup (~15 min)

**1. Two Discord webhooks** - for each channel: *Channel settings -> Integrations -> Webhooks -> New Webhook -> Copy
Webhook URL*. Keep both URLs private (anyone with one can post in that channel).

**2. A Clash Royale API key that works from GitHub** - GitHub's servers change IP, but API keys are tied to IPs. The
free RoyaleAPI proxy solves that: at https://developer.clashroyale.com create a **new key** and allow the IP
`45.79.218.79`. (Delete any key you ever pasted in a chat or committed anywhere.)

**3. A GitHub repo** - create a **public** repo (public repos get free, unlimited Actions minutes) and upload every
file in this folder, including the hidden `.github/workflows/war-logs.yml`.

**4. Secrets** - repo *Settings -> Secrets and variables -> Actions -> New repository secret*:

| Secret | Value |
|---|---|
| `CR_API_KEY` | the key from step 2 |
| `DISCORD_WEBHOOK_CLAN1` | webhook URL for clan 1's channel |
| `DISCORD_WEBHOOK_CLAN2` | webhook URL for clan 2's channel |

**5. Config** - copy `config.example.json` to `config.json`, put your two clan tags and names in, commit it. (Clan
tags aren't secret. The webhooks and key live only in Secrets, never in the repo.)

**6. Run it** - *Actions* tab -> enable workflows -> *War logs* -> *Run workflow*. The first pass for each clan only
records the war battles that already exist (no spam). To see a post right away, run it once with
`backfill = 2`. After that it runs every ~10 minutes by itself.

## Training Day vs Battle Day
The battle log has no such field. The clan's live `/currentriverrace` does (`periodType`: `training`, `warDay`,
`colosseum`, plus `periodIndex`), so each posted image is labelled **Training Day 1-3 / Battle Day 1-4 / Colosseum Day**
from that. It is only added when the period did not change since the previous pass and the battle is recent
(under 3 h old) - otherwise the label is left off rather than guessed. Whether training-day battles show up in the
battle log at all isn't documented, so check on the next training day.

## Things worth knowing
* **Cadence/limits:** GitHub's scheduler can start a run several minutes late. If you make the repo *private* you get
  a monthly minutes allowance instead of unlimited - then change the cron to hourly (`0 * * * *`). Check GitHub's
  current pricing page for the numbers.
* GitHub may pause scheduled workflows after a long stretch without repo activity; if it does, re-enable it in the
  Actions tab.
* **Cost per pass** is tiny: 2 calls per clan + one battle-log call per player whose war counters changed.
* Use another host instead? Any always-on PC / Raspberry Pi / free VM works: `python cr_war_discord.py --loop 300`
  (the proxy means the host's IP doesn't matter).
* **Test without posting:** `python cr_war_discord.py --dry-run --backfill 2` writes images to `./dry_run`.
* Drop official art (Supercell Fan Kit downloads etc.) into an `assets/` folder next to the script to use it: icons by
  file name (`crown blue`, `elixir`, ...), card art as `Card Name.png`, `Card Name Hero.png`.
* Image look options live in `cr_battlelog_image.py` (e.g. `SHOW_CROWNS`, the clan emblem is off by default).
