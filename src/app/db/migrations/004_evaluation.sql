CREATE TABLE evaluation (
    id TEXT PRIMARY KEY NOT NULL,
    turn_id TEXT NOT NULL UNIQUE REFERENCES turn(id),
    status TEXT NOT NULL CHECK (status IN ('pending','ready','failed')),
    assessment_json TEXT,
    item_score REAL CHECK (item_score BETWEEN 0 AND 100),
    error_code TEXT,
    model_context_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (
        (status = 'ready' AND assessment_json IS NOT NULL AND item_score IS NOT NULL AND error_code IS NULL)
        OR (status = 'pending' AND assessment_json IS NULL AND item_score IS NULL AND error_code IS NULL)
        OR (status = 'failed' AND assessment_json IS NULL AND item_score IS NULL AND error_code IS NOT NULL)
    )
);
