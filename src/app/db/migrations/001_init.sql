CREATE TABLE schema_migration (
    version INTEGER PRIMARY KEY,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE interview_session (
    id TEXT PRIMARY KEY NOT NULL,
    owner_id TEXT NOT NULL,
    phase TEXT NOT NULL DEFAULT 'PLANNING' CHECK (phase IN ('PLANNING', 'ACTIVE', 'FINISHED')),
    delete_state TEXT NOT NULL DEFAULT 'none' CHECK (delete_state IN ('none', 'deleting')),
    end_reason TEXT,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    control_epoch INTEGER NOT NULL DEFAULT 0 CHECK (control_epoch >= 0),
    profile_id TEXT NOT NULL DEFAULT 'ai_app_engineer.tech',
    level TEXT NOT NULL DEFAULT 'junior',
    mode TEXT NOT NULL CHECK (mode IN ('mixed', 'knowledge_only')),
    question_count INTEGER NOT NULL DEFAULT 5 CHECK (question_count IN (5, 8)),
    selected_dimensions_json TEXT NOT NULL DEFAULT '["agent","rag","backend"]',
    input_mode TEXT NOT NULL CHECK (input_mode IN ('text', 'form')),
    jd_text TEXT,
    resume_profile_json TEXT NOT NULL,
    consent_version TEXT,
    provider_id TEXT,
    policy_snapshot_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    ended_at TEXT,
    draft_id TEXT NOT NULL,
    confirmation_key TEXT NOT NULL,
    confirmation_hash TEXT NOT NULL,
    UNIQUE (owner_id, draft_id),
    UNIQUE (owner_id, confirmation_key)
);
