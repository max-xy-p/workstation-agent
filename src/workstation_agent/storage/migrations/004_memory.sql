CREATE TABLE IF NOT EXISTS memory_migrations(version INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS memory_records(
    id TEXT PRIMARY KEY,
    namespace TEXT NOT NULL,
    content TEXT NOT NULL,
    payload TEXT NOT NULL,
    version INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS memory_namespace ON memory_records(namespace);
CREATE TABLE IF NOT EXISTS memory_scopes(
    namespace TEXT PRIMARY KEY,
    generation INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS memory_batches(
    namespace TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    PRIMARY KEY(namespace, batch_id)
);
INSERT OR IGNORE INTO memory_migrations(version) VALUES(4);
