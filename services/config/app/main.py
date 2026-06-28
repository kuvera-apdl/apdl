"""APDL Config Service -- FastAPI application entry point."""

import asyncio
import logging
import os
from contextlib import asynccontextmanager

import asyncpg
import redis.asyncio as aioredis
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.experiments import expiry
from app.routers import admin, evaluate, flags, stream
from app.sse.broadcaster import SSEBroadcaster

logger = logging.getLogger(__name__)

CREATE_FLAGS_TABLE = """
CREATE TABLE IF NOT EXISTS flags (
    key TEXT NOT NULL,
    project_id TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'draft'
        CHECK (state IN ('draft', 'active', 'disabled', 'archived')),
    owners JSONB NOT NULL DEFAULT '[]'::jsonb,
    review_by TEXT,
    enabled BOOLEAN NOT NULL DEFAULT false,
    description TEXT NOT NULL DEFAULT '',
    default_variant TEXT NOT NULL DEFAULT 'control',
    variants JSONB NOT NULL DEFAULT '[{"key":"control","weight":1},{"key":"treatment","weight":1}]'::jsonb,
    rules JSONB NOT NULL DEFAULT '[]'::jsonb,
    fallthrough JSONB NOT NULL DEFAULT '{"rollout":{"percentage":0,"bucket_by":"user_id"}}'::jsonb,
    salt TEXT NOT NULL DEFAULT md5(random()::text || clock_timestamp()::text),
    evaluation_mode TEXT NOT NULL DEFAULT 'client'
        CHECK (evaluation_mode IN ('client', 'server', 'both')),
    auto_disable BOOLEAN NOT NULL DEFAULT true,
    guardrails JSONB NOT NULL DEFAULT '[]'::jsonb,
    disabled_reason TEXT NOT NULL DEFAULT '',
    disabled_by TEXT NOT NULL DEFAULT '',
    disabled_at TIMESTAMPTZ,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    archived_at TIMESTAMPTZ,
    CONSTRAINT flags_default_variant_non_empty_check CHECK (
        default_variant IS NOT NULL AND default_variant <> ''
    ),
    CONSTRAINT flags_variants_array_non_empty_check CHECK (
        CASE
            WHEN jsonb_typeof(variants) <> 'array' THEN false
            ELSE jsonb_array_length(variants) > 0
        END
    ),
    CONSTRAINT flags_rules_array_check CHECK (jsonb_typeof(rules) = 'array'),
    CONSTRAINT flags_fallthrough_rollout_only_check CHECK (
        CASE
            WHEN jsonb_typeof(fallthrough) <> 'object' THEN false
            ELSE (fallthrough ? 'rollout')
                AND (fallthrough - 'rollout') = '{}'::jsonb
        END
    ),
    CONSTRAINT flags_state_enabled_check CHECK ((state = 'active') = enabled),
    PRIMARY KEY (project_id, key)
);
"""

