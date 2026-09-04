-- 切换语义向量模型：NVIDIA/Voyage(2048 维) -> Cloudflare Workers AI @cf/qwen/qwen3-embedding-0.6b(1024 维)。
-- 旧 2048 维向量与新模型语义空间不兼容，必须先清空再改列维度。
-- 执行方式：在 Supabase SQL Editor 中执行（幂等，可重复执行）。

-- 1) 清空旧向量（幂等；回填脚本会重新生成）
UPDATE knowledge_docs SET embedding = NULL WHERE embedding IS NOT NULL;
UPDATE sql_templates SET embedding = NULL WHERE embedding IS NOT NULL;
UPDATE table_catalog SET embedding = NULL WHERE embedding IS NOT NULL;

-- 2) 收缩列维度 2048 -> 1024（pgvector 无 2048->1024 cast，必须先清空再改列）
ALTER TABLE knowledge_docs   ALTER COLUMN embedding TYPE vector(1024);
ALTER TABLE sql_templates    ALTER COLUMN embedding TYPE vector(1024);
ALTER TABLE table_catalog    ALTER COLUMN embedding TYPE vector(1024);

-- 3)（可选）1024 维低于 HNSW 2000 维上限，数据规模上万后可加 ANN 索引：
-- CREATE INDEX IF NOT EXISTS idx_knowledge_docs_embedding_hnsw
--     ON knowledge_docs USING hnsw (embedding vector_cosine_ops);
