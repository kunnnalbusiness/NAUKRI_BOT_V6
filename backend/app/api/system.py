"""
Admin System Monitor API Router.
Exposes container-aware resource stats, an SSE live stream, and manual
incident notes. Strictly restricted to the admin role.
"""

import asyncio
import json
from typing import Dict

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from backend.app.core.dependencies import require_admin, require_csrf
from backend.app.services import system_stats, uptime_service

router = APIRouter(prefix="/admin/system", tags=["System Monitor"])


class IncidentNoteRequest(BaseModel):
    component: str = Field("api", max_length=40)
    note: str = Field(..., min_length=1, max_length=500)


@router.get("/stats")
async def get_system_stats(_claims: Dict[str, str] = Depends(require_admin)):
    """Admin only: one-shot system stats snapshot (CPU, memory, disk, workers, DB)."""
    return await asyncio.to_thread(system_stats.collect_stats)


@router.get("/stats/stream")
async def stream_system_stats(
    request: Request, _claims: Dict[str, str] = Depends(require_admin)
):
    """
    Admin only: Server-Sent Events stream pushing stats every 2 seconds.
    Stops when the client disconnects.
    """

    async def event_generator():
        while True:
            if await request.is_disconnected():
                break
            try:
                stats = await asyncio.to_thread(system_stats.collect_stats)
                payload = json.dumps(stats, default=str)
                yield f"data: {payload}\n\n"
            except Exception:
                yield f"event: error\ndata: {json.dumps({'detail': 'stats collection failed'})}\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/history")
async def get_stats_history(_claims: Dict[str, str] = Depends(require_admin)):
    """Admin only: 15-minute rolling sparkline history (5 s resolution)."""
    return {"history": system_stats.get_history()}


@router.post("/incidents")
async def add_incident_note(
    body: IncidentNoteRequest,
    claims: Dict[str, str] = Depends(require_admin),
    _csrf: bool = Depends(require_csrf),
):
    """Admin only: posts a manual incident note shown on the public status page."""
    incident = uptime_service.add_manual_note(
        component=body.component,
        note=body.note,
        author=claims.get("username", "admin"),
    )
    incident.pop("_id", None)
    return {"success": True, "incident": incident}
