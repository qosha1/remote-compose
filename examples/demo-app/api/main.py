"""URL shortener API — demo app for remote-compose.

Endpoints:
    POST /shorten      {"url": "https://..."} -> {"code": "...", "short_url": "..."}
    GET  /{code}       302 redirect + enqueue click event for worker
    GET  /stats/{code} {"code": "...", "url": "...", "clicks": N}
    GET  /health       200 if DB + Redis both reachable, 503 otherwise
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets as _secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

import psycopg
import redis
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, HttpUrl


DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://demo:demo@db:5432/demo")
REDIS_URL = os.environ.get("REDIS_URL", "redis://cache:6379/0")
APP_SECRET_KEY = os.environ.get("APP_SECRET_KEY", "dev-not-for-prod")
APP_VERSION = os.environ.get("APP_VERSION", "dev")
CLICK_QUEUE = "clicks"


def _connect_db() -> psycopg.Connection:
    return psycopg.connect(DATABASE_URL, autocommit=True)


def _connect_redis() -> redis.Redis:
    return redis.from_url(REDIS_URL, decode_responses=True)


def _init_schema() -> None:
    with _connect_db() as conn, conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS urls (
                code TEXT PRIMARY KEY,
                url  TEXT NOT NULL,
                clicks INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)


def _make_code(url: str) -> str:
    salt = _secrets.token_hex(4)
    digest = hashlib.sha256(f"{APP_SECRET_KEY}:{url}:{salt}".encode()).hexdigest()
    return digest[:8]


@asynccontextmanager
async def lifespan(app: FastAPI):
    for attempt in range(30):
        try:
            _init_schema()
            break
        except Exception as exc:
            if attempt == 29:
                raise
            import asyncio
            await asyncio.sleep(1)
    yield


app = FastAPI(title="remote-compose demo: URL shortener", lifespan=lifespan)


class ShortenRequest(BaseModel):
    url: HttpUrl


class ShortenResponse(BaseModel):
    code: str
    short_url: str


@app.post("/shorten", response_model=ShortenResponse)
def shorten(body: ShortenRequest, request: Request) -> ShortenResponse:
    code = _make_code(str(body.url))
    with _connect_db() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO urls (code, url) VALUES (%s, %s) "
            "ON CONFLICT (code) DO NOTHING",
            (code, str(body.url)),
        )
    base = str(request.base_url).rstrip("/")
    return ShortenResponse(code=code, short_url=f"{base}/{code}")


@app.get("/stats/{code}")
def stats(code: str) -> dict:
    with _connect_db() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT code, url, clicks, created_at FROM urls WHERE code = %s",
            (code,),
        )
        row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="code not found")
    return {
        "code": row[0],
        "url": row[1],
        "clicks": row[2],
        "created_at": row[3].isoformat(),
    }


@app.get("/health")
def health() -> dict:
    db_ok = False
    redis_ok = False
    db_err: Optional[str] = None
    redis_err: Optional[str] = None
    try:
        with _connect_db() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        db_ok = True
    except Exception as exc:
        db_err = str(exc)[:120]
    try:
        r = _connect_redis()
        r.ping()
        redis_ok = True
    except Exception as exc:
        redis_err = str(exc)[:120]
    status_code = 200 if (db_ok and redis_ok) else 503
    body = {
        "status": "ok" if (db_ok and redis_ok) else "degraded",
        "version": APP_VERSION,
        "db": {"ok": db_ok, "error": db_err},
        "redis": {"ok": redis_ok, "error": redis_err},
    }
    if status_code != 200:
        raise HTTPException(status_code=status_code, detail=body)
    return body


@app.get("/{code}")
def redirect(code: str) -> RedirectResponse:
    with _connect_db() as conn, conn.cursor() as cur:
        cur.execute("SELECT url FROM urls WHERE code = %s", (code,))
        row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="code not found")
    target = row[0]
    try:
        r = _connect_redis()
        r.rpush(CLICK_QUEUE, json.dumps({
            "code": code,
            "at": datetime.now(timezone.utc).isoformat(),
        }))
    except Exception:
        pass
    return RedirectResponse(url=target, status_code=302)
