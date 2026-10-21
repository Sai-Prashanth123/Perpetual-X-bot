"""Boot-time reconciler for the agency bot (runs as the hermes user before the gateway starts).

The clients table in Postgres is the source of truth (clients are added by chatting with
Perpetual; clients.yaml is only an optional seed). On every boot — and again, with
--from-runtime, whenever a client is added or changed by chat — this:
  * applies the database schema and syncs the client roster into Postgres,
  * sets up the default profile as the HQ agent (#hq) holding the one shared Slack bot,
  * creates/refreshes one Hermes profile per client, routed from its Slack channel,
  * (re)registers each profile's cron jobs when the schedule version or settings change.

Learned state is never overwritten: client-voice, playbook, agency-playbook, memories,
sessions, the database. Idempotent — safe to run on every boot.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

sys.path.append("/opt/perpetual/pylib")

from ruamel.yaml import YAML  # noqa: E402  (ships with Hermes)

from perpetual_core import agency, ledger  # noqa: E402
from perpetual_core.settings import profile_slug  # noqa: E402

DATA = Path(os.environ.get("HERMES_HOME", "/opt/data"))
SRC = Path("/opt/perpetual")
TEMPLATE = SRC / "hermes-template"
CRON_VERSION = (SRC / "CRON_VERSION").read_text().strip()
INTRO_VERSION = "v5"        # bump to have every client agent re-introduce itself (how it works changed)
SHARED_SKILLS = DATA / "shared-skills"
# Private client voice packs copied onto the server (scripts/build_voice_import.py); never in git.
IMPORTS = DATA / "imports"
# Clients without a Slack channel are parked here: their voice, memory and history stay on disk,
# but Hermes doesn't load them (every live profile costs memory on a small server).
PARKED = DATA / "profiles-parked"
LEARNED = {"client-voice", "playbook"}
PY = "/opt/hermes/.venv/bin/python"

yaml = YAML()
yaml.preserve_quotes = True


def log(msg):
    print(f"[perpetual] {msg}", flush=True)


def _dotenv(path):
    out = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"')
    return out


_SAVED = _dotenv(DATA / ".env")   # what the last boot wrote: the fallback for a --from-runtime run


def env(name, default=None, required=False):
    v = os.environ.get(name) or _SAVED.get(name) or default
    if required and not v:
        log(f"ERROR: {name} is not set — cannot start")
        sys.exit(1)
    return v


def hermes(*args, profile=None, check=True):
    cmd = ["hermes"] + (["-p", profile] if profile else []) + list(args)
    r = subprocess.run(cmd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:6])}… failed: {r.stderr.strip()[-400:]}")
    return r.stdout


# ---------------------------------------------------------------- file helpers

def write_env(home, values):
    """Merge KEY=VALUE pairs into a profile's .env (Hermes reads it into the profile's secret scope)."""
    path = home / ".env"
    lines, seen = [], set()
    if path.exists():
        for line in path.read_text().splitlines():
            key = line.split("=", 1)[0].strip() if "=" in line else None
            if key in values:
                if values[key] is not None:
                    lines.append(f"{key}={values[key]}")
                seen.add(key)
            else:
                lines.append(line)
    for k, v in values.items():
        if k not in seen and v is not None:
            lines.append(f"{k}={v}")
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o600)


def edit_config(home, changes):
    """Set dotted keys in a profile's config.yaml, keeping comments and everything else."""
    path = home / "config.yaml"
    doc = yaml.load(path.read_text()) if path.exists() else {}
    doc = doc or {}
    for dotted, value in changes.items():
        node = doc
        parts = dotted.split(".")
        for p in parts[:-1]:
            if not isinstance(node.get(p), dict):
                node[p] = {}
            node = node[p]
        node[parts[-1]] = value
    with path.open("w") as f:
        yaml.dump(doc, f)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def seed_skills(src_dir, dest_dir, learned=()):
    """Refresh procedural skills unless the agent improved them since the last seed; seed learned ones once."""
    for skill in sorted(p for p in src_dir.iterdir() if p.is_dir()):
        dest = dest_dir / skill.name
        src_file, dest_file, stamp = skill / "SKILL.md", dest / "SKILL.md", dest / ".seeded.sha"
        dest.mkdir(parents=True, exist_ok=True)
        if skill.name in learned:
            if not dest_file.exists():
                shutil.copy(src_file, dest_file)
            continue
        untouched = stamp.exists() and dest_file.exists() and sha(dest_file) == stamp.read_text().strip()
        if not dest_file.exists() or untouched or env("PX_RESEED_SKILLS") == "1":
            shutil.copy(src_file, dest_file)
            stamp.write_text(sha(src_file))
        else:
            log(f"keeping agent-edited skill {dest_dir.parent.parent.name}/{skill.name}")


