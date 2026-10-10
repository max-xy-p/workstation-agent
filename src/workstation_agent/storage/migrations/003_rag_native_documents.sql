ALTER TABLE rag_documents
    ADD COLUMN format_version INTEGER NOT NULL DEFAULT 2;
INSERT INTO rag_migrations(version) VALUES (3);
