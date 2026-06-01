"""APDL Config Service -- FastAPI application entry point."""

import logging
import os
from contextlib import asynccontextmanager

import asyncpg
import redis.asyncio as aioredis
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routers import admin, flags, stream
from app.sse.broadcaster import SSEBroadcaster

logger = logging.getLogger(__name__)

CREATE_FLAGS_TABLE = """
CREATE TABLE IF NOT EXISTS flags (
    key TEXT NOT NULL,
    project_id TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    enabled BOOLEAN NOT NULL DEFAULT false,
    description TEXT NOT NULL DEFAULT '',
    default_value BOOLEAN NOT NULL DEFAULT false,
    rules JSONB NOT NULL DEFAULT '[]'::jsonb,
    fallthrough JSONB NOT NULL DEFAULT '{"value":false,"rollout":{"percentage":0,"bucket_by":"user_id"}}'::jsonb,
    salt TEXT NOT NULL DEFAULT md5(random()::text || clock_timestamp()::text),
    client_exposed BOOLEAN NOT NULL DEFAULT true,
    auto_disable BOOLEAN NOT NULL DEFAULT true,
    guardrails JSONB NOT NULL DEFAULT '[]'::jsonb,
    disabled_reason TEXT NOT NULL DEFAULT '',
    disabled_by TEXT NOT NULL DEFAULT '',
    disabled_at TIMESTAMPTZ,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    archived_at TIMESTAMPTZ,
    PRIMARY KEY (project_id, key)
);
"""

MIGRATE_FLAGS_TABLE = """
ALTER TABLE flags ADD COLUMN IF NOT EXISTS name TEXT NOT NULL DEFAULT '';
ALTER TABLE flags ADD COLUMN IF NOT EXISTS rules JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE flags ADD COLUMN IF NOT EXISTS fallthrough JSONB NOT NULL DEFAULT '{"value":false,"rollout":{"percentage":0,"bucket_by":"user_id"}}'::jsonb;
ALTER TABLE flags ADD COLUMN IF NOT EXISTS salt TEXT NOT NULL DEFAULT md5(random()::text || clock_timestamp()::text);
ALTER TABLE flags ADD COLUMN IF NOT EXISTS client_exposed BOOLEAN NOT NULL DEFAULT true;
ALTER TABLE flags ADD COLUMN IF NOT EXISTS auto_disable BOOLEAN NOT NULL DEFAULT true;
ALTER TABLE flags ADD COLUMN IF NOT EXISTS guardrails JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE flags ADD COLUMN IF NOT EXISTS disabled_reason TEXT NOT NULL DEFAULT '';
ALTER TABLE flags ADD COLUMN IF NOT EXISTS disabled_by TEXT NOT NULL DEFAULT '';
ALTER TABLE flags ADD COLUMN IF NOT EXISTS disabled_at TIMESTAMPTZ;
ALTER TABLE flags ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE flags ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ;

ALTER TABLE flags ALTER COLUMN default_value DROP DEFAULT;
ALTER TABLE flags
    ALTER COLUMN default_value TYPE BOOLEAN
    USING CASE WHEN lower(default_value::text) = 'true' THEN true ELSE false END;
ALTER TABLE flags ALTER COLUMN default_value SET DEFAULT false;
ALTER TABLE flags ALTER COLUMN default_value SET NOT NULL;

UPDATE flags SET name = key WHERE name = '';
UPDATE flags
SET salt = md5(random()::text || clock_timestamp()::text || project_id || key)
WHERE salt = '';

DO $$
BEGIN
    IF to_regclass('public.feature_flags') IS NOT NULL THEN
        INSERT INTO flags (
            key, project_id, name, enabled, description, default_value,
            rules, fallthrough, salt, client_exposed, auto_disable,
            guardrails, created_at, updated_at
        )
        SELECT
            flag_key,
            project_id,
            name,
            COALESCE(enabled, false),
            COALESCE(description, ''),
            false,
            COALESCE(rules, '[]'::jsonb),
            jsonb_build_object(
                'value', true,
                'rollout', jsonb_build_object(
                    'percentage', COALESCE(rollout_percentage, 0),
                    'bucket_by', 'user_id'
                )
            ),
            salt,
            true,
            true,
            '[]'::jsonb,
            COALESCE(created_at, NOW()),
            COALESCE(updated_at, NOW())
        FROM feature_flags
        ON CONFLICT (project_id, key) DO NOTHING;

        DROP TABLE feature_flags;
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'flags' AND column_name = 'rollout_percentage'
    ) THEN
        EXECUTE $sql$
            UPDATE flags
            SET fallthrough = jsonb_build_object(
                'value', true,
                'rollout', jsonb_build_object(
                    'percentage', rollout_percentage,
                    'bucket_by', 'user_id'
                )
            )
            WHERE fallthrough = '{"value":false,"rollout":{"percentage":0,"bucket_by":"user_id"}}'::jsonb
        $sql$;
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'flags' AND column_name = 'rules_json'
    ) AND EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'flags' AND column_name = 'rollout_percentage'
    ) THEN
        EXECUTE $sql$
            WITH migrated AS (
                SELECT
                    f.project_id,
                    f.key,
                    COALESCE(
                        jsonb_agg(
                            jsonb_build_object(
                                'id', COALESCE(NULLIF(rule->>'id', ''), 'rule_' || ordinality::text),
                                'name', COALESCE(rule->>'name', ''),
                                'conditions',
                                    CASE
                                        WHEN jsonb_typeof(rule->'conditions') = 'array'
                                            THEN rule->'conditions'
                                        ELSE jsonb_build_array(rule - 'id' - 'name' - 'rollout')
                                    END,
                                'rollout',
                                    CASE
                                        WHEN jsonb_typeof(rule->'rollout') = 'object'
                                            THEN rule->'rollout'
                                        ELSE jsonb_build_object(
                                            'percentage', f.rollout_percentage,
                                            'bucket_by', 'user_id'
                                        )
                                    END
                            )
                        ) FILTER (WHERE jsonb_typeof(rule) = 'object'),
                        '[]'::jsonb
                    ) AS rules
                FROM flags f
                CROSS JOIN LATERAL jsonb_array_elements(f.rules_json::jsonb)
                    WITH ORDINALITY AS parsed(rule, ordinality)
                GROUP BY f.project_id, f.key
            )
            UPDATE flags f
            SET rules = migrated.rules
            FROM migrated
            WHERE f.project_id = migrated.project_id
              AND f.key = migrated.key
              AND f.rules = '[]'::jsonb
        $sql$;
    END IF;
END $$;

ALTER TABLE flags DROP COLUMN IF EXISTS variant_type;
ALTER TABLE flags DROP COLUMN IF EXISTS rules_json;
ALTER TABLE flags DROP COLUMN IF EXISTS variants_json;
ALTER TABLE flags DROP COLUMN IF EXISTS rollout_percentage;
"""

