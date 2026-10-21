-- 会话及可展示的文本历史独立于官方检查点；scope 由可信身份配置生成。
CREATE TABLE workstation.conversations (
    scope TEXT NOT NULL,
    id TEXT NOT NULL,
    title TEXT NOT NULL,
    revision BIGINT NOT NULL DEFAULT 0 CHECK (revision >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (scope, id)
);
CREATE INDEX conversations_updated ON workstation.conversations(scope, updated_at DESC, id);
CREATE TABLE workstation.conversation_messages (
    scope TEXT NOT NULL,
    session_id TEXT NOT NULL,
    id TEXT NOT NULL,
    position BIGINT GENERATED ALWAYS AS IDENTITY,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    run_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (scope, session_id, id),
    FOREIGN KEY (scope, session_id) REFERENCES workstation.conversations(scope, id) ON DELETE CASCADE
);
CREATE INDEX conversation_messages_order ON workstation.conversation_messages(scope, session_id, position);
