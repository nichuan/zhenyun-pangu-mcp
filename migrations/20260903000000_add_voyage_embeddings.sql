-- Voyage A/B migration for the knowledge base.
-- Safe phase: adds new columns and new RPCs only; never overwrites or drops NVIDIA data.
-- Apply this file manually after reviewing it in the Supabase SQL editor.

CREATE EXTENSION IF NOT EXISTS vector;

ALTER TABLE knowledge_docs
    ADD COLUMN IF NOT EXISTS embedding_voyage vector(2048),
    ADD COLUMN IF NOT EXISTS embedding_voyage_provider TEXT,
    ADD COLUMN IF NOT EXISTS embedding_voyage_model TEXT,
    ADD COLUMN IF NOT EXISTS embedding_voyage_dimension INTEGER,
    ADD COLUMN IF NOT EXISTS embedding_voyage_updated_at TIMESTAMPTZ;

ALTER TABLE sql_templates
    ADD COLUMN IF NOT EXISTS embedding_voyage vector(2048),
    ADD COLUMN IF NOT EXISTS embedding_voyage_provider TEXT,
    ADD COLUMN IF NOT EXISTS embedding_voyage_model TEXT,
    ADD COLUMN IF NOT EXISTS embedding_voyage_dimension INTEGER,
    ADD COLUMN IF NOT EXISTS embedding_voyage_updated_at TIMESTAMPTZ;

ALTER TABLE table_catalog
    ADD COLUMN IF NOT EXISTS embedding_voyage vector(2048),
    ADD COLUMN IF NOT EXISTS embedding_voyage_provider TEXT,
    ADD COLUMN IF NOT EXISTS embedding_voyage_model TEXT,
    ADD COLUMN IF NOT EXISTS embedding_voyage_dimension INTEGER,
    ADD COLUMN IF NOT EXISTS embedding_voyage_updated_at TIMESTAMPTZ;

-- Keep cosine distance and the old RPC signatures. These RPCs only read the new
-- Voyage column, so a partial backfill cannot mix NVIDIA and Voyage vectors.
DROP FUNCTION IF EXISTS match_knowledge_docs_voyage(vector, float, int, text, text, text, text);
CREATE OR REPLACE FUNCTION match_knowledge_docs_voyage(
    query_embedding vector,
    match_threshold float,
    match_count int DEFAULT 10,
    p_knowledge_type text DEFAULT NULL,
    p_system text DEFAULT NULL,
    p_module text DEFAULT NULL,
    p_status text DEFAULT NULL
)
RETURNS TABLE (
    id bigint, title text, knowledge_type text, system text, module text,
    summary text, content_md text, tags text[], core_tables text[],
    related_template_ids bigint[], status text, source_type text, similarity float
)
LANGUAGE sql STABLE
AS $$
    SELECT k.id, k.title, k.knowledge_type, k.system, k.module, k.summary,
           k.content_md, k.tags, k.core_tables, k.related_template_ids,
           k.status, k.source_type,
           (1 - (k.embedding_voyage <=> query_embedding))::float AS similarity
    FROM knowledge_docs k
    WHERE k.embedding_voyage IS NOT NULL
      AND (p_knowledge_type IS NULL OR k.knowledge_type = p_knowledge_type)
      AND (p_system IS NULL OR k.system = p_system)
      AND (p_module IS NULL OR k.module = p_module)
      AND (p_status IS NULL OR k.status = p_status)
      AND (1 - (k.embedding_voyage <=> query_embedding)) > match_threshold
    ORDER BY k.embedding_voyage <=> query_embedding
    LIMIT match_count;
$$;

DROP FUNCTION IF EXISTS match_sql_templates_voyage(vector, float, int, text, text, text, boolean);
CREATE OR REPLACE FUNCTION match_sql_templates_voyage(
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
           (1 - (t.embedding_voyage <=> query_embedding))::float AS similarity
    FROM sql_templates t
    WHERE t.embedding_voyage IS NOT NULL
      AND (p_category IS NULL OR t.category = p_category)
      AND (p_system IS NULL OR t.system = p_system)
      AND (p_business_domain IS NULL OR t.business_domain = p_business_domain)
      AND (NOT p_verified_only OR t.verified)
      AND (1 - (t.embedding_voyage <=> query_embedding)) > match_threshold
    ORDER BY t.embedding_voyage <=> query_embedding
    LIMIT match_count;
$$;

DROP FUNCTION IF EXISTS search_table_catalog_voyage(vector, int, text, text);
CREATE OR REPLACE FUNCTION search_table_catalog_voyage(
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
           t.usage_count,
           (1 - (t.embedding_voyage <=> query_embedding))::float AS similarity
    FROM table_catalog t
    WHERE t.embedding_voyage IS NOT NULL
      AND (filter_domain IS NULL OR t.domain = filter_domain)
      AND (filter_db IS NULL OR t.db_name = filter_db)
    ORDER BY t.embedding_voyage <=> query_embedding
    LIMIT match_count;
$$;

REVOKE ALL ON FUNCTION match_knowledge_docs_voyage(vector, float, int, text, text, text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION match_sql_templates_voyage(vector, float, int, text, text, text, boolean) FROM PUBLIC;
REVOKE ALL ON FUNCTION search_table_catalog_voyage(vector, int, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION match_knowledge_docs_voyage(vector, float, int, text, text, text, text) TO service_role;
GRANT EXECUTE ON FUNCTION match_sql_templates_voyage(vector, float, int, text, text, text, boolean) TO service_role;
GRANT EXECUTE ON FUNCTION search_table_catalog_voyage(vector, int, text, text) TO service_role;