CREATE_FLAG_AUDIT_TABLE = """
CREATE TABLE IF NOT EXISTS flag_audit_log (
    id BIGSERIAL PRIMARY KEY,
    project_id TEXT NOT NULL,
    flag_key TEXT NOT NULL,
    action TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT 'system',
    previous_version INTEGER,
    new_version INTEGER,
    before JSONB,
    after JSONB,
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    reason TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

MIGRATE_FLAG_AUDIT_TABLE = """
ALTER TABLE flag_audit_log ADD COLUMN IF NOT EXISTS evidence JSONB NOT NULL DEFAULT '{}'::jsonb;
"""

CREATE_EXPERIMENTS_TABLE = """
CREATE TABLE IF NOT EXISTS experiments (
    key TEXT NOT NULL,
    project_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    description TEXT NOT NULL DEFAULT '',
    variants_json TEXT NOT NULL DEFAULT '[]',
    targeting_rules_json TEXT NOT NULL DEFAULT '[]',
    traffic_percentage DOUBLE PRECISION NOT NULL DEFAULT 100.0,
    start_date TEXT NOT NULL DEFAULT '',
    end_date TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (project_id, key)
);
"""

CREATE_FLAGS_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_flags_project_updated "
    "ON flags (project_id, archived_at, updated_at DESC);"
)

CREATE_FLAG_AUDIT_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_flag_audit_project_flag "
    "ON flag_audit_log (project_id, flag_key, created_at DESC);"
)

CREATE_EXPERIMENTS_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_experiments_project_updated "
    "ON experiments (project_id, updated_at DESC);"
)


@asynccontextmanager
async def lifespan(application: FastAPI):
    """Manage startup/shutdown of shared resources."""
    # PostgreSQL connection pool
    pg_dsn = os.environ.get(
        "POSTGRES_URL",
        "postgresql://apdl:apdl_dev@localhost:5432/apdl",
    )
    pg_pool_size = int(os.environ.get("PG_POOL_SIZE", "4"))

    pg_pool = await asyncpg.create_pool(
        dsn=pg_dsn, min_size=2, max_size=pg_pool_size
    )
    logger.info("PostgreSQL connection pool initialized")

    # Initialize schema
    async with pg_pool.acquire() as conn:
        await conn.execute(CREATE_FLAGS_TABLE)
        await conn.execute(MIGRATE_FLAGS_TABLE)
        await conn.execute(CREATE_FLAG_AUDIT_TABLE)
        await conn.execute(MIGRATE_FLAG_AUDIT_TABLE)
        await conn.execute(CREATE_EXPERIMENTS_TABLE)
        await conn.execute(CREATE_FLAGS_INDEX)
        await conn.execute(CREATE_FLAG_AUDIT_INDEX)
        await conn.execute(CREATE_EXPERIMENTS_INDEX)
    logger.info("Database schema initialized")

    # Redis
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
    redis_client = aioredis.from_url(redis_url)
    logger.info("Redis connection initialized")

    # SSE Broadcaster
    broadcaster = SSEBroadcaster()
    await broadcaster.start()
    logger.info("SSE broadcaster started")

    # Store in app state
    application.state.pg_pool = pg_pool
    application.state.redis = redis_client
    application.state.broadcaster = broadcaster

    yield

    # Shutdown
    await broadcaster.stop()
    logger.info("SSE broadcaster stopped")

    await redis_client.aclose()
    logger.info("Redis connection closed")

    await pg_pool.close()
    logger.info("PostgreSQL connection pool closed")


app = FastAPI(
    title="APDL Config Service",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(flags.router)
app.include_router(stream.router)
app.include_router(admin.router)


@app.get("/health")
async def health_check():
    """Liveness/readiness probe -- checks PG, Redis, and SSE connection count."""
    status = {"status": "ok", "service": "apdl-config"}

    try:
        async with app.state.pg_pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        status["postgres"] = "ok"
    except Exception as exc:
        logger.error("Health check: PostgreSQL error: %s", exc)
        status["postgres"] = "error"
        status["status"] = "degraded"

    try:
        await app.state.redis.ping()
        status["redis"] = "ok"
    except Exception as exc:
        logger.error("Health check: Redis error: %s", exc)
        status["redis"] = "error"
        status["status"] = "degraded"

    status["sse_connections"] = await app.state.broadcaster.total_connection_count()

    return status
