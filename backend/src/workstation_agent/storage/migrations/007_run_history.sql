-- 为运行记录增加可信身份范围；旧记录保留在空范围，不自动推测其用户或项目归属。
ALTER TABLE workstation.runs ADD COLUMN scope TEXT NOT NULL DEFAULT '';
ALTER TABLE workstation.runs DROP CONSTRAINT runs_pkey;
ALTER TABLE workstation.runs ADD PRIMARY KEY (workspace_id, scope, id);
CREATE INDEX runs_history ON workstation.runs(workspace_id, scope, updated_at DESC, id DESC);
CREATE INDEX runs_session_history ON workstation.runs(workspace_id, scope, session_id, updated_at DESC, id DESC);