def install_plugin(home, name):
    dest = home / "plugins" / name
    shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(TEMPLATE / "plugins" / name, dest, ignore=shutil.ignore_patterns("__pycache__"))


def write_script(home, name, args):
    scripts = home / "scripts"
    scripts.mkdir(exist_ok=True)
    (scripts / name).write_text(
        "import sys\nsys.path.append('/opt/perpetual/pylib')\n"
        "from perpetual_core.jobs import main\n"
        f"sys.exit(main({args!r}))\n")


# ---------------------------------------------------------------- cron

def reconcile_cron(home, profile, jobs, fingerprint):
    """Recreate this profile's px-* jobs when the fingerprint changed; otherwise leave them alone."""
    stamp = home / ".perpetual-cron"
    if stamp.exists() and stamp.read_text() == fingerprint:
        return False
    listing = hermes("cron", "list", "--all", profile=profile, check=False)
    for name in sorted({w for w in listing.split() if w.startswith("px-")}):
        hermes("cron", "remove", name, profile=profile, check=False)
    for job in jobs:
        prompt = [job["prompt"]] if job["prompt"] else []
        hermes("cron", "create", job["schedule"], *prompt, "--name", job["name"], *job["args"], profile=profile)
    stamp.write_text(fingerprint)
    return True


def skill_args(*skills):
    out = []
    for s in skills:
        out += ["--skill", s]
    return out


# ---------------------------------------------------------------- main

def tavily_key():
    """Web research key: from TAVILY_API_KEYS (comma-separated, several free accounts) pick the one
    with the most searches left this month; TAVILY_API_KEY alone also works."""
    keys = [k.strip() for k in (env("TAVILY_API_KEYS") or "").split(",") if k.strip()]
    if env("TAVILY_API_KEY") and env("TAVILY_API_KEY") not in keys:
        keys.append(env("TAVILY_API_KEY"))
    best, best_left = (keys[0] if keys else None), -1
    for key in keys:
        try:
            req = urllib.request.Request("https://api.tavily.com/usage", headers={"Authorization": f"Bearer {key}"})
            with urllib.request.urlopen(req, timeout=15) as r:
                acct = json.loads(r.read()).get("account") or {}
            left = (acct.get("plan_limit") or 0) - (acct.get("plan_usage") or 0)
        except Exception:  # noqa: BLE001 — an unreachable or revoked key just isn't picked
            continue
        if left > best_left:
            best, best_left = key, left
    if keys:
        log(f"web research: {len(keys)} Tavily key(s), using one with {max(best_left, 0)} searches left")
    return best


