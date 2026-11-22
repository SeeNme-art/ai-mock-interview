CREATE TABLE cloud_budget (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    total_limit_nanos INTEGER NOT NULL CHECK (total_limit_nanos > 0),
    turn_limit_nanos INTEGER NOT NULL CHECK (turn_limit_nanos BETWEEN 1 AND total_limit_nanos),
    spent_nanos INTEGER NOT NULL DEFAULT 0 CHECK (typeof(spent_nanos) = 'integer' AND spent_nanos >= 0),
    reserved_nanos INTEGER NOT NULL DEFAULT 0 CHECK (typeof(reserved_nanos) = 'integer' AND reserved_nanos >= 0)
);

ALTER TABLE call_attempt ADD COLUMN turn_id TEXT;
CREATE INDEX call_attempt_turn ON call_attempt(session_id, turn_id) WHERE turn_id IS NOT NULL;
