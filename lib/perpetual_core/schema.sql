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