def main(runtime=False):
    shared = {
        "ANTHROPIC_API_KEY": env("ANTHROPIC_API_KEY", required=True),
        "XCONSOLE_API_KEY": env("XCONSOLE_API_KEY", required=True),
        "XCONSOLE_URL": env("XCONSOLE_URL", "https://scraping.thought-pilot.co/api"),
        "DATABASE_URL": env("DATABASE_URL", required=True),
        "AGENCY_WEB_URL": env("AGENCY_WEB_URL") or (f"http://{env('AGENCY_WEB_HOSTPORT')}" if env("AGENCY_WEB_HOSTPORT") else None),
        "INTERNAL_TOKEN": env("INTERNAL_TOKEN"),
        # One-tap posting to X happens inside the bot (Slack "Post now" and /post).
        "TOKEN_ENCRYPTION_KEY": env("TOKEN_ENCRYPTION_KEY"),
        "X_CLIENT_ID": env("X_CLIENT_ID"),
        "X_CLIENT_SECRET": env("X_CLIENT_SECRET"),
        "OWNER_SLACK_IDS": env("OWNER_SLACK_IDS"),
        # Small server: run scheduled jobs one at a time instead of piling them into memory.
        "HERMES_CRON_MAX_PARALLEL": env("HERMES_CRON_MAX_PARALLEL", "1"),
        "TAVILY_API_KEY": tavily_key(),
    }
    model = env("PX_MODEL", "claude-sonnet-5-5")
    learn_model = env("PX_LEARN_MODEL", "claude-opus-5-5")
    hq_channel = env("HQ_SLACK_CHANNEL")
    agency_tz = env("AGENCY_TIMEZONE", "UTC")

    roster_file = Path(env("PX_CLIENTS_FILE", str(SRC / "clients.yaml")))
    seed = ((yaml.load(roster_file.read_text()) or {}).get("clients") or []) if roster_file.exists() else []
    seed = [dict(c) for c in seed]

    # Scripts run outside any profile scope; give them the shared settings.
    shared_env = DATA / "perpetual-shared.env"
    script_env = {k: shared.get(k) for k in ("DATABASE_URL", "XCONSOLE_API_KEY", "XCONSOLE_URL", "OWNER_SLACK_IDS")}
    script_env["SLACK_BOT_TOKEN"] = env("SLACK_BOT_TOKEN")      # jobs that DM receipts
    shared_env.write_text("".join(f"{k}={v}\n" for k, v in script_env.items() if v))
    shared_env.chmod(0o600)

    conn = connect_with_retry()
    ledger.ensure_schema(conn)
    imported = _imported_clients()
    if imported:
        added = agency.seed_missing_clients(conn, imported)
        if added:
            log(f"imported {len(added)} new client(s): {', '.join(added)}")
        history = agency.apply_import_extras(conn, imported)
        if history:
            log(f"imported {history} published/drafted pieces into content history")
    if seed and not runtime:
        agency.sync_clients(conn, seed)
    clients = [{"handle": r["handle"], "name": r["display_name"], "slack_channel": r["slack_channel"],
                "employee": r["employee_slack"], "timezone": r["timezone"], "niche_topics": r["niche_topics"],
                "watch_accounts": r["watch_accounts"], "avoid_accounts": r["avoid_accounts"],
                "team": r["company"], "package": r["package"]}
               for r in agency.active_clients(conn)]
    log(f"{len(clients)} active client(s)")

    # ---- shared agency playbook (written by HQ, read by every client)
    seed_skills(TEMPLATE / "shared-skills", SHARED_SKILLS, learned={"agency-playbook"})
    for pack in sorted((IMPORTS / "_shared").glob("*/")) if (IMPORTS / "_shared").is_dir() else []:
        shutil.copytree(pack, SHARED_SKILLS / pack.name, dirs_exist_ok=True)   # house standards: always current

    # ---- clients
    routes, channels, needs_backfill = [], [], []
    for c in clients:
        handle = c["handle"].lstrip("@").lower()
        slug = profile_slug(handle)
        channel = c.get("slack_channel")
        home = DATA / "profiles" / slug
        parked = PARKED / slug
        if not channel:
            if home.exists():
                reconcile_cron(home, slug, [], json.dumps([CRON_VERSION, "parked"]))   # stop its jobs first
                PARKED.mkdir(exist_ok=True)
                if parked.exists():
                    shutil.rmtree(parked)
                shutil.move(str(home), str(parked))
                log(f"@{handle}: no Slack channel yet — parked (voice and history kept, not loaded)")
            if not conn.execute("SELECT 1 FROM metrics_daily WHERE client = %s LIMIT 1", (handle,)).fetchone():
                needs_backfill.append(handle)
            continue
        if not home.exists() and parked.exists():
            shutil.move(str(parked), str(home))
            log(f"@{handle}: has a channel now — unparked")
        if not home.exists():
            log(f"creating profile {slug} for @{handle}")
            hermes("profile", "create", slug, "--no-alias", "--description", f"X strategist for @{handle}")
        install_plugin(home, "perpetual")
        name = c.get("name") or handle
        (home / "SOUL.md").write_text((TEMPLATE / "SOUL.md").read_text()
                                      .replace("{{CLIENT_NAME}}", name).replace("{{CLIENT_HANDLE}}", handle))
        skills_dir = home / "skills" / "perpetual-x"
        seed_skills(TEMPLATE / "skills" / "perpetual-x", skills_dir, learned=LEARNED)
        for skill in LEARNED:   # an imported voice pack replaces the blank starter, never a learned voice
            pack = IMPORTS / handle / skill
            dest = skills_dir / skill / "SKILL.md"
            if (pack / "SKILL.md").exists() and (not dest.exists() or "version: 0.0.0" in dest.read_text()):
                shutil.copytree(pack, skills_dir / skill, dirs_exist_ok=True)
                log(f"@{handle}: imported {skill} from the voice pack")
            elif (pack / "references").is_dir():
                # Newer imports bring extra reference files (claim bank, lint config): add the missing
                # ones to a learned voice without touching anything already there.
                dest_refs = skills_dir / skill / "references"
                for ref in (pack / "references").iterdir():
                    if ref.is_file() and not (dest_refs / ref.name).exists():
                        dest_refs.mkdir(parents=True, exist_ok=True)
                        shutil.copy(ref, dest_refs / ref.name)
        brief = skills_dir / "client-brief"
        brief.mkdir(parents=True, exist_ok=True)
        (brief / "SKILL.md").write_text(client_brief(c, handle, name))
        write_script(home, "px_requests.py", ["requests-pending", handle])
        write_script(home, "px_postmortem.py", ["postmortem-pending", handle])
        write_script(home, "px_slots.py", ["calendar-due", handle])
        # PX_SLACK_TOKEN, not SLACK_BOT_TOKEN: a profile holding SLACK_BOT_TOKEN would start its own
        # Slack connection for the same app (Hermes refuses duplicate credentials).
        write_env(home, {**shared, "CLIENT_HANDLE": handle, "PX_SLACK_TOKEN": env("SLACK_BOT_TOKEN")})
        tz = c.get("timezone") or "UTC"
        edit_config(home, {
            "model.provider": "anthropic", "model.default": model, "timezone": tz,
            "approvals.mode": "manual", "memory.memory_enabled": True,
            "plugins.enabled": ["perpetual"], "skills.external_dirs": [str(SHARED_SKILLS)],
            # One client, thought about deeply: high reasoning, and the web through Tavily.
            "agent.reasoning_effort": "high", "web.backend": "tavily",
        })
        deliver = f"slack:{channel}"
        routes.append({"name": f"x-{slug}", "platform": "slack", "chat_id": str(channel), "profile": slug})
        channels.append(str(channel))
        fp = json.dumps([CRON_VERSION, deliver, model, learn_model, tz, client_jobs_skills()])
        if reconcile_cron(home, slug, client_jobs(deliver, learn_model, offset=minute_offset(handle)), fp):
            log(f"registered cron jobs for @{handle} → {deliver}")
        voice = skills_dir / "client-voice" / "SKILL.md"
        intro_marker = home / ".perpetual-intro"
        voice_ready = "version: 0.0.0" not in voice.read_text()
        intro_key = f"{channel}:{INTRO_VERSION}"
        introduced_here = intro_marker.exists() and intro_marker.read_text().strip() == intro_key
        if voice_ready and not introduced_here:
            # Voice came from an import, so onboarding won't run: introduce the agent in its channel
            # (again whenever the way it works changes).
            hermes("cron", "remove", "px-intro", profile=slug, check=False)
            hermes("cron", "create", "2m",
                   "Post a short hello in this channel (plain message, no document): you are the dedicated X "
                   "strategist for this client only; you answer when asked here (or when Perpetual hands you "
                   "work) and otherwise send only a daily brief each morning and a weekly report on Mondays; "
                   "every answer arrives as one document with a short summary, never a pile of cards; you "
                   "research the client every few days (their numbers, competitors, the niche on the web). "
                   "Then 4 example asks as bullets: 'replies for my latest article', 'what's working and what "
                   "isn't this month?', 'how do we grow the profile faster?', 'make this week's package'. "
                   "End with how to react to drafts: ok R3 / edit R3: … / no R3 reason. Under 12 lines.",
                   "--name", "px-intro", "--deliver", deliver, "--repeat", "1",
                   *skill_args("client-brief", "client-voice"), profile=slug)
            intro_marker.write_text(intro_key)
            log(f"@{handle}: introduction scheduled in its channel")
        if "version: 0.0.0" in voice.read_text():
            hermes("cron", "remove", "px-onboarding", profile=slug, check=False)
            hermes("cron", "create", "2m", "Onboard this client: follow client-onboarding end to end.",
                   "--name", "px-onboarding", "--deliver", deliver, "--repeat", "1",
                   *skill_args("client-brief", "client-voice", "playbook", "agency-playbook", "client-onboarding"),
                   "--provider", "anthropic", "--model", learn_model, profile=slug)
            log(f"@{handle}: voice not learned yet — onboarding scheduled")
        if not conn.execute("SELECT 1 FROM metrics_daily WHERE client = %s LIMIT 1", (handle,)).fetchone():
            needs_backfill.append(handle)

    if needs_backfill:  # new clients: four weeks of history for the Home tab straight away
        log(f"backfilling 28 days of metrics for {', '.join(needs_backfill)}")
        run_job("snapshot-metrics", "--backfill", "28", background=runtime)

    # Retire profiles whose client was removed: stop their jobs, keep their history.
    active = {profile_slug(c["handle"]) for c in clients}
    for home in (DATA / "profiles").glob("*"):
        if home.is_dir() and home.name not in active and (home / ".perpetual-cron").exists():
            listing = hermes("cron", "list", "--all", profile=home.name, check=False)
            for name in sorted({w for w in listing.split() if w.startswith("px-")}):
                hermes("cron", "remove", name, profile=home.name, check=False)
            (home / ".perpetual-cron").unlink()
            log(f"retired profile {home.name} (no longer an active client)")

    # ---- HQ (default profile): holds the one Slack bot and routes client channels.
    install_plugin(DATA, "perpetual-hq")
    (DATA / "SOUL.md").write_text((TEMPLATE / "hq" / "SOUL.md").read_text())
    seed_skills(TEMPLATE / "hq" / "skills", DATA / "skills" / "perpetual-hq")
    write_script(DATA, "px_metrics.py", ["snapshot-metrics"])
    write_script(DATA, "px_viral.py", ["snapshot-viral"])
    write_script(DATA, "px_radar.py", ["radar-scan"])
    # The bot converses only in client channels (and DMs, which Slack never gates). The report
    # channel is deliberately left out, so messages there are ignored. An empty allowlist would
    # mean "every channel", hence the placeholder while there are no clients yet.
    write_env(DATA, {
        **shared,
        "SLACK_BOT_TOKEN": env("SLACK_BOT_TOKEN"), "SLACK_APP_TOKEN": env("SLACK_APP_TOKEN"),
        "SLACK_ALLOWED_USERS": env("SLACK_ALLOWED_USERS"), "SLACK_HOME_CHANNEL": hq_channel,
        "SLACK_ALLOWED_CHANNELS": ",".join(channels) or "CNONE",
        # Answer in client channels without needing an @mention.
        "SLACK_FREE_RESPONSE_CHANNELS": ",".join(channels) or "CNONE",
    })
    edit_config(DATA, {
        "model.provider": "anthropic", "model.default": model, "timezone": agency_tz,
        "approvals.mode": "manual", "plugins.enabled": ["perpetual-hq"], "web.backend": "tavily",
        "gateway.multiplex_profiles": True, "gateway.profile_routes": routes,
        # Adding a client restarts the gateway; don't announce restarts in the reports channel.
        "slack.gateway_restart_notification": False,
        # Reply in the conversation itself, like a chat, instead of opening a thread per message.
        "slack.reply_in_thread": False,
    })
    hq_deliver = f"slack:{hq_channel}" if hq_channel else "local"
    fp = json.dumps([CRON_VERSION, hq_deliver, model, learn_model, agency_tz])
    if reconcile_cron(DATA, None, hq_jobs(hq_deliver, learn_model), fp):
        log(f"registered HQ cron jobs → {hq_deliver}")
    if not runtime and not conn.execute("SELECT 1 FROM viral_feed LIMIT 1").fetchone():
        run_job("snapshot-viral")
    conn.close()
    log(f"ready: {len(clients)} client(s), {len(routes)} routed channel(s), reports → {hq_deliver}")
    if runtime:
        print(json.dumps({"clients": [c["handle"] for c in clients], "routes": len(routes)}))


