CREATE TABLE IF NOT EXISTS rag_migrations (
    version INTEGER PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS rag_documents (
    source_uri TEXT PRIMARY KEY,
    document_id TEXT NOT NULL UNIQUE,
    bundle TEXT NOT NULL
);
INSERT OR IGNORE INTO rag_migrations(version) VALUES (2);
