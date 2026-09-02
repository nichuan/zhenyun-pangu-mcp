-- ============================================================================
-- zhenyun-pangu-mcp 知识库表结构（Supabase / Postgres）
-- 用途：沉淀 Agent 的「认知层」——业务知识、系统机制、排查经验、数据模型解释、
--       稳定业务规则等非模板类知识。与 sql_templates（可复用行动）、
--       table_catalog / table_relations（机器事实）互补。
-- 执行方式：在 Supabase 控制台 SQL Editor 中全量执行本文件即可。
-- （本文件原位于 knowledge-ops-mcp，整合进 zhenyun-pangu-mcp 后保留于此。）
-- ============================================================================

-- 启用模糊检索扩展（中文/子串匹配）
CREATE EXTENSION IF NOT EXISTS pg_trgm;
-- 启用向量检索扩展（语义匹配，NVIDIA nv-embed-v1 2048 维）
CREATE EXTENSION IF NOT EXISTS vector;

-- ============================================================================
-- knowledge_docs：业务知识、系统知识、排查经验、数据模型解释、稳定业务规则
-- 设计要点（对齐"Skill 管行为，Knowledge 管认知，MCP 管事实，Template 管行动"）：
--   knowledge_type 区分知识性质（business/system/technical/troubleshooting/...）
--   system + module 形成 系统 → 模块 → 知识 的三层归类
--   core_tables / related_template_ids 与 table_catalog / sql_templates 关联
--   status 区分事实等级（draft/verified/deprecated/archived），verified_at 记录验证时点
-- ============================================================================
CREATE TABLE IF NOT EXISTS knowledge_docs (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    title               TEXT NOT NULL,                                   -- 知识标题
    knowledge_type      TEXT NOT NULL DEFAULT 'business',                -- 知识类型：business/system/technical/troubleshooting/data_model/configuration/experience/rule
    system              TEXT,                                            -- 所属系统：srm / pangu（对应 天工 / 盘古）
    module              TEXT,                                            -- 所属模块：mdm / data-index / po / rfx ...
    content_md          TEXT NOT NULL,                                   -- 知识正文（Markdown）
    summary             TEXT,                                            -- 一句话摘要（检索展示用）
    core_tables         TEXT[] DEFAULT '{}',                             -- 关联核心表（对齐 table_catalog.table_name）
    related_template_ids BIGINT[] DEFAULT '{}',                          -- 关联 SQL 模板 id（对齐 sql_templates.id）
    tags                TEXT[] DEFAULT '{}',                             -- 标签/关键词
    embedding           vector(2048),                                    -- 语义向量（nvidia/nv-embed-v1，2048 维；与 config.EMBEDDING_DIM 一致）
    status              TEXT NOT NULL DEFAULT 'draft',                   -- 事实等级：draft(草稿)/verified(已验证)/deprecated(废弃)/archived(归档)
    source_type         TEXT NOT NULL DEFAULT 'manual',                  -- 来源：manual(人工)/migration(迁移)/generated(自动生成)/experience(经验沉淀)/official(官方文档)
    verified_at         TIMESTAMPTZ,                                     -- 验证时间（status=verified 时建议填写）
    verified_by         TEXT,                                            -- 验证人
    created_by          TEXT,                                            -- 作者（团队共享预留）
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 检索索引
CREATE INDEX IF NOT EXISTS idx_knowledge_docs_title_trgm
    ON knowledge_docs USING gin (title gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_knowledge_docs_content_trgm
    ON knowledge_docs USING gin (content_md gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_knowledge_docs_tags
    ON knowledge_docs USING gin (tags);
CREATE INDEX IF NOT EXISTS idx_knowledge_docs_core_tables
    ON knowledge_docs USING gin (core_tables);
CREATE INDEX IF NOT EXISTS idx_knowledge_docs_related_templates
    ON knowledge_docs USING gin (related_template_ids);
CREATE INDEX IF NOT EXISTS idx_knowledge_docs_type ON knowledge_docs (knowledge_type);
CREATE INDEX IF NOT EXISTS idx_knowledge_docs_system ON knowledge_docs (system);
CREATE INDEX IF NOT EXISTS idx_knowledge_docs_module ON knowledge_docs (module);
CREATE INDEX IF NOT EXISTS idx_knowledge_docs_status ON knowledge_docs (status);

-- 注意：pgvector 的 HNSW / IVFFlat 索引均限制最多 2000 维，而 nv-embed-v1 返回 2048 维，
-- 因此此处【不建向量索引】，改用顺序扫描做精确余弦检索（cosine）。知识库规模小（数百条），
-- 顺序扫描性能完全足够，且保留完整 2048 维语义信息。
-- 若后续换用 ≤2000 维模型并需要 ANN 加速，可改为 hnsw (embedding vector_cosine_ops)。

-- 自动维护 updated_at
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_knowledge_docs_updated_at ON knowledge_docs;
CREATE TRIGGER trg_knowledge_docs_updated_at
    BEFORE UPDATE ON knowledge_docs
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ============================================================================
-- 语义检索 RPC（混合检索的向量召回部分）
-- 通过 embedding 余弦距离召回相似知识，支持 knowledge_type / system / module 过滤。
-- 向量维度依赖 embedding 列（当前 vector(2048)，随模型变更），调用前需先跑 backfill 生成 embedding。
-- match_threshold 建议 0.5~0.75；低于阈值的结果会被过滤。
-- ============================================================================
DROP FUNCTION IF EXISTS match_knowledge_docs(vector, float, int, text, text, text, text);
CREATE OR REPLACE FUNCTION match_knowledge_docs(
    query_embedding vector,
    match_threshold float,
    match_count int DEFAULT 10,
    p_knowledge_type text DEFAULT NULL,
    p_system text DEFAULT NULL,
    p_module text DEFAULT NULL,
    p_status text DEFAULT NULL
)
RETURNS TABLE (
    id bigint,
    title text,
    knowledge_type text,
    system text,
    module text,
    summary text,
    content_md text,
    tags text[],
    core_tables text[],
    related_template_ids bigint[],
    status text,
    source_type text,
    similarity float
)
LANGUAGE plpgsql
AS $$
BEGIN
    RETURN QUERY
    SELECT
        k.id,
        k.title,
        k.knowledge_type,
        k.system,
        k.module,
        k.summary,
        k.content_md,
        k.tags,
        k.core_tables,
        k.related_template_ids,
        k.status,
        k.source_type,
        1 - (k.embedding <=> query_embedding) AS similarity
    FROM knowledge_docs k
    WHERE k.embedding IS NOT NULL
      AND (p_knowledge_type IS NULL OR k.knowledge_type = p_knowledge_type)
      AND (p_system IS NULL OR k.system = p_system)
      AND (p_module IS NULL OR k.module = p_module)
      AND (p_status IS NULL OR k.status = p_status)
      AND (1 - (k.embedding <=> query_embedding)) > match_threshold
    ORDER BY k.embedding <=> query_embedding
    LIMIT match_count;
END;
$$;

-- ============================================================================
-- 关键词检索 RPC（混合检索的关键词召回部分，embedding 不可用时的退化路径）
-- ============================================================================
DROP FUNCTION IF EXISTS search_knowledge_docs_keyword(text, int, text, text, text, text);
CREATE OR REPLACE FUNCTION search_knowledge_docs_keyword(
    keyword text,
    match_count int DEFAULT 10,
    p_knowledge_type text DEFAULT NULL,
    p_system text DEFAULT NULL,
    p_module text DEFAULT NULL,
    p_status text DEFAULT NULL
)
RETURNS TABLE (
    id bigint,
    title text,
    knowledge_type text,
    system text,
    module text,
    summary text,
    content_md text,
    tags text[],
    core_tables text[],
    related_template_ids bigint[],
    status text,
    source_type text,
    rank float
)
LANGUAGE sql STABLE
AS $$
    SELECT
        k.id,
        k.title,
        k.knowledge_type,
        k.system,
        k.module,
        k.summary,
        k.content_md,
        k.tags,
        k.core_tables,
        k.related_template_ids,
        k.status,
        k.source_type,
        (CASE WHEN k.title ILIKE '%'||keyword||'%' THEN 4.0 ELSE 0 END
         + CASE WHEN k.summary ILIKE '%'||keyword||'%' THEN 3.0 ELSE 0 END
         + CASE WHEN array_to_string(k.tags,' ') ILIKE '%'||keyword||'%' THEN 2.0 ELSE 0 END
         + CASE WHEN k.content_md ILIKE '%'||keyword||'%' THEN 1.5 ELSE 0 END
         + CASE WHEN array_to_string(k.core_tables,' ') ILIKE '%'||keyword||'%' THEN 1.0 ELSE 0 END)::float AS rank
    FROM knowledge_docs k
    WHERE (k.title ILIKE '%'||keyword||'%'
        OR k.summary ILIKE '%'||keyword||'%'
        OR k.content_md ILIKE '%'||keyword||'%'
        OR array_to_string(k.tags,' ') ILIKE '%'||keyword||'%'
        OR array_to_string(k.core_tables,' ') ILIKE '%'||keyword||'%')
      AND (p_knowledge_type IS NULL OR k.knowledge_type = p_knowledge_type)
      AND (p_system IS NULL OR k.system = p_system)
      AND (p_module IS NULL OR k.module = p_module)
      AND (p_status IS NULL OR k.status = p_status)
    ORDER BY rank DESC
    LIMIT match_count;
$$;

-- ============================================================================
-- sql_templates：可复用 SQL 行动模板
-- 说明：服务层已经依赖这些字段和 RPC；这里显式建表，避免新环境只有知识表而
--       模板检索在运行时才失败。
-- ============================================================================
CREATE TABLE IF NOT EXISTS sql_templates (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    title               TEXT NOT NULL,
    template_no         TEXT,
    category            TEXT NOT NULL DEFAULT 'query',
    system              TEXT,
    business_domain     TEXT,
    scenario            TEXT NOT NULL DEFAULT '',
    problem_description TEXT,
    symptom             TEXT,
    root_cause           TEXT,
    sql_text            TEXT NOT NULL,
    keywords            TEXT[] NOT NULL DEFAULT '{}',
    core_tables         TEXT[] NOT NULL DEFAULT '{}',
    parameters          JSONB NOT NULL DEFAULT '{}'::jsonb,
    execution_policy    TEXT,
    risk_level          TEXT NOT NULL DEFAULT 'LOW',
    status              TEXT NOT NULL DEFAULT 'draft',
    source_type         TEXT NOT NULL DEFAULT 'manual',
    verified            BOOLEAN NOT NULL DEFAULT FALSE,
    verified_at         TIMESTAMPTZ,
    verified_by         TEXT,
    created_by          TEXT,
    usage_count         INTEGER NOT NULL DEFAULT 0,
    embedding           vector(2048),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sql_templates_title_trgm
    ON sql_templates USING gin (title gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_sql_templates_scenario_trgm
    ON sql_templates USING gin (scenario gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_sql_templates_keywords
    ON sql_templates USING gin (keywords);
CREATE INDEX IF NOT EXISTS idx_sql_templates_core_tables
    ON sql_templates USING gin (core_tables);
CREATE INDEX IF NOT EXISTS idx_sql_templates_category ON sql_templates (category);
CREATE INDEX IF NOT EXISTS idx_sql_templates_system ON sql_templates (system);
CREATE INDEX IF NOT EXISTS idx_sql_templates_domain ON sql_templates (business_domain);
CREATE INDEX IF NOT EXISTS idx_sql_templates_status ON sql_templates (status);
CREATE INDEX IF NOT EXISTS idx_sql_templates_verified ON sql_templates (verified);

DROP TRIGGER IF EXISTS trg_sql_templates_updated_at ON sql_templates;
CREATE TRIGGER trg_sql_templates_updated_at
    BEFORE UPDATE ON sql_templates
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ============================================================================
-- table_catalog：表级事实与自进化使用计数
-- ============================================================================
CREATE TABLE IF NOT EXISTS table_catalog (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    db_name       TEXT NOT NULL DEFAULT 'srm',
    table_name    TEXT NOT NULL,
    table_comment TEXT,
    description   TEXT,
    domain        TEXT,
    key_columns   JSONB NOT NULL DEFAULT '[]'::jsonb,
    entry_columns JSONB NOT NULL DEFAULT '[]'::jsonb,
    tags          TEXT[] NOT NULL DEFAULT '{}',
    verified      BOOLEAN NOT NULL DEFAULT FALSE,
    usage_count   INTEGER NOT NULL DEFAULT 0,
    embedding     vector(2048),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (db_name, table_name)
);

CREATE INDEX IF NOT EXISTS idx_table_catalog_name_trgm
    ON table_catalog USING gin (table_name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_table_catalog_comment_trgm
    ON table_catalog USING gin (table_comment gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_table_catalog_description_trgm
    ON table_catalog USING gin (description gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_table_catalog_tags
    ON table_catalog USING gin (tags);
CREATE INDEX IF NOT EXISTS idx_table_catalog_domain ON table_catalog (domain);
CREATE INDEX IF NOT EXISTS idx_table_catalog_db ON table_catalog (db_name);

DROP TRIGGER IF EXISTS trg_table_catalog_updated_at ON table_catalog;
CREATE TRIGGER trg_table_catalog_updated_at
    BEFORE UPDATE ON table_catalog
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ============================================================================
-- 模板 / 表目录检索与使用计数 RPC
-- ============================================================================
DROP FUNCTION IF EXISTS match_sql_templates(vector, float, int, text, text, text, boolean);
CREATE OR REPLACE FUNCTION match_sql_templates(
    query_embedding vector,
    match_threshold float,
    match_count int DEFAULT 10,
    p_category text DEFAULT NULL,
    p_system text DEFAULT NULL,
    p_business_domain text DEFAULT NULL,
    p_verified_only boolean DEFAULT FALSE
)
RETURNS TABLE (
    id bigint, title text, template_no text, category text, system text,
    business_domain text, scenario text, sql_text text, keywords text[],
    core_tables text[], risk_level text, status text, source_type text,
    verified boolean, verified_at timestamptz, usage_count integer, similarity float
)
LANGUAGE sql STABLE
AS $$
    SELECT t.id, t.title, t.template_no, t.category, t.system,
           t.business_domain, t.scenario, t.sql_text, t.keywords,
           t.core_tables, t.risk_level, t.status, t.source_type,
           t.verified, t.verified_at, t.usage_count,
           (1 - (t.embedding <=> query_embedding))::float AS similarity
    FROM sql_templates t
    WHERE t.embedding IS NOT NULL
      AND (p_category IS NULL OR t.category = p_category)
      AND (p_system IS NULL OR t.system = p_system)
      AND (p_business_domain IS NULL OR t.business_domain = p_business_domain)
      AND (NOT p_verified_only OR t.verified)
      AND (1 - (t.embedding <=> query_embedding)) > match_threshold
    ORDER BY t.embedding <=> query_embedding
    LIMIT match_count;
$$;

DROP FUNCTION IF EXISTS search_sql_templates_keyword(text, int, text, text, text, boolean);
CREATE OR REPLACE FUNCTION search_sql_templates_keyword(
    keyword text,
    match_count int DEFAULT 10,
    p_category text DEFAULT NULL,
    p_system text DEFAULT NULL,
    p_business_domain text DEFAULT NULL,
    p_verified_only boolean DEFAULT FALSE
)
RETURNS TABLE (
    id bigint, title text, template_no text, category text, system text,
    business_domain text, scenario text, sql_text text, keywords text[],
    core_tables text[], risk_level text, status text, source_type text,
    verified boolean, verified_at timestamptz, usage_count integer, rank float
)
LANGUAGE sql STABLE
AS $$
    SELECT t.id, t.title, t.template_no, t.category, t.system,
           t.business_domain, t.scenario, t.sql_text, t.keywords,
           t.core_tables, t.risk_level, t.status, t.source_type,
           t.verified, t.verified_at, t.usage_count,
           (CASE WHEN t.title ILIKE '%' || keyword || '%' THEN 4.0 ELSE 0 END
            + CASE WHEN t.scenario ILIKE '%' || keyword || '%' THEN 3.0 ELSE 0 END
            + CASE WHEN array_to_string(t.keywords, ' ') ILIKE '%' || keyword || '%' THEN 2.0 ELSE 0 END
            + CASE WHEN array_to_string(t.core_tables, ' ') ILIKE '%' || keyword || '%' THEN 1.0 ELSE 0 END)::float AS rank
    FROM sql_templates t
    WHERE (t.title ILIKE '%' || keyword || '%'
        OR t.scenario ILIKE '%' || keyword || '%'
        OR array_to_string(t.keywords, ' ') ILIKE '%' || keyword || '%'
        OR array_to_string(t.core_tables, ' ') ILIKE '%' || keyword || '%')
      AND (p_category IS NULL OR t.category = p_category)
      AND (p_system IS NULL OR t.system = p_system)
      AND (p_business_domain IS NULL OR t.business_domain = p_business_domain)
      AND (NOT p_verified_only OR t.verified)
      AND t.status <> 'deprecated'
    ORDER BY rank DESC, t.updated_at DESC
    LIMIT match_count;
$$;

DROP FUNCTION IF EXISTS search_table_catalog_keyword(text, int, text, text);
CREATE OR REPLACE FUNCTION search_table_catalog_keyword(
    keyword text,
    match_count int DEFAULT 5,
    filter_domain text DEFAULT NULL,
    filter_db text DEFAULT NULL
)
RETURNS TABLE (
    id bigint, db_name text, table_name text, table_comment text,
    description text, domain text, key_columns jsonb, entry_columns jsonb,
    tags text[], verified boolean, usage_count integer, rank float
)
LANGUAGE sql STABLE
AS $$
    SELECT t.id, t.db_name, t.table_name, t.table_comment, t.description,
           t.domain, t.key_columns, t.entry_columns, t.tags, t.verified,
           t.usage_count,
           (CASE WHEN t.table_name ILIKE '%' || keyword || '%' THEN 4.0 ELSE 0 END
            + CASE WHEN COALESCE(t.table_comment, '') ILIKE '%' || keyword || '%' THEN 3.0 ELSE 0 END
            + CASE WHEN COALESCE(t.description, '') ILIKE '%' || keyword || '%' THEN 2.0 ELSE 0 END
            + CASE WHEN array_to_string(t.tags, ' ') ILIKE '%' || keyword || '%' THEN 1.0 ELSE 0 END)::float AS rank
    FROM table_catalog t
    WHERE (t.table_name ILIKE '%' || keyword || '%'
        OR COALESCE(t.table_comment, '') ILIKE '%' || keyword || '%'
        OR COALESCE(t.description, '') ILIKE '%' || keyword || '%'
        OR array_to_string(t.tags, ' ') ILIKE '%' || keyword || '%')
      AND (filter_domain IS NULL OR t.domain = filter_domain)
      AND (filter_db IS NULL OR t.db_name = filter_db)
    ORDER BY rank DESC, t.usage_count DESC, t.table_name
    LIMIT match_count;
$$;

DROP FUNCTION IF EXISTS search_table_catalog(vector, int, text, text);
CREATE OR REPLACE FUNCTION search_table_catalog(
    query_embedding vector,
    match_count int DEFAULT 5,
    filter_domain text DEFAULT NULL,
    filter_db text DEFAULT NULL
)
RETURNS TABLE (
    id bigint, db_name text, table_name text, table_comment text,
    description text, domain text, key_columns jsonb, entry_columns jsonb,
    tags text[], verified boolean, usage_count integer, similarity float
)
LANGUAGE sql STABLE
AS $$
    SELECT t.id, t.db_name, t.table_name, t.table_comment, t.description,
           t.domain, t.key_columns, t.entry_columns, t.tags, t.verified,
           t.usage_count, (1 - (t.embedding <=> query_embedding))::float AS similarity
    FROM table_catalog t
    WHERE t.embedding IS NOT NULL
      AND (filter_domain IS NULL OR t.domain = filter_domain)
      AND (filter_db IS NULL OR t.db_name = filter_db)
    ORDER BY t.embedding <=> query_embedding
    LIMIT match_count;
$$;

DROP FUNCTION IF EXISTS increment_template_usage(bigint);
CREATE OR REPLACE FUNCTION increment_template_usage(p_template_id bigint)
RETURNS void
LANGUAGE sql VOLATILE
AS $$
    UPDATE sql_templates
       SET usage_count = usage_count + 1
     WHERE id = p_template_id;
$$;

DROP FUNCTION IF EXISTS increment_table_usage(text);
CREATE OR REPLACE FUNCTION increment_table_usage(p_table_name text)
RETURNS void
LANGUAGE sql VOLATILE
AS $$
    UPDATE table_catalog
       SET usage_count = usage_count + 1
     WHERE table_name = p_table_name;
$$;

-- ============================================================================
-- table_relations：表与表之间的关联关系（JOIN 候选；机器事实，供 SQL Agent 复用）
-- 设计要点（P0-3 可信度增强）：
--   confidence 0~1：对 join 正确性的置信度
--   verified    是否已经 Archery/SELECT 实测验证过两端字段与 join 结果
--   source      来源：archery_select(实测)/ddl(外键推断)/manual(人工)/inferred(自动推断)
-- SQL Agent 应优先采信 verified=true + source=archery_select 的关系；未验证的仅作候选。
-- ============================================================================
CREATE TABLE IF NOT EXISTS table_relations (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    from_table    TEXT NOT NULL,                           -- 来源表
    to_table      TEXT NOT NULL,                           -- 目标表
    from_db       TEXT NOT NULL DEFAULT 'srm',             -- 来源库
    to_db         TEXT NOT NULL DEFAULT 'srm',             -- 目标库
    join_on       TEXT NOT NULL,                           -- 连接条件（可读，如 a.order_id = b.order_id）
    relation_type TEXT NOT NULL DEFAULT 'ref',             -- 关系类型：ref/fk/many-to-many/...
    description   TEXT DEFAULT '',                         -- 描述
    confidence    DOUBLE PRECISION NOT NULL DEFAULT 1.0,   -- 置信度 0~1
    verified      BOOLEAN NOT NULL DEFAULT FALSE,          -- 是否已实测验证
    source        TEXT NOT NULL DEFAULT 'manual',          -- 来源：archery_select/ddl/manual/inferred
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (from_table, to_table, join_on)                 -- upsert 去重键
);

CREATE INDEX IF NOT EXISTS idx_table_relations_from ON table_relations (from_table);
CREATE INDEX IF NOT EXISTS idx_table_relations_to ON table_relations (to_table);
CREATE INDEX IF NOT EXISTS idx_table_relations_verified ON table_relations (verified);

DROP TRIGGER IF EXISTS trg_table_relations_updated_at ON table_relations;
CREATE TRIGGER trg_table_relations_updated_at
    BEFORE UPDATE ON table_relations
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- 若生产库已有 table_relations 但缺 source 列，执行以下迁移：
-- ALTER TABLE table_relations ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'manual';
-- ALTER TABLE table_relations ADD COLUMN IF NOT EXISTS verified BOOLEAN NOT NULL DEFAULT FALSE;

-- ============================================================================
-- 权限配置（重要，务必执行！）
-- 若未配置，使用 service_role key 连接会报：permission denied for schema public (SQLSTATE 42501)
-- ============================================================================
GRANT USAGE ON SCHEMA public TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO service_role;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO service_role;
GRANT EXECUTE ON FUNCTION match_knowledge_docs(vector, float, int, text, text, text, text) TO service_role;
GRANT EXECUTE ON FUNCTION search_knowledge_docs_keyword(text, int, text, text, text, text) TO service_role;
GRANT EXECUTE ON FUNCTION match_sql_templates(vector, float, int, text, text, text, boolean) TO service_role;
GRANT EXECUTE ON FUNCTION search_sql_templates_keyword(text, int, text, text, text, boolean) TO service_role;
GRANT EXECUTE ON FUNCTION search_table_catalog_keyword(text, int, text, text) TO service_role;
GRANT EXECUTE ON FUNCTION search_table_catalog(vector, int, text, text) TO service_role;
GRANT EXECUTE ON FUNCTION increment_template_usage(bigint) TO service_role;
GRANT EXECUTE ON FUNCTION increment_table_usage(text) TO service_role;
