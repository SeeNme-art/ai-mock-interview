ALTER TABLE interview_session ADD COLUMN plan_version INTEGER NOT NULL DEFAULT 0 CHECK (plan_version >= 0);
ALTER TABLE interview_session ADD COLUMN difficulty TEXT NOT NULL DEFAULT 'basic' CHECK (difficulty IN ('basic','applied'));

CREATE TABLE plan_item (
    id TEXT PRIMARY KEY NOT NULL,
    session_id TEXT NOT NULL REFERENCES interview_session(id),
    seq INTEGER NOT NULL CHECK (seq > 0),
    plan_version INTEGER NOT NULL CHECK (plan_version > 0),
    task_type TEXT NOT NULL CHECK (task_type IN ('knowledge','project')),
    dimension TEXT NOT NULL CHECK (dimension IN ('agent','rag','backend')),
    difficulty TEXT NOT NULL CHECK (difficulty IN ('basic','applied')),
    topic TEXT NOT NULL,
    generation_method TEXT NOT NULL CHECK (generation_method IN ('original','rewrite','generated')),
    question_id TEXT,
    question_revision INTEGER CHECK (question_revision > 0),
    project_ref TEXT,
    project_field TEXT,
    form_reason TEXT,
    scoring_targets_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','active','closed')),
    outcome TEXT CHECK (outcome IN ('answered','skipped','unanswered','generation_failed')),
    UNIQUE (session_id, seq),
    UNIQUE (session_id, question_id),
    CHECK ((status = 'closed') = (outcome IS NOT NULL)),
    CHECK (
        (task_type = 'knowledge' AND question_id IS NOT NULL AND question_revision IS NOT NULL
         AND project_ref IS NULL AND project_field IS NULL AND generation_method IN ('original','rewrite'))
        OR
        (task_type = 'project' AND question_id IS NULL AND question_revision IS NULL
         AND project_ref IS NOT NULL AND project_field IS NOT NULL AND generation_method = 'generated')
    )
);

CREATE TABLE operation (
    id TEXT PRIMARY KEY NOT NULL,
    session_id TEXT NOT NULL REFERENCES interview_session(id),
    owner_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    expected_epoch INTEGER NOT NULL CHECK (expected_epoch >= 0),
    status TEXT NOT NULL CHECK (status IN ('queued','running','recovery_required','succeeded','failed','cancelled')),
    safe_request_json TEXT NOT NULL,
    result_refs_json TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (owner_id, kind, idempotency_key)
);

CREATE UNIQUE INDEX operation_active_session ON operation(session_id)
    WHERE status IN ('queued','running','recovery_required');

CREATE TABLE migration_backup (
    file_name TEXT PRIMARY KEY NOT NULL,
    schema_version INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
