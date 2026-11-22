CREATE TABLE budget_account (
    session_id TEXT PRIMARY KEY NOT NULL REFERENCES interview_session(id),
    currency TEXT NOT NULL CHECK (currency = 'USD'),
    hard_limit_nanos INTEGER NOT NULL CHECK (hard_limit_nanos > 0),
    closing_reserve_nanos INTEGER NOT NULL CHECK (closing_reserve_nanos BETWEEN 0 AND hard_limit_nanos),
    spent_nanos INTEGER NOT NULL DEFAULT 0 CHECK (typeof(spent_nanos) = 'integer' AND spent_nanos >= 0),
    reserved_nanos INTEGER NOT NULL DEFAULT 0 CHECK (typeof(reserved_nanos) = 'integer' AND reserved_nanos >= 0),
    updated_at TEXT NOT NULL
);

CREATE TABLE call_attempt (
    id TEXT PRIMARY KEY NOT NULL,
    session_id TEXT NOT NULL REFERENCES budget_account(session_id),
    operation_id TEXT NOT NULL REFERENCES operation(id),
    step_key TEXT NOT NULL,
    attempt_no INTEGER NOT NULL CHECK (attempt_no IN (1, 2)),
    role TEXT NOT NULL CHECK (role IN ('plan','question','question_check','probe','evaluate','report')),
    status TEXT NOT NULL CHECK (status IN ('reserved','usage_unknown','settled')),
    estimated_input_tokens INTEGER NOT NULL CHECK (estimated_input_tokens > 0),
    max_output_tokens INTEGER NOT NULL CHECK (max_output_tokens > 0),
    reserved_nanos INTEGER NOT NULL CHECK (reserved_nanos >= 0),
    actual_input_tokens INTEGER CHECK (actual_input_tokens >= 0),
    actual_output_tokens INTEGER CHECK (actual_output_tokens >= 0),
    actual_cached_input_tokens INTEGER CHECK (actual_cached_input_tokens BETWEEN 0 AND actual_input_tokens),
    actual_nanos INTEGER CHECK (actual_nanos >= 0),
    pricing_snapshot_json TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    UNIQUE (operation_id, step_key, attempt_no),
    CHECK ((status = 'settled' AND actual_input_tokens IS NOT NULL AND actual_output_tokens IS NOT NULL
            AND actual_cached_input_tokens IS NOT NULL AND actual_nanos IS NOT NULL)
        OR (status <> 'settled' AND actual_input_tokens IS NULL AND actual_output_tokens IS NULL
            AND actual_cached_input_tokens IS NULL AND actual_nanos IS NULL)),
    CHECK ((status = 'reserved') = (ended_at IS NULL))
);

CREATE INDEX call_attempt_session ON call_attempt(session_id);