MIGRATE_FLAGS_TABLE = """
ALTER TABLE flags ADD COLUMN IF NOT EXISTS name TEXT NOT NULL DEFAULT '';
ALTER TABLE flags ADD COLUMN IF NOT EXISTS state TEXT NOT NULL DEFAULT 'draft';
ALTER TABLE flags ADD COLUMN IF NOT EXISTS owners JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE flags ADD COLUMN IF NOT EXISTS review_by TEXT;
ALTER TABLE flags ADD COLUMN IF NOT EXISTS default_variant TEXT NOT NULL DEFAULT 'control';
ALTER TABLE flags ADD COLUMN IF NOT EXISTS variants JSONB NOT NULL DEFAULT '[{"key":"control","weight":1},{"key":"treatment","weight":1}]'::jsonb;
ALTER TABLE flags ADD COLUMN IF NOT EXISTS rules JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE flags ADD COLUMN IF NOT EXISTS fallthrough JSONB NOT NULL DEFAULT '{"rollout":{"percentage":0,"bucket_by":"user_id"}}'::jsonb;
ALTER TABLE flags ADD COLUMN IF NOT EXISTS salt TEXT NOT NULL DEFAULT md5(random()::text || clock_timestamp()::text);
ALTER TABLE flags ADD COLUMN IF NOT EXISTS evaluation_mode TEXT NOT NULL DEFAULT 'client';
ALTER TABLE flags ADD COLUMN IF NOT EXISTS auto_disable BOOLEAN NOT NULL DEFAULT true;
ALTER TABLE flags ADD COLUMN IF NOT EXISTS guardrails JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE flags ADD COLUMN IF NOT EXISTS disabled_reason TEXT NOT NULL DEFAULT '';
ALTER TABLE flags ADD COLUMN IF NOT EXISTS disabled_by TEXT NOT NULL DEFAULT '';
ALTER TABLE flags ADD COLUMN IF NOT EXISTS disabled_at TIMESTAMPTZ;
ALTER TABLE flags ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE flags ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ;

UPDATE flags SET name = key WHERE name = '';
UPDATE flags
SET salt = md5(random()::text || clock_timestamp()::text || project_id || key)
WHERE salt = '';

UPDATE flags
SET default_variant = 'control'
WHERE default_variant IS NULL OR default_variant = '';

UPDATE flags
SET variants = '[{"key":"control","weight":1},{"key":"treatment","weight":1}]'::jsonb
WHERE CASE
    WHEN variants IS NULL THEN true
    WHEN jsonb_typeof(variants) <> 'array' THEN true
    ELSE jsonb_array_length(variants) = 0
END;

UPDATE flags
SET default_variant = 'control',
    variants = '[{"key":"control","weight":1},{"key":"treatment","weight":1}]'::jsonb
WHERE EXISTS (
        SELECT 1
        FROM jsonb_array_elements(variants) AS variant(value)
        WHERE jsonb_typeof(variant.value) <> 'object'
           OR COALESCE(variant.value->>'key', '') = ''
           OR COALESCE(variant.value->>'weight', '') !~ '^(0|[1-9][0-9]*)$'
    )
   OR NOT EXISTS (
        SELECT 1
        FROM jsonb_array_elements(variants) AS variant(value)
        WHERE variant.value->>'key' = default_variant
    )
   OR (
        SELECT COUNT(*)
        FROM jsonb_array_elements(variants) AS variant(value)
    ) <> (
        SELECT COUNT(DISTINCT variant.value->>'key')
        FROM jsonb_array_elements(variants) AS variant(value)
    )
   OR COALESCE((
        SELECT SUM((variant.value->>'weight')::numeric)
        FROM jsonb_array_elements(variants) AS variant(value)
        WHERE COALESCE(variant.value->>'weight', '') ~ '^(0|[1-9][0-9]*)$'
    ), 0) <= 0;

UPDATE flags
SET rules = '[]'::jsonb
WHERE rules IS NULL OR jsonb_typeof(rules) <> 'array';

UPDATE flags
SET fallthrough = jsonb_build_object(
    'rollout',
    CASE
        WHEN fallthrough IS NOT NULL
         AND jsonb_typeof(fallthrough) = 'object'
         AND jsonb_typeof(fallthrough->'rollout') = 'object'
        THEN fallthrough->'rollout'
        ELSE jsonb_build_object('percentage', 0, 'bucket_by', 'user_id')
    END
)
WHERE CASE
    WHEN fallthrough IS NULL THEN true
    WHEN jsonb_typeof(fallthrough) <> 'object' THEN true
    WHEN NOT (fallthrough ? 'rollout') THEN true
    ELSE (fallthrough - 'rollout') <> '{}'::jsonb
END;
UPDATE flags
SET state = CASE
    WHEN archived_at IS NOT NULL THEN 'archived'
    WHEN disabled_at IS NOT NULL OR disabled_reason <> '' THEN 'disabled'
    WHEN enabled THEN 'active'
    ELSE 'draft'
END
WHERE state = 'draft'
  AND (archived_at IS NOT NULL OR disabled_at IS NOT NULL OR disabled_reason <> '' OR enabled);

DO $$
BEGIN
    IF to_regclass('public.feature_flags') IS NOT NULL THEN
        DROP TABLE feature_flags;
    END IF;
END $$;

UPDATE flags
SET enabled = (state = 'active')
WHERE enabled IS DISTINCT FROM (state = 'active');

ALTER TABLE flags DROP CONSTRAINT IF EXISTS flags_evaluation_mode_check;
ALTER TABLE flags ADD CONSTRAINT flags_evaluation_mode_check
    CHECK (evaluation_mode IN ('client', 'server', 'both'));
ALTER TABLE flags DROP CONSTRAINT IF EXISTS flags_state_check;
ALTER TABLE flags ADD CONSTRAINT flags_state_check
    CHECK (state IN ('draft', 'active', 'disabled', 'archived'));
ALTER TABLE flags DROP CONSTRAINT IF EXISTS flags_state_enabled_check;
ALTER TABLE flags ADD CONSTRAINT flags_state_enabled_check
    CHECK ((state = 'active') = enabled);
ALTER TABLE flags ALTER COLUMN default_variant SET DEFAULT 'control';
ALTER TABLE flags ALTER COLUMN default_variant SET NOT NULL;
ALTER TABLE flags ALTER COLUMN variants SET DEFAULT '[{"key":"control","weight":1},{"key":"treatment","weight":1}]'::jsonb;
ALTER TABLE flags ALTER COLUMN variants SET NOT NULL;
ALTER TABLE flags ALTER COLUMN rules SET DEFAULT '[]'::jsonb;
ALTER TABLE flags ALTER COLUMN rules SET NOT NULL;
ALTER TABLE flags ALTER COLUMN fallthrough SET DEFAULT '{"rollout":{"percentage":0,"bucket_by":"user_id"}}'::jsonb;
ALTER TABLE flags ALTER COLUMN fallthrough SET NOT NULL;
ALTER TABLE flags DROP CONSTRAINT IF EXISTS flags_default_variant_non_empty_check;
ALTER TABLE flags ADD CONSTRAINT flags_default_variant_non_empty_check
    CHECK (default_variant IS NOT NULL AND default_variant <> '');
ALTER TABLE flags DROP CONSTRAINT IF EXISTS flags_variants_array_non_empty_check;
ALTER TABLE flags ADD CONSTRAINT flags_variants_array_non_empty_check
    CHECK (
        CASE
            WHEN jsonb_typeof(variants) <> 'array' THEN false
            ELSE jsonb_array_length(variants) > 0
        END
    );
ALTER TABLE flags DROP CONSTRAINT IF EXISTS flags_rules_array_check;
ALTER TABLE flags ADD CONSTRAINT flags_rules_array_check
    CHECK (jsonb_typeof(rules) = 'array');
ALTER TABLE flags DROP CONSTRAINT IF EXISTS flags_fallthrough_rollout_only_check;
ALTER TABLE flags ADD CONSTRAINT flags_fallthrough_rollout_only_check
    CHECK (
        CASE
            WHEN jsonb_typeof(fallthrough) <> 'object' THEN false
            ELSE (fallthrough ? 'rollout')
                AND (fallthrough - 'rollout') = '{}'::jsonb
        END
    );

ALTER TABLE flags DROP COLUMN IF EXISTS default_value;
ALTER TABLE flags DROP COLUMN IF EXISTS variant_type;
ALTER TABLE flags DROP COLUMN IF EXISTS rules_json;
ALTER TABLE flags DROP COLUMN IF EXISTS variants_json;
ALTER TABLE flags DROP COLUMN IF EXISTS rollout_percentage;
ALTER TABLE flags DROP COLUMN IF EXISTS client_exposed;
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
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'running', 'completed', 'stopped')),
    description TEXT NOT NULL DEFAULT '',
    flag_key TEXT NOT NULL DEFAULT '',
    default_variant TEXT NOT NULL DEFAULT 'control',
    variants_json TEXT NOT NULL DEFAULT '[]',
    targeting_rules_json TEXT NOT NULL DEFAULT '[]',
    primary_metric_json TEXT NOT NULL DEFAULT '{}',
    traffic_percentage DOUBLE PRECISION NOT NULL DEFAULT 100.0,
    start_date TEXT NOT NULL DEFAULT '',
    end_date TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (project_id, key)
);
"""

