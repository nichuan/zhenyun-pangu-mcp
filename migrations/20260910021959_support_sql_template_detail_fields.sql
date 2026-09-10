ALTER TABLE public.sql_templates ADD COLUMN IF NOT EXISTS execution_flow TEXT;
ALTER TABLE public.sql_templates ADD COLUMN IF NOT EXISTS example_case TEXT;
ALTER TABLE public.sql_templates ADD COLUMN IF NOT EXISTS problem_description TEXT;
ALTER TABLE public.sql_templates ADD COLUMN IF NOT EXISTS symptom TEXT;
ALTER TABLE public.sql_templates ADD COLUMN IF NOT EXISTS root_cause TEXT;
ALTER TABLE public.sql_templates ADD COLUMN IF NOT EXISTS preconditions TEXT;
ALTER TABLE public.sql_templates ADD COLUMN IF NOT EXISTS diagnosis_steps TEXT;
ALTER TABLE public.sql_templates ADD COLUMN IF NOT EXISTS verify_sql TEXT;
ALTER TABLE public.sql_templates ADD COLUMN IF NOT EXISTS rollback_sql TEXT;
ALTER TABLE public.sql_templates ADD COLUMN IF NOT EXISTS parameters JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE public.sql_templates ADD COLUMN IF NOT EXISTS execution_policy TEXT;
ALTER TABLE public.sql_templates ADD COLUMN IF NOT EXISTS last_used_at TIMESTAMPTZ;

DROP FUNCTION IF EXISTS public.match_sql_templates(
    vector, double precision, integer, text, text, text, boolean
);
CREATE FUNCTION public.match_sql_templates(
    query_embedding vector,
    match_threshold double precision,
    match_count integer DEFAULT 10,
    p_category text DEFAULT NULL,
    p_system text DEFAULT NULL,
    p_business_domain text DEFAULT NULL,
    p_verified_only boolean DEFAULT FALSE
)
RETURNS TABLE (
    id bigint, title text, template_no text, category text, system text,
    business_domain text, scenario text, problem_description text, symptom text,
    root_cause text, preconditions text, diagnosis_steps text,
    execution_flow text, example_case text, sql_text text, verify_sql text,
    rollback_sql text, keywords text[], core_tables text[], parameters jsonb,
    execution_policy text, risk_level text, status text, source_type text,
    verified boolean, verified_at timestamptz, usage_count bigint,
    last_used_at timestamptz, similarity double precision
)
LANGUAGE sql STABLE
SET search_path = public, pg_temp
AS $$
    SELECT t.id, t.title, t.template_no, t.category, t.system,
           t.business_domain, t.scenario, t.problem_description, t.symptom,
           t.root_cause, t.preconditions, t.diagnosis_steps,
           t.execution_flow, t.example_case, t.sql_text, t.verify_sql,
           t.rollback_sql, t.keywords, t.core_tables, t.parameters,
           t.execution_policy, t.risk_level, t.status, t.source_type,
           t.verified, t.verified_at, t.usage_count, t.last_used_at,
           (1 - (t.embedding <=> query_embedding))::double precision AS similarity
    FROM public.sql_templates t
    WHERE t.embedding IS NOT NULL
      AND (p_category IS NULL OR t.category = p_category)
      AND (p_system IS NULL OR t.system = p_system)
      AND (p_business_domain IS NULL OR t.business_domain = p_business_domain)
      AND (NOT p_verified_only OR t.verified)
      AND t.status <> 'deprecated'
      AND (1 - (t.embedding <=> query_embedding)) > match_threshold
    ORDER BY t.embedding <=> query_embedding
    LIMIT match_count;
$$;

