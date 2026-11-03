-- 随最终答复保存检索时的片段，知识库刷新或运行记录删除后仍能核对回答依据。
-- 旧消息没有可确认的原始证据，保留空列表，不从当前索引回填历史来源。
ALTER TABLE workstation.conversation_messages
    ADD COLUMN sources JSONB NOT NULL DEFAULT '[]'::jsonb
    CHECK (jsonb_typeof(sources) = 'array');
