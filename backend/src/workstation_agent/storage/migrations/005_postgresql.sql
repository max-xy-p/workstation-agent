-- PostgreSQL 业务表；旧 SQLite 文件不由本脚本读取或删除。
CREATE TABLE IF NOT EXISTS workstation.runs (
    workspace_id TEXT NOT NULL,
    id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    status TEXT NOT NULL,
    payload JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (workspace_id, id)
);
CREATE INDEX IF NOT EXISTS runs_session ON workstation.runs(workspace_id, session_id);
CREATE TABLE IF NOT EXISTS workstation.memory_records (
    workspace_id TEXT NOT NULL,
    id TEXT NOT NULL,
    namespace TEXT NOT NULL,
    content TEXT NOT NULL,
    payload JSONB NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    PRIMARY KEY (workspace_id, id)
);
CREATE INDEX IF NOT EXISTS memory_namespace ON workstation.memory_records(workspace_id, namespace);
CREATE TABLE IF NOT EXISTS workstation.memory_scopes (
    workspace_id TEXT NOT NULL,
    namespace TEXT NOT NULL,
    generation BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (workspace_id, namespace)
);
CREATE TABLE IF NOT EXISTS workstation.memory_batches (
    workspace_id TEXT NOT NULL,
    namespace TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    PRIMARY KEY (workspace_id, namespace, batch_id)
);
CREATE TABLE IF NOT EXISTS workstation.rag_documents (
    workspace_id TEXT NOT NULL,
    source_uri TEXT NOT NULL,
    document_id TEXT NOT NULL,
    bundle JSONB NOT NULL,
    format_version INTEGER NOT NULL,
    PRIMARY KEY (workspace_id, source_uri)
);