MIGRATE_EXPERIMENTS_TABLE = """
ALTER TABLE experiments ADD COLUMN IF NOT EXISTS flag_key TEXT NOT NULL DEFAULT '';
ALTER TABLE experiments ADD COLUMN IF NOT EXISTS default_variant TEXT NOT NULL DEFAULT 'control';
ALTER TABLE experiments ADD COLUMN IF NOT EXISTS primary_metric_json TEXT NOT NULL DEFAULT '{}';

-- The flag-key link becomes a stored column: default it to the experiment key.
UPDATE experiments SET flag_key = key WHERE flag_key = '';

-- Normalize legacy status values onto the canonical lifecycle literal before
-- enforcing the constraint (the agent previously wrote 'active').
UPDATE experiments SET status = 'running' WHERE status = 'active';
UPDATE experiments SET status = 'draft'
    WHERE status NOT IN ('draft', 'running', 'completed', 'stopped');

ALTER TABLE experiments DROP CONSTRAINT IF EXISTS experiments_status_check;
ALTER TABLE experiments ADD CONSTRAINT experiments_status_check
    CHECK (status IN ('draft', 'running', 'completed', 'stopped'));
"""

CREATE_FLAGS_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_flags_project_updated "
    "ON flags (project_id, archived_at, updated_at DESC);"
)