def _imported_clients():
    path = IMPORTS / "clients.json"
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("clients") or []
    except ValueError:
        log("WARNING: imports/clients.json is not valid JSON; skipped")
        return []


def connect_with_retry(attempts=8):
    """The container's network can lag the boot by a few seconds; an external database
    (Neon, Supabase) shouldn't fail the whole start on the first try."""
    import time
    for i in range(1, attempts + 1):
        try:
            return ledger.connect(autocommit=True)
        except Exception as e:  # noqa: BLE001
            if i == attempts:
                raise
            log(f"database not reachable yet ({str(e).splitlines()[0][:120]}); retry {i}/{attempts - 1}")
            time.sleep(min(5 * i, 30))


def run_job(*args, background=False):
    cmd = [PY, "-m", "perpetual_core.jobs", *args]
    job_env = {**os.environ, "PYTHONPATH": "/opt/perpetual/pylib"}
    if background:
        subprocess.Popen(cmd, env=job_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    else:
        subprocess.run(cmd, env=job_env, check=False)


def package_line(pkg):
    pkg = pkg or {"article": 1, "long_tweet": 1, "article_qrts": True}
    if isinstance(pkg, str):
        pkg = json.loads(pkg)
    parts = [f"{n} {k.replace('_', ' ')}" for k, n in pkg.items() if isinstance(n, int) and not isinstance(n, bool) and n]
    if pkg.get("article_qrts"):
        parts.append("promo QRTs from teammates for each article")
    return ", ".join(parts) + " — every Monday"


def client_brief(c, handle, name):
    def show(v, fallback):
        if not v:
            return fallback
        return ", ".join(v) if isinstance(v, (list, tuple)) else str(v)
    return f"""---
name: client-brief
description: "Who this bot works for: X handle, niche topics, accounts to watch and avoid, timezone, goals and notes. Load first in every job."
version: 1.0.0
metadata:
  hermes:
    tags: [perpetual-x, config]
---

# Client brief — generated from the client's settings (change them by asking Perpetual, not here)

- **Client:** {name} (@{handle})
- **Niche topics:** {show(c.get('niche_topics'), 'not set — infer from their posts')}
- **Watch accounts (peers, competitors, people to reply to):** {show(c.get('watch_accounts'), 'none yet')}
- **Never engage with:** {show(c.get('avoid_accounts'), 'none')}
- **Timezone:** {c.get('timezone') or 'UTC'}
- **Goals:** {show(c.get('goals'), 'grow reach and inbound with their ideal audience')}
- **Team:** {show(c.get('team'), 'solo')} (teammates write promo QRTs for each other's articles)
- **Weekly package:** {package_line(c.get('package'))}

## Notes
{show(c.get('notes'), 'none')}
"""


def client_jobs_skills():
    """Part of the schedule fingerprint: jobs are re-registered when shared skills appear."""
    return (SHARED_SKILLS / "agency-writing-standards" / "SKILL.md").exists()


def minute_offset(handle):
    """A stable per-client minute (0-49) so fifteen clients don't all fire at :00 on a 1 GB server."""
    import zlib
    return zlib.crc32(handle.encode()) % 50


def client_jobs(deliver, learn_model, offset=0):
    """Client agents work on demand. Unasked, they only: pick up hand-offs from Perpetual (a model-free
    check every 2 minutes), research their client (silently, 3 nights a week), send one daily brief,
    a weekly report, and run the weekly learning review (silent)."""
    common = ["--deliver", deliver, "--failure-deliver", deliver]
    base = ["client-brief", "client-voice", "playbook", "agency-playbook", "content-pipeline", "doc-templates"]
    if (SHARED_SKILLS / "agency-writing-standards" / "SKILL.md").exists():
        base.append("agency-writing-standards")
    formats = ["x-article", "x-long-tweet", "article-qrt", "video-qrt"]
    deep = ["--provider", "anthropic", "--model", learn_model, "--reasoning-effort", "high"]
    return [
        {"name": "px-requests", "schedule": "every 2m",
         "prompt": "Work was handed to you (by Perpetual or a teammate's agent): follow agency-requests.",
         "args": common + skill_args(*base, "agency-requests", "reply-pack", "deep-research", "weekly-package",
                                     *formats, "tweet-ideas", "reply-hunter", "quote-hunter", "radar-reply",
                                     "lead-finder", "content-calendar", "post-mortem")
                 + ["--monitor-script", "px_requests.py", "--reasoning-effort", "high"]},
        {"name": "px-research", "schedule": f"{offset} 2 * * 1,3,5",
         "prompt": "Scheduled research run: follow deep-research (save to research_memory, publish nothing), "
                   "then reply exactly [SILENT].",
         "args": common + skill_args("client-brief", "client-voice", "playbook", "deep-research") + deep},
        {"name": "px-brief", "schedule": f"{offset} 9 * * 1-5",
         "prompt": "Morning: follow daily-brief.",
         "args": common + skill_args(*base, "daily-brief") + ["--reasoning-effort", "high"]},
        {"name": "px-report", "schedule": f"{offset} 11 * * 1",
         "prompt": "Weekly performance report: follow performance-report.",
         "args": common + skill_args("client-brief", "playbook", "doc-templates", "performance-report")},
        {"name": "px-postmortem", "schedule": f"{(offset + 7) % 60} */2 * * *",
         "prompt": "Posts reached 48 hours: follow post-mortem, then reply exactly [SILENT].",
         "args": common + skill_args("client-brief", "client-voice", "playbook", "post-mortem")
                 + ["--monitor-script", "px_postmortem.py"]},
        {"name": "px-leads", "schedule": f"{offset} 10 * * 4",
         "prompt": "Weekly lead finder: follow lead-finder.",
         "args": common + skill_args("client-brief", "client-voice", "doc-templates", "lead-finder") + deep},
        {"name": "px-slots", "schedule": "every 15m", "prompt": "",
         "args": ["--deliver", deliver, "--no-agent", "--script", "px_slots.py"]},
        {"name": "px-learn", "schedule": f"{offset} 22 * * 0",
         "prompt": "Weekly learning review: follow learning-review and update the playbook and client-voice "
                   "skills. Then reply exactly [SILENT].",
         "args": common + skill_args(*base, "learning-review") + deep},
    ]


def hq_jobs(deliver, learn_model):
    common = ["--deliver", deliver, "--failure-deliver", deliver]
    return [
        {"name": "px-metrics", "schedule": "0 6 * * *", "prompt": "",
         "args": ["--deliver", deliver, "--no-agent", "--script", "px_metrics.py"]},
        {"name": "px-viral", "schedule": "30 6,14 * * *", "prompt": "",
         "args": ["--deliver", deliver, "--no-agent", "--script", "px_viral.py"]},
        # Early-reply radar: one model-free scan for every client; hits become client requests.
        {"name": "px-radar", "schedule": "*/10 7-22 * * *", "prompt": "",
         "args": ["--deliver", deliver, "--no-agent", "--script", "px_radar.py"]},
        {"name": "px-digest", "schedule": "0 9 * * *",
         "prompt": "Daily agency digest: follow agency-digest.",
         "args": common + skill_args("agency-digest")},
        {"name": "px-agency-report", "schedule": "0 10 * * 1",
         "prompt": "Weekly agency report: follow agency-report.",
         "args": common + skill_args("agency-report")},
        {"name": "px-agency-learn", "schedule": "30 23 * * 0",
         "prompt": "Weekly agency learning: follow agency-learning and update the agency playbook.",
         "args": common + skill_args("agency-learning")
                 + ["--provider", "anthropic", "--model", learn_model, "--reasoning-effort", "high"]},
    ]


if __name__ == "__main__":
    main(runtime="--from-runtime" in sys.argv)