DROP FUNCTION IF EXISTS public.search_sql_templates_keyword(
    text, integer, text, text, text, boolean
);
CREATE FUNCTION public.search_sql_templates_keyword(
    keyword text,
    match_count integer DEFAULT 10,
    p_category text DEFAULT NULL,
    p_system text DEFAULT NULL,
    p_business_domain text DEFAULT NULL,
    p_verified_only boolean DEFAULT FALSE
)
RETURNS TABLE (
    id bigint, title text, template_no text, category text, system text,
    business_domain text, scenario text, problem_description text, symptom text,
    root_cause text, preconditions text, diagnosis_steps text,
    execution_flow text, example_case text, sql_text text, verify_sql text,
    rollback_sql text, keywords text[], core_tables text[], parameters jsonb,
    execution_policy text, risk_level text, status text, source_type text,
    verified boolean, verified_at timestamptz, usage_count bigint,
    last_used_at timestamptz, rank double precision
)
LANGUAGE sql STABLE
SET search_path = public, pg_temp
AS $$
    SELECT t.id, t.title, t.template_no, t.category, t.system,
           t.business_domain, t.scenario, t.problem_description, t.symptom,
           t.root_cause, t.preconditions, t.diagnosis_steps,
           t.execution_flow, t.example_case, t.sql_text, t.verify_sql,
           t.rollback_sql, t.keywords, t.core_tables, t.parameters,
           t.execution_policy, t.risk_level, t.status, t.source_type,
           t.verified, t.verified_at, t.usage_count, t.last_used_at,
           (CASE WHEN t.title ILIKE '%' || keyword || '%' THEN 6.0 ELSE 0 END
            + CASE WHEN t.scenario ILIKE '%' || keyword || '%' THEN 5.0 ELSE 0 END
            + CASE WHEN COALESCE(t.problem_description, '') ILIKE '%' || keyword || '%' THEN 4.0 ELSE 0 END
            + CASE WHEN COALESCE(t.symptom, '') ILIKE '%' || keyword || '%' THEN 4.0 ELSE 0 END
            + CASE WHEN COALESCE(t.root_cause, '') ILIKE '%' || keyword || '%' THEN 4.0 ELSE 0 END
            + CASE WHEN COALESCE(t.execution_flow, '') ILIKE '%' || keyword || '%' THEN 3.0 ELSE 0 END
            + CASE WHEN COALESCE(t.example_case, '') ILIKE '%' || keyword || '%' THEN 3.0 ELSE 0 END
            + CASE WHEN COALESCE(t.diagnosis_steps, '') ILIKE '%' || keyword || '%' THEN 3.0 ELSE 0 END
            + CASE WHEN array_to_string(t.keywords, ' ') ILIKE '%' || keyword || '%' THEN 2.0 ELSE 0 END
            + CASE WHEN array_to_string(t.core_tables, ' ') ILIKE '%' || keyword || '%' THEN 2.0 ELSE 0 END
            + CASE WHEN t.sql_text ILIKE '%' || keyword || '%' THEN 1.0 ELSE 0 END
            + CASE WHEN COALESCE(t.verify_sql, '') ILIKE '%' || keyword || '%' THEN 1.0 ELSE 0 END
           )::double precision AS rank
    FROM public.sql_templates t
    WHERE (t.title ILIKE '%' || keyword || '%'
        OR t.scenario ILIKE '%' || keyword || '%'
        OR COALESCE(t.problem_description, '') ILIKE '%' || keyword || '%'
        OR COALESCE(t.symptom, '') ILIKE '%' || keyword || '%'
        OR COALESCE(t.root_cause, '') ILIKE '%' || keyword || '%'
        OR COALESCE(t.preconditions, '') ILIKE '%' || keyword || '%'
        OR COALESCE(t.diagnosis_steps, '') ILIKE '%' || keyword || '%'
        OR COALESCE(t.execution_flow, '') ILIKE '%' || keyword || '%'
        OR COALESCE(t.example_case, '') ILIKE '%' || keyword || '%'
        OR t.sql_text ILIKE '%' || keyword || '%'
        OR COALESCE(t.verify_sql, '') ILIKE '%' || keyword || '%'
        OR COALESCE(t.rollback_sql, '') ILIKE '%' || keyword || '%'
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

DROP FUNCTION IF EXISTS public.increment_template_usage(bigint);
CREATE FUNCTION public.increment_template_usage(p_template_id bigint)
RETURNS void
LANGUAGE sql VOLATILE
SET search_path = public, pg_temp
AS $$
    UPDATE public.sql_templates
       SET usage_count = usage_count + 1,
           last_used_at = NOW()
     WHERE id = p_template_id;
$$;

GRANT EXECUTE ON FUNCTION public.match_sql_templates(
    vector, double precision, integer, text, text, text, boolean
) TO service_role;
GRANT EXECUTE ON FUNCTION public.search_sql_templates_keyword(
    text, integer, text, text, text, boolean
) TO service_role;
GRANT EXECUTE ON FUNCTION public.increment_template_usage(bigint) TO service_role;

NOTIFY pgrst, 'reload schema';
