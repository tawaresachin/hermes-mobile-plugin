"""
Mobile API endpoints for Hermes Agent.

Mounted at /api/plugins/hermes-mobile/ by the dashboard plugin system.
"""

from __future__ import annotations

import secrets
import time
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

# Hermes Agent imports (available at runtime)
try:
    from hermes_state import SessionDB
    from agent.usage_pricing import CanonicalUsage
    from hermes_cli.config import cfg_get
except ImportError:
    # For type checking / development
    SessionDB = Any
    CanonicalUsage = Any
    cfg_get = Any

router = APIRouter(prefix="/hermes-mobile", tags=["mobile"])

# ============================================================
# Models
# ============================================================

class HealthResponse(BaseModel):
    status: str = "ok"
    version: str = "1.0.0"
    timestamp: float = Field(default_factory=time.time)


class ChatRequest(BaseModel):
    query: str = ""
    session_id: str | None = None
    model: str | None = None
    multi_agent: bool = False
    attachment_url: str = ""
    attachment_type: str = ""
    reply_to: str | None = None


class ChatSyncRequest(ChatRequest):
    stream: bool = False


class FollowUpRequest(BaseModel):
    query: str
    session_id: str
    attachment_url: str = ""
    attachment_type: str = ""


class PairVerifyRequest(BaseModel):
    pairing_token: str
    device_name: str = "Mobile Device"
    device_id: str | None = None


class PairVerifyResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int = 2592000  # 30 days
    desktop_info: dict


class ModelSwitchRequest(BaseModel):
    model: str
    provider: str | None = None


class SessionCreateRequest(BaseModel):
    title: str | None = None
    model: str | None = None
    parent_session_id: str | None = None


class UsageResponse(BaseModel):
    daily: list[dict]
    by_model: list[dict]
    totals: dict
    period_days: int
    skills: dict


# ============================================================
# Auth Helpers
# ============================================================

# In-memory pairing tokens (in production, use Redis or DB)
_pairing_tokens: dict[str, dict] = {}

# Mobile JWT tokens (simplified - use proper JWT in production)
_mobile_tokens: dict[str, dict] = {}


def generate_pairing_token() -> str:
    """Generate a short-lived pairing token for QR code."""
    token = secrets.token_urlsafe(24)
    _pairing_tokens[token] = {
        "created_at": time.time(),
        "expires_at": time.time() + 300,  # 5 minutes
        "used": False,
    }
    return token


def verify_pairing_token(token: str) -> dict | None:
    """Verify and consume a pairing token."""
    data = _pairing_tokens.get(token)
    if not data:
        return None
    if data["used"]:
        return None
    if time.time() > data["expires_at"]:
        del _pairing_tokens[token]
        return None
    data["used"] = True
    return data


def create_mobile_token(device_name: str, device_id: str | None = None) -> str:
    """Create a long-lived mobile JWT token."""
    token = secrets.token_urlsafe(32)
    _mobile_tokens[token] = {
        "device_name": device_name,
        "device_id": device_id,
        "created_at": time.time(),
        "expires_at": time.time() + 2592000,  # 30 days
    }
    return token


def verify_mobile_token(token: str) -> dict | None:
    """Verify a mobile JWT token."""
    data = _mobile_tokens.get(token)
    if not data:
        return None
    if time.time() > data["expires_at"]:
        del _mobile_tokens[token]
        return None
    return data


async def get_mobile_user(request: Request) -> dict:
    """Dependency to verify mobile JWT token."""
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid token")
    token = auth[7:]
    user = verify_mobile_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return user


# ============================================================
# Endpoints
# ============================================================

