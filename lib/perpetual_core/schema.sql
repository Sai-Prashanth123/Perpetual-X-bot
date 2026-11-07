-- Perpetual X Bot — agency database (Postgres). Idempotent: applied on every boot by the
-- bot and the web app. Add columns with ALTER ... ADD COLUMN IF NOT EXISTS, never edit
-- a CREATE in place.

CREATE TABLE IF NOT EXISTS clients (
    handle          TEXT PRIMARY KEY,                -- lowercase X handle, no @
    display_name    TEXT NOT NULL,
    niche_topics    TEXT,
    watch_accounts  TEXT,
    avoid_accounts  TEXT,
    timezone        TEXT NOT NULL DEFAULT 'UTC',
    slack_channel   TEXT,
    employee_slack  TEXT,
    active          BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS employees (
    slack_id        TEXT PRIMARY KEY,
    name            TEXT,
    is_owner        BOOLEAN NOT NULL DEFAULT FALSE,
    last_seen       TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS counters (
    client          TEXT NOT NULL,
    prefix          TEXT NOT NULL,
    n               INTEGER NOT NULL,
    PRIMARY KEY (client, prefix)
);

CREATE TABLE IF NOT EXISTS suggestions (
    client          TEXT NOT NULL,
    code            TEXT NOT NULL,
    kind            TEXT NOT NULL,                   -- tweet|thread|reply|quote|repost|engage
    text            TEXT NOT NULL,
    target_id       TEXT,
    target_url      TEXT,
    target_text     TEXT,
    target_by       TEXT,
    rationale       TEXT,
    hook            TEXT,
    format          TEXT,
    topic           TEXT,
    batch           TEXT,
    intent_url      TEXT,
    api_postable    BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    status          TEXT NOT NULL DEFAULT 'pending', -- pending|approved|edited|rejected|posted|expired
    final_text      TEXT,
    note            TEXT,
    decided_at      TIMESTAMPTZ,
    decided_by      TEXT,
    posted_id       TEXT,
    match_score     REAL,
    PRIMARY KEY (client, code)
);
CREATE INDEX IF NOT EXISTS suggestions_recent ON suggestions (client, created_at DESC);

CREATE TABLE IF NOT EXISTS feedback_events (
    id              BIGSERIAL PRIMARY KEY,
    client          TEXT NOT NULL,
    code            TEXT NOT NULL,
    action          TEXT NOT NULL,
    text            TEXT,
    note            TEXT,
    by_name         TEXT,
    at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS seen_targets (
    client          TEXT NOT NULL,
    tweet_id        TEXT NOT NULL,
    first_seen      TIMESTAMPTZ NOT NULL DEFAULT now(),
    used_for        TEXT,
    PRIMARY KEY (client, tweet_id)
);

CREATE TABLE IF NOT EXISTS outcomes (
    client          TEXT NOT NULL,
    tweet_id        TEXT NOT NULL,
    code            TEXT,
    kind            TEXT,
    text            TEXT,
    url             TEXT,
    posted_at       TIMESTAMPTZ,
    latest          JSONB,
    at_24h          JSONB,
    at_72h          JSONB,
    checked_at      TIMESTAMPTZ,
    PRIMARY KEY (client, tweet_id)
);

CREATE TABLE IF NOT EXISTS published (
    id              BIGSERIAL PRIMARY KEY,
    client          TEXT NOT NULL,
    code            TEXT,
    tweet_id        TEXT NOT NULL,
    url             TEXT,
    text            TEXT,
    by_name         TEXT,
    via             TEXT NOT NULL,                   -- slack|dashboard
    at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS x_tokens (
    client          TEXT PRIMARY KEY,
    x_user_id       TEXT,
    x_username      TEXT,
    access_enc      TEXT NOT NULL,                   -- Fernet-encrypted
    refresh_enc     TEXT,
    expires_at      TIMESTAMPTZ,
    scope           TEXT,
    connected_by    TEXT,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS metrics_daily (
    client          TEXT NOT NULL,
    day             DATE NOT NULL,
    followers       INTEGER,
    posts           INTEGER,
    likes           INTEGER,
    replies         INTEGER,
    reposts         INTEGER,
    bookmarks       INTEGER,
    views           BIGINT,
    raw             JSONB,
    PRIMARY KEY (client, day)
);

CREATE TABLE IF NOT EXISTS viral_feed (
    day             DATE NOT NULL,
    niche           TEXT NOT NULL,
    tweet_id        TEXT NOT NULL,
    url             TEXT,
    author          TEXT,
    text            TEXT,
    likes           INTEGER,
    views           BIGINT,
    bookmarks       INTEGER,
    likes_per_h     REAL,
    posted_at       TIMESTAMPTZ,
    PRIMARY KEY (day, niche, tweet_id)
);

CREATE TABLE IF NOT EXISTS learning_log (
    id              BIGSERIAL PRIMARY KEY,
    client          TEXT NOT NULL,                   -- '_agency' for the HQ review
    at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    summary         TEXT NOT NULL,
    changes         JSONB
);

CREATE TABLE IF NOT EXISTS agency_requests (     -- dashboard -> a client's bot ("draft for client X")
    id              BIGSERIAL PRIMARY KEY,
    client          TEXT NOT NULL,
    request         TEXT NOT NULL,
    by_name         TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    picked_up_at    TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS documents (           -- latest text of learned skills, for the dashboard
    key             TEXT PRIMARY KEY,                -- '<client>:playbook', '<client>:client-voice', 'agency-playbook'
    body            TEXT NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- v3: each suggestion is an interactive Slack card; remember where it lives so it can be updated.
ALTER TABLE suggestions ADD COLUMN IF NOT EXISTS slack_channel TEXT;
ALTER TABLE suggestions ADD COLUMN IF NOT EXISTS slack_ts TEXT;

-- v4: who added a client (DM receipts and milestones go to them), company for teammate QRTs,
-- the weekly content package, and richer suggestions (references, fact-check, scores, parent).
ALTER TABLE clients ADD COLUMN IF NOT EXISTS added_by TEXT;
ALTER TABLE clients ADD COLUMN IF NOT EXISTS company TEXT;
ALTER TABLE clients ADD COLUMN IF NOT EXISTS package JSONB;
ALTER TABLE suggestions ADD COLUMN IF NOT EXISTS refs JSONB;
ALTER TABLE suggestions ADD COLUMN IF NOT EXISTS factcheck JSONB;
ALTER TABLE suggestions ADD COLUMN IF NOT EXISTS scores JSONB;
ALTER TABLE suggestions ADD COLUMN IF NOT EXISTS parent_code TEXT;
ALTER TABLE suggestions ADD COLUMN IF NOT EXISTS parent_client TEXT;

-- What a client has already published or drafted (imported from content-agent, plus our own),
-- so agents never repeat a topic inside the cooldown.
CREATE TABLE IF NOT EXISTS content_history (
    id              BIGSERIAL PRIMARY KEY,
    client          TEXT NOT NULL,
    day             DATE,
    format          TEXT,
    title           TEXT,
    topic           TEXT,
    status          TEXT,
    source          TEXT NOT NULL DEFAULT 'perpetual',
    UNIQUE (client, day, format, title)
);

-- v5: on-demand agents. Hand-offs remember who asked (they get the result link by DM); research
-- memory holds what each client agent has learned about its client from X data and the web; and
-- documents published to Slack (canvas or thread) are listed so people can find them again.
ALTER TABLE agency_requests ADD COLUMN IF NOT EXISTS requester_slack TEXT;

CREATE TABLE IF NOT EXISTS research (
    id              BIGSERIAL PRIMARY KEY,
    client          TEXT NOT NULL,
    at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    kind            TEXT NOT NULL,                   -- performance | competitors | niche | growth | content | note
    title           TEXT NOT NULL,
    body            TEXT NOT NULL,
    sources         JSONB
);
CREATE INDEX IF NOT EXISTS research_client_at ON research (client, at DESC);

CREATE TABLE IF NOT EXISTS published_docs (
    id              BIGSERIAL PRIMARY KEY,
    client          TEXT NOT NULL,
    at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    title           TEXT NOT NULL,
    kind            TEXT,                            -- reply-pack | package | brief | research | report | answer
    mode            TEXT NOT NULL,                   -- canvas | thread
    link            TEXT,
    channel         TEXT,
    ts              TEXT
);

-- v6: early-reply radar, post-mortems, lead finder, content calendar.
ALTER TABLE clients ADD COLUMN IF NOT EXISTS radar_accounts TEXT;      -- comma-separated handles
ALTER TABLE clients ADD COLUMN IF NOT EXISTS radar_enabled BOOLEAN NOT NULL DEFAULT true;
ALTER TABLE clients ADD COLUMN IF NOT EXISTS icp TEXT;                  -- who buys from this client
ALTER TABLE agency_requests ADD COLUMN IF NOT EXISTS kind TEXT;

CREATE TABLE IF NOT EXISTS alerts (                -- radar alerts sent (daily cap, no repeats)
    id              BIGSERIAL PRIMARY KEY,
    client          TEXT NOT NULL,
    kind            TEXT NOT NULL,
    tweet_id        TEXT,
    at              TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS alerts_client_at ON alerts (client, at DESC);

CREATE TABLE IF NOT EXISTS postmortems (
    client          TEXT NOT NULL,
    tweet_id        TEXT NOT NULL,
    code            TEXT,
    format          TEXT,
    url             TEXT,
    verdict         TEXT,                            -- win | par | flop (null until the agent judges it)
    metrics         JSONB,
    baseline        JSONB,                           -- the client's 30-day medians for this format
    vs_median       JSONB,                           -- ratio per metric
    why             TEXT,
    lesson          TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    judged_at       TIMESTAMPTZ,
    PRIMARY KEY (client, tweet_id)
);

CREATE TABLE IF NOT EXISTS leads (
    id              BIGSERIAL PRIMARY KEY,
    client          TEXT NOT NULL,
    code            TEXT NOT NULL,                   -- L1, L2… per client
    handle          TEXT NOT NULL,
    name            TEXT,
    bio             TEXT,
    followers       INTEGER,
    role_company    TEXT,
    said            TEXT,
    said_url        TEXT,
    fit_score       INTEGER,                         -- 1-10
    why             TEXT,
    opener          TEXT,
    status          TEXT NOT NULL DEFAULT 'new',     -- new | contacted | replied | won | not_a_fit
    note            TEXT,
    found_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ,
    UNIQUE (client, handle),
    UNIQUE (client, code)
);

CREATE TABLE IF NOT EXISTS calendar_slots (
    id              BIGSERIAL PRIMARY KEY,
    client          TEXT NOT NULL,
    slot_at         TIMESTAMPTZ NOT NULL,
    code            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'planned', -- planned | approved | reminded | posted | skipped
    note            TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (client, code)
);
CREATE INDEX IF NOT EXISTS calendar_due ON calendar_slots (status, slot_at);

-- v7: each teammate's own tool connections (Google, Notion), tokens encrypted.
CREATE TABLE IF NOT EXISTS user_connections (
    slack_user      TEXT NOT NULL,
    provider        TEXT NOT NULL,                   -- google | notion
    account         TEXT,                            -- e.g. the Google email
    access_enc      TEXT NOT NULL,
    refresh_enc     TEXT,
    expires_at      TIMESTAMPTZ,
    scopes          TEXT,
    connected_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (slack_user, provider)
);

-- v8: content studio hook library, trend-jacking switch.
CREATE TABLE IF NOT EXISTS hook_library (
    id              BIGSERIAL PRIMARY KEY,
    client          TEXT,                            -- NULL = niche-wide
    niche           TEXT,
    hook            TEXT NOT NULL,
    label           TEXT,
    source_url      TEXT,
    source_author   TEXT,
    likes           INT,
    views           INT,
    bookmarks       INT,
    er              REAL,
    found_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    used_code       TEXT,
    outcome         TEXT,                            -- win|par|flop
    UNIQUE (client, source_url)
);
CREATE INDEX IF NOT EXISTS hook_library_client_niche ON hook_library (client, niche);
ALTER TABLE clients ADD COLUMN IF NOT EXISTS trend_enabled BOOLEAN NOT NULL DEFAULT true;

-- v9: team tasks + meeting follow-up (Read AI).
CREATE TABLE IF NOT EXISTS team_tasks (
    id           SERIAL PRIMARY KEY,
    title        TEXT NOT NULL,
    owner_slack  TEXT,
    owner_name   TEXT,
    client       TEXT,
    due          DATE,
    status       TEXT NOT NULL DEFAULT 'open',        -- open | doing | blocked | done | dropped
    source       TEXT NOT NULL DEFAULT 'manual',      -- manual | meeting | standup
    source_ref   TEXT,
    notes        TEXT,
    created_by   TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    done_at      TIMESTAMPTZ,
    reminded_on  DATE
);
CREATE INDEX IF NOT EXISTS team_tasks_owner ON team_tasks(owner_slack, status);
CREATE TABLE IF NOT EXISTS meetings (
    id            TEXT PRIMARY KEY,                   -- readai:<id>
    source        TEXT NOT NULL,
    title         TEXT,
    started_at    TIMESTAMPTZ,
    ended_at      TIMESTAMPTZ,
    participants  JSONB,
    summary       TEXT,
    action_items  JSONB,
    key_questions JSONB,
    url           TEXT,
    client        TEXT,
    fetched_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at  TIMESTAMPTZ,
    tasks_created INT,
    note          TEXT
);

-- v10: daily standup run from Slack.
CREATE TABLE IF NOT EXISTS standup_config (
    id              INT PRIMARY KEY DEFAULT 1,
    time            TEXT NOT NULL DEFAULT '11:30',
    timezone        TEXT NOT NULL DEFAULT 'Asia/Kolkata',
    days            TEXT NOT NULL DEFAULT '1,2,3,4,5',
    lead_minutes    INT NOT NULL DEFAULT 45,
    summary_minutes INT NOT NULL DEFAULT 5,
    channel         TEXT,
    members         JSONB,                       -- null/empty = everyone in Slack
    excluded        JSONB,
    enabled         BOOLEAN NOT NULL DEFAULT true
);
CREATE TABLE IF NOT EXISTS standup_runs (
    day          DATE PRIMARY KEY,
    asked_at     TIMESTAMPTZ,
    asked_count  INT,
    summary_at   TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS standup_answers (
    day          DATE NOT NULL,
    slack_user   TEXT NOT NULL,
    name         TEXT,
    yesterday    JSONB,
    today        JSONB,
    blockers     JSONB,
    answered_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (day, slack_user)
);

-- v11: daily Slack List per day, check-ins, intro.
CREATE TABLE IF NOT EXISTS daily_lists (
    day        DATE PRIMARY KEY,
    list_id    TEXT NOT NULL,
    url        TEXT,
    columns    JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE team_tasks ADD COLUMN IF NOT EXISTS list_id TEXT;
ALTER TABLE team_tasks ADD COLUMN IF NOT EXISTS list_item_id TEXT;
ALTER TABLE standup_config ADD COLUMN IF NOT EXISTS checkin_time TEXT NOT NULL DEFAULT '15:00';
ALTER TABLE standup_config ADD COLUMN IF NOT EXISTS eod_time TEXT NOT NULL DEFAULT '20:15';
ALTER TABLE standup_config ADD COLUMN IF NOT EXISTS intro_sent_at TIMESTAMPTZ;
ALTER TABLE standup_runs ADD COLUMN IF NOT EXISTS checkin_at TIMESTAMPTZ;
ALTER TABLE standup_runs ADD COLUMN IF NOT EXISTS eod_at TIMESTAMPTZ;

-- v12: Notion Agency OS sync.
CREATE TABLE IF NOT EXISTS notion_os (
    id           INT PRIMARY KEY DEFAULT 1,
    page_id      TEXT,
    url          TEXT,
    dbs          JSONB,                      -- {"tasks": id, "meetings": id, "clients": id, "goals": id}
    last_pull    TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE team_tasks ADD COLUMN IF NOT EXISTS notion_page_id TEXT;
ALTER TABLE team_tasks ADD COLUMN IF NOT EXISTS notion_synced_at TIMESTAMPTZ;
ALTER TABLE meetings ADD COLUMN IF NOT EXISTS notion_page_id TEXT;
ALTER TABLE meetings ADD COLUMN IF NOT EXISTS notion_synced_at TIMESTAMPTZ;
ALTER TABLE clients ADD COLUMN IF NOT EXISTS notion_page_id TEXT;
ALTER TABLE clients ADD COLUMN IF NOT EXISTS notion_synced_at TIMESTAMPTZ;

-- v13: approvals queue.
CREATE TABLE IF NOT EXISTS approvals (
    id            serial PRIMARY KEY,
    kind          text NOT NULL,
    summary       text,
    payload       jsonb NOT NULL DEFAULT '{}',
    preview       text,
    requested_by  text,
    requested_for text,                       -- whose account runs it: a Slack id or 'agency'
    client        text,
    status        text NOT NULL DEFAULT 'pending',  -- pending|approved|rejected|executed|failed|cancelled
    decided_by    text,
    decided_at    timestamptz,
    result        jsonb,
    error         text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    approver_msg  jsonb NOT NULL DEFAULT '[]'  -- [{"channel", "ts"}] of each owner's card
);
CREATE INDEX IF NOT EXISTS approvals_status ON approvals (status, created_at);

-- v13: client goals + weekly team scorecard.
CREATE TABLE IF NOT EXISTS client_goals (
    id          SERIAL PRIMARY KEY,
    client      TEXT NOT NULL,
    metric      TEXT NOT NULL CHECK (metric IN ('followers_gain','impressions','likes','replies','reposts',
                                                'bookmarks','posts','engagement_rate')),
    target      NUMERIC NOT NULL,
    period      TEXT NOT NULL CHECK (period IN ('total','per_month','per_week','per_post','per_day')),
    start_date  DATE NOT NULL DEFAULT current_date,
    end_date    DATE,
    note        TEXT,
    created_by  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    active      BOOLEAN NOT NULL DEFAULT true
);
CREATE INDEX IF NOT EXISTS client_goals_client ON client_goals(client, active);

CREATE TABLE IF NOT EXISTS scorecard_runs (
    week_end   DATE PRIMARY KEY,
    posted_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- v13: proactive alerts.
CREATE TABLE IF NOT EXISTS proactive_alerts (
    id            SERIAL PRIMARY KEY,
    trigger       TEXT NOT NULL,
    key           TEXT NOT NULL,                    -- dedupe key, e.g. 'overdue:TASK-12'
    target_slack  TEXT NOT NULL,                    -- who gets it
    subject       TEXT,
    facts         JSONB,
    status        TEXT NOT NULL DEFAULT 'candidate', -- candidate|sent|skipped|useful|not_now|never|ignored|expired
    reason        TEXT,                             -- why the judge skipped it
    message       TEXT,                             -- what was sent (few-shot examples)
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at       TIMESTAMPTZ,
    feedback_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS proactive_alerts_key ON proactive_alerts (key, target_slack, created_at DESC);
CREATE INDEX IF NOT EXISTS proactive_alerts_target ON proactive_alerts (target_slack, sent_at DESC);
CREATE TABLE IF NOT EXISTS proactive_mutes (
    target_slack  TEXT NOT NULL,
    trigger       TEXT NOT NULL,
    until         TIMESTAMPTZ,                      -- NULL = forever
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (target_slack, trigger)
);

-- v14: long-term memory + conversation log.
CREATE TABLE IF NOT EXISTS agency_memory (
    id          SERIAL PRIMARY KEY,
    scope       TEXT NOT NULL,                    -- agency | person:<slack id> | client:<handle>
    text        TEXT NOT NULL,
    created_by  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used   TIMESTAMPTZ,
    uses        INT NOT NULL DEFAULT 0,
    active      BOOLEAN NOT NULL DEFAULT true
);
CREATE INDEX IF NOT EXISTS agency_memory_scope ON agency_memory(scope, active);
CREATE TABLE IF NOT EXISTS conversation_log (
    id          BIGSERIAL PRIMARY KEY,
    slack_user  TEXT,
    chat_id     TEXT,
    platform    TEXT,
    session_id  TEXT,
    user_text   TEXT,
    reply_text  TEXT,
    at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS conversation_log_user ON conversation_log(slack_user, at DESC);

-- v15: daily List sharing status (write access for the team).
ALTER TABLE daily_lists ADD COLUMN IF NOT EXISTS shared_at TIMESTAMPTZ;
ALTER TABLE daily_lists ADD COLUMN IF NOT EXISTS share_error TEXT;

-- v16: memory full-text search + nightly digest marker.
ALTER TABLE conversation_log ADD COLUMN IF NOT EXISTS digested_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS agency_memory_fts ON agency_memory USING GIN (to_tsvector('english', text));
CREATE INDEX IF NOT EXISTS conversation_log_fts ON conversation_log
    USING GIN (to_tsvector('english', coalesce(user_text, '') || ' ' || coalesce(reply_text, '')));

-- v17: document library (MarkItDown) + chunks for search.
CREATE TABLE IF NOT EXISTS library_docs (
    id          SERIAL PRIMARY KEY,
    sha256      TEXT UNIQUE NOT NULL,
    name        TEXT NOT NULL,
    source      TEXT,                     -- slack upload / slack file id
    slack_user  TEXT,
    chat_id     TEXT,
    chars       INT NOT NULL DEFAULT 0,
    text        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS library_chunks (
    doc_id  INT NOT NULL REFERENCES library_docs(id) ON DELETE CASCADE,
    n       INT NOT NULL,
    text    TEXT NOT NULL,
    PRIMARY KEY (doc_id, n)
);
CREATE INDEX IF NOT EXISTS library_chunks_fts ON library_chunks USING GIN (to_tsvector('english', text));

-- v18: connected memory (Graphiti) progress.
CREATE TABLE IF NOT EXISTS graph_state (
    id              INT PRIMARY KEY DEFAULT 1,
    last_memory_id  INT NOT NULL DEFAULT 0,
    last_run        TIMESTAMPTZ,
    last_result     TEXT
);
