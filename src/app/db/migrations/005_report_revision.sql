CREATE TABLE report_revision (
    id TEXT PRIMARY KEY NOT NULL,
    session_id TEXT NOT NULL REFERENCES interview_session(id),
    revision_no INTEGER NOT NULL CHECK (revision_no > 0),
    status TEXT NOT NULL CHECK (status IN ('ready','template_only','failed')),
    is_partial INTEGER NOT NULL CHECK (is_partial IN (0,1)),
    counts_json TEXT NOT NULL,
    knowledge_score REAL CHECK (knowledge_score BETWEEN 0 AND 100),
    project_score REAL CHECK (project_score BETWEEN 0 AND 100),
    dimension_summary_json TEXT NOT NULL,
    evaluation_snapshot_json TEXT NOT NULL,
    improvements_json TEXT NOT NULL,
    markdown TEXT,
    model_context_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (session_id, revision_no)
);