@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Health check endpoint."""
    return HealthResponse()


@router.post("/pair/verify", response_model=PairVerifyResponse)
async def pair_verify(body: PairVerifyRequest) -> PairVerifyResponse:
    """
    Verify pairing token from QR code and return mobile JWT.
    
    Mobile app scans QR code from desktop dashboard, which contains
    a pairing token. This endpoint validates it and returns a long-lived JWT.
    """
    token_data = verify_pairing_token(body.pairing_token)
    if not token_data:
        raise HTTPException(status_code=400, detail="Invalid or expired pairing token")
    
    mobile_token = create_mobile_token(body.device_name, body.device_id)
    
    return PairVerifyResponse(
        access_token=mobile_token,
        desktop_info={
            "version": "1.0.0",
            "server_time": time.time(),
        }
    )


@router.post("/auth/refresh")
async def auth_refresh(user: dict = Depends(get_mobile_user)) -> dict:
    """Refresh mobile token (extend expiry)."""
    # Find and update token
    for token, data in _mobile_tokens.items():
        if data.get("device_id") == user.get("device_id"):
            data["expires_at"] = time.time() + 2592000
            return {"access_token": token, "expires_in": 2592000}
    raise HTTPException(status_code=401, detail="Token not found")


# ---- Sessions ----

@router.get("/sessions")
async def list_sessions(
    limit: int = 20,
    offset: int = 0,
    user: dict = Depends(get_mobile_user)
) -> list[dict]:
    """List sessions (mobile-optimized)."""
    db = SessionDB()
    try:
        sessions = db.list_sessions_rich(limit=limit, offset=offset)
        # Mobile-optimized format
        return [
            {
                "id": s["id"],
                "title": s.get("title") or "New Session",
                "model": s.get("model"),
                "message_count": s.get("message_count", 0),
                "updated_at": s.get("updated_at"),
                "input_tokens": s.get("input_tokens", 0),
                "output_tokens": s.get("output_tokens", 0),
            }
            for s in sessions
        ]
    finally:
        db.close()


@router.post("/sessions")
async def create_session(
    body: SessionCreateRequest,
    user: dict = Depends(get_mobile_user)
) -> dict:
    """Create a new session."""
    import uuid
    from hermes_state import SessionDB
    
    session_id = uuid.uuid4().hex[:8]
    db = SessionDB()
    try:
        db.upsert_session({
            "id": session_id,
            "title": body.title,
            "model": body.model,
            "parent_session_id": body.parent_session_id,
            "created_at": time.time(),
            "updated_at": time.time(),
            "message_count": 0,
            "input_tokens": 0,
            "output_tokens": 0,
        })
        return {"session_id": session_id, "created": True}
    finally:
        db.close()


@router.get("/sessions/{session_id}")
async def get_session(
    session_id: str,
    user: dict = Depends(get_mobile_user)
) -> dict:
    """Get session detail with messages."""
    db = SessionDB()
    try:
        sid = db.resolve_session_id(session_id)
        if not sid:
            raise HTTPException(status_code=404, detail="Session not found")
        session = db.get_session(sid)
        messages = db.get_messages(sid)
        return {"session": session, "messages": messages}
    finally:
        db.close()


@router.delete("/sessions/{session_id}")
async def delete_session(
    session_id: str,
    user: dict = Depends(get_mobile_user)
) -> dict:
    """Delete a session."""
    db = SessionDB()
    try:
        if not db.delete_session(session_id):
            raise HTTPException(status_code=404, detail="Session not found")
        return {"deleted": True}
    finally:
        db.close()


# ---- Chat Streaming ----

@router.post("/chat/stream")
async def chat_stream(
    body: ChatRequest,
    user: dict = Depends(get_mobile_user)
) -> StreamingResponse:
    """
    Streaming chat endpoint (SSE).
    
    Reuses the desktop agent's conversation loop to process the message
    and streams events back to the mobile client.
    """
    from hermes_cli.web_server import _build_openai_messages
    import json
    import asyncio
    
    session_id = body.session_id or secrets.token_hex(4)
    
    async def event_generator():
        """Generate SSE events from agent conversation loop."""
        # Build message history
        openai_messages = await asyncio.to_thread(_build_openai_messages, session_id)
        
        # Add user message
        if body.query:
            openai_messages.append({"role": "user", "content": body.query})
        
        # TODO: Connect to actual agent conversation loop
        # For now, yield mock events
        yield f"data: {json.dumps({'type': 'text', 'content': 'Processing...'})}\n\n"
        await asyncio.sleep(0.1)
        yield f"data: {json.dumps({'type': 'turn_end'})}\n\n"
    
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"}
    )


@router.post("/chat")
async def chat_sync(
    body: ChatSyncRequest,
    user: dict = Depends(get_mobile_user)
) -> dict:
    """Non-streaming chat (wait for complete response)."""
    # TODO: Implement using agent conversation loop
    return {"response": "Not implemented yet", "session_id": body.session_id}


@router.post("/chat/followup")
async def chat_followup(
    body: FollowUpRequest,
    user: dict = Depends(get_mobile_user)
) -> StreamingResponse:
    """Follow-up message in existing session (streaming)."""
    # Similar to chat_stream but continues existing session
    return await chat_stream(ChatRequest(
        query=body.query,
        session_id=body.session_id,
        attachment_url=body.attachment_url,
        attachment_type=body.attachment_type,
    ), user)


# ---- Models ----

@router.get("/models")
async def list_models(user: dict = Depends(get_mobile_user)) -> list[dict]:
    """List available models."""
    # TODO: Fetch from desktop agent's model registry
    return [
        {"id": "auto", "name": "Auto (Best Coding)", "provider": "omnirouter"},
        {"id": "gpt-4o", "name": "GPT-4o", "provider": "openai"},
        {"id": "claude-3.5-sonnet", "name": "Claude 3.5 Sonnet", "provider": "anthropic"},
    ]


@router.post("/models/switch")
async def switch_model(
    body: ModelSwitchRequest,
    user: dict = Depends(get_mobile_user)
) -> dict:
    """Switch active model."""
    # TODO: Call desktop agent's model switch logic
    return {"model": body.model, "switched": True}


# ---- Usage ----

@router.get("/usage", response_model=UsageResponse)
async def get_usage(
    days: int = 30,
    user: dict = Depends(get_mobile_user)
) -> UsageResponse:
    """Get usage analytics (real data from desktop DB)."""
    from hermes_state import SessionDB
    from agent.insights import InsightsEngine
    
    db = SessionDB()
    try:
        cutoff = time.time() - (days * 86400)
        
        # Daily aggregates
        cur = db._conn.execute("""
            SELECT date(started_at, 'unixepoch') as day,
                   SUM(input_tokens) as input_tokens,
                   SUM(output_tokens) as output_tokens,
                   SUM(cache_read_tokens) as cache_read_tokens,
                   SUM(reasoning_tokens) as reasoning_tokens,
                   COALESCE(SUM(estimated_cost_usd), 0) as estimated_cost,
                   COALESCE(SUM(actual_cost_usd), 0) as actual_cost,
                   COUNT(*) as sessions,
                   SUM(COALESCE(api_call_count, 0)) as api_calls
            FROM sessions WHERE started_at > ?
            GROUP BY day ORDER BY day
        """, (cutoff,))
        daily = [dict(r) for r in cur.fetchall()]
        
        # By model
        cur2 = db._conn.execute("""
            SELECT model,
                   SUM(input_tokens) as input_tokens,
                   SUM(output_tokens) as output_tokens,
                   COALESCE(SUM(estimated_cost_usd), 0) as estimated_cost,
                   COUNT(*) as sessions,
                   SUM(COALESCE(api_call_count, 0)) as api_calls
            FROM sessions WHERE started_at > ? AND model IS NOT NULL
            GROUP BY model ORDER BY SUM(input_tokens) + SUM(output_tokens) DESC
        """, (cutoff,))
        by_model = [dict(r) for r in cur2.fetchall()]
        
        # Totals
        cur3 = db._conn.execute("""
            SELECT SUM(input_tokens) as total_input,
                   SUM(output_tokens) as total_output,
                   SUM(cache_read_tokens) as total_cache_read,
                   SUM(reasoning_tokens) as total_reasoning,
                   COALESCE(SUM(estimated_cost_usd), 0) as total_estimated_cost,
                   COALESCE(SUM(actual_cost_usd), 0) as total_actual_cost,
                   COUNT(*) as total_sessions,
                   SUM(COALESCE(api_call_count, 0)) as total_api_calls
            FROM sessions WHERE started_at > ?
        """, (cutoff,))
        totals = dict(cur3.fetchone())
        
        insights = InsightsEngine(db).generate(days=days)
        skills = insights.get("skills", {})
        
        return UsageResponse(
            daily=daily,
            by_model=by_model,
            totals=totals,
            period_days=days,
            skills=skills,
        )
    finally:
        db.close()


# ---- QR Pairing (Desktop Dashboard calls this) ----

@router.post("/pair/generate")
async def generate_pairing_qr() -> dict:
    """
    Generate a pairing token and QR code data.
    Called by desktop dashboard when user clicks 'Pair Mobile'.
    """
    token = generate_pairing_token()
    # QR contains: hermes://pair?token=XXX&host=100.x.x.x:9119
    # The mobile app parses this
    pairing_url = f"hermes://pair?token={token}&host=auto"
    
    return {
        "pairing_token": token,
        "pairing_url": pairing_url,
        "expires_in": 300,
        "qr_data": pairing_url,
    }
