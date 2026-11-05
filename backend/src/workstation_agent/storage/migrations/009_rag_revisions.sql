-- 每次发布或删除都增加来源版本。删除后保留版本，旧计划不能重新创建已移除的索引。
CREATE TABLE workstation.rag_revisions (
    workspace_id TEXT NOT NULL,
    source_uri TEXT NOT NULL,
    revision BIGINT NOT NULL,
    PRIMARY KEY (workspace_id, source_uri)
);
