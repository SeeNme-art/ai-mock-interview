CREATE UNIQUE INDEX plan_item_session_id_id ON plan_item(session_id, id);

CREATE TABLE turn (
    id TEXT PRIMARY KEY NOT NULL,
    session_id TEXT NOT NULL REFERENCES interview_session(id),
    plan_item_id TEXT NOT NULL UNIQUE,
    task_type TEXT NOT NULL CHECK (task_type IN ('knowledge','project')),
    dimension TEXT NOT NULL CHECK (dimension IN ('agent','rag','backend')),
    difficulty TEXT NOT NULL CHECK (difficulty IN ('basic','applied')),
    generation_method TEXT NOT NULL CHECK (generation_method IN ('original','rewrite','generated')),
    fallback_reason TEXT,
    state TEXT NOT NULL CHECK (state IN ('generating','awaiting_answer','processing','closed')),
    probe_count INTEGER NOT NULL DEFAULT 0 CHECK (probe_count BETWEEN 0 AND 2),
    question_snapshot_json TEXT NOT NULL,
    source_snapshot_json TEXT NOT NULL,
    rubric_snapshot_json TEXT NOT NULL,
    interrupted INTEGER NOT NULL DEFAULT 0 CHECK (interrupted IN (0,1)),
    created_at TEXT NOT NULL,
    closed_at TEXT,
    FOREIGN KEY (session_id, plan_item_id) REFERENCES plan_item(session_id, id)
);

CREATE UNIQUE INDEX turn_active_session ON turn(session_id) WHERE state <> 'closed';

CREATE TABLE exchange (
    id TEXT PRIMARY KEY NOT NULL,
    turn_id TEXT NOT NULL REFERENCES turn(id),
    round_no INTEGER NOT NULL CHECK (round_no BETWEEN 0 AND 2),
    question_text TEXT NOT NULL CHECK (length(trim(question_text)) > 0),
    purpose TEXT,
    reason_json TEXT,
    answer_text TEXT,
    answer_mode TEXT CHECK (answer_mode = 'text'),
    answered_at TEXT,
    UNIQUE (turn_id, round_no)
);

CREATE TABLE operation_step (
    operation_id TEXT NOT NULL REFERENCES operation(id),
    step_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('succeeded','failed')),
    result_json TEXT NOT NULL,
    attempts INTEGER NOT NULL CHECK (attempts > 0),
    version_context_json TEXT NOT NULL,
    PRIMARY KEY (operation_id, step_key)
);
