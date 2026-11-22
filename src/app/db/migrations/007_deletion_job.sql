CREATE TABLE deletion_job (
    id TEXT PRIMARY KEY NOT NULL,
    target_session_id TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('queued', 'failed', 'completed')),
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
