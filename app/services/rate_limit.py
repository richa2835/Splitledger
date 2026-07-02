"""
Token bucket rate limiting, backed by Redis so it works correctly even with
multiple API instances behind a load balancer (in-memory counters wouldn't).

Each identity (user id or API key) gets a bucket of `capacity` tokens that
refill at `refill_rate` tokens/second. Every request costs 1 token.
"""

import os
import time

import redis

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
redis_client = redis.from_url(REDIS_URL, decode_responses=True)

CAPACITY = int(os.getenv("RATE_LIMIT_CAPACITY", "100"))       # max burst size
REFILL_RATE = int(os.getenv("RATE_LIMIT_REFILL_RATE", "30"))  # tokens added per second


def check_rate_limit(identity: str) -> tuple[bool, int]:
    """
    Returns (allowed: bool, remaining: int).
    Lua script keeps the read-modify-write atomic inside Redis, which
    matters here for the same reason row locking matters in Postgres --
    two concurrent requests must not both read the same token count and
    both think they're allowed through.
    """
    lua_script = """
    local key = KEYS[1]
    local capacity = tonumber(ARGV[1])
    local refill_rate = tonumber(ARGV[2])
    local now = tonumber(ARGV[3])

    local bucket = redis.call('HMGET', key, 'tokens', 'last_refill')
    local tokens = tonumber(bucket[1])
    local last_refill = tonumber(bucket[2])

    if tokens == nil then
        tokens = capacity
        last_refill = now
    end

    local elapsed = math.max(0, now - last_refill)
    tokens = math.min(capacity, tokens + elapsed * refill_rate)

    local allowed = 0
    if tokens >= 1 then
        allowed = 1
        tokens = tokens - 1
    end

    redis.call('HMSET', key, 'tokens', tokens, 'last_refill', now)
    redis.call('EXPIRE', key, 60)

    return {allowed, math.floor(tokens)}
    """
    key = f"ratelimit:{identity}"
    now = time.time()
    allowed, remaining = redis_client.eval(lua_script, 1, key, CAPACITY, REFILL_RATE, now)
    return bool(allowed), int(remaining)
