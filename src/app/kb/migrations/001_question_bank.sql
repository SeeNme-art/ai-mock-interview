CREATE TABLE schema_migration (
    version INTEGER PRIMARY KEY,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE knowledge_source (
    id TEXT PRIMARY KEY NOT NULL,
    title TEXT NOT NULL,
    locator TEXT NOT NULL,
    section TEXT NOT NULL,
    source_type TEXT NOT NULL CHECK (source_type IN ('official_docs', 'paper')),
    source_version TEXT,
    checked_at TEXT NOT NULL
);

CREATE TABLE question (
    id TEXT PRIMARY KEY NOT NULL,
    stable_faiss_id INTEGER NOT NULL UNIQUE CHECK (stable_faiss_id > 0),
    revision INTEGER NOT NULL CHECK (revision > 0),
    dimension TEXT NOT NULL CHECK (dimension IN ('agent', 'rag', 'backend')),
    difficulty TEXT NOT NULL CHECK (difficulty IN ('basic', 'applied')),
    content TEXT NOT NULL,
    prompt_key TEXT NOT NULL UNIQUE,
    objective TEXT NOT NULL,
    answer_points_json TEXT NOT NULL,
    acceptable_alternatives_json TEXT NOT NULL,
    common_errors_json TEXT NOT NULL,
    applicable_dimensions_json TEXT NOT NULL,
    tags_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('draft', 'verified', 'retired')),
    checked_at TEXT NOT NULL,
    content_hash TEXT NOT NULL
);

CREATE TABLE question_source (
    question_id TEXT NOT NULL REFERENCES question(id) ON DELETE CASCADE,
    source_id TEXT NOT NULL REFERENCES knowledge_source(id),
    position INTEGER NOT NULL CHECK (position >= 0),
    PRIMARY KEY (question_id, source_id),
    UNIQUE (question_id, position)
);

CREATE INDEX question_candidate_filters ON question(status, dimension, difficulty, stable_faiss_id);