CREATE_FLAGS_LIFECYCLE_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_flags_project_state_review "
    "ON flags (project_id, state, review_by, updated_at DESC);"
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
        await conn.execute(MIGRATE_EXPERIMENTS_TABLE)
        await conn.execute(CREATE_FLAGS_INDEX)
        await conn.execute(CREATE_FLAGS_LIFECYCLE_INDEX)
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

    # Experiment expiry monitor: completes running experiments past their
    # end_date (cascading to disable their backing flags). Nothing else acts on
    # end_date, so without this they run forever.
    expiry_task = _start_expiry_monitor(pg_pool, redis_client, broadcaster)

    # Store in app state
    application.state.pg_pool = pg_pool
    application.state.redis = redis_client
    application.state.broadcaster = broadcaster
    application.state.expiry_task = expiry_task

    yield

    if expiry_task is not None:
        expiry_task.cancel()
        try:
            await expiry_task
        except asyncio.CancelledError:
            pass
        logger.info("Experiment expiry monitor stopped")

    # Shutdown
    await broadcaster.stop()
    logger.info("SSE broadcaster stopped")

    await redis_client.aclose()
    logger.info("Redis connection closed")

    await pg_pool.close()
    logger.info("PostgreSQL connection pool closed")


def _start_expiry_monitor(pg_pool, redis_client, broadcaster) -> asyncio.Task | None:
    if os.environ.get("EXPERIMENT_EXPIRY_ENABLED", "true").lower() != "true":
        logger.info("Experiment expiry monitor disabled")
        return None
    interval_seconds = int(os.environ.get("EXPERIMENT_EXPIRY_INTERVAL_SECONDS", "300"))
    logger.info("Starting experiment expiry monitor every %ds", interval_seconds)
    return asyncio.create_task(
        expiry.run_expiry_monitor(
            pg_pool,
            redis_client,
            broadcaster,
            interval_seconds=interval_seconds,
        )
    )


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
app.include_router(evaluate.router)
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
