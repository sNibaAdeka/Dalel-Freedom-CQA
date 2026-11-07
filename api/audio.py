"""/api/audio/{conv_id} — fresh signed mp3 link + playback offsets per message (cqa.audio)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from cqa import audio

router = APIRouter()


@router.get("/audio/{conv_id}")
def get_audio(request: Request, conv_id: str, refresh: bool = False):
    state = request.app.state.cqa
    conv = state.repo.conversation(conv_id)
    if conv is None:
        raise HTTPException(404, "Диалог не найден.")
    if refresh and conv.get("audio_url"):
        state.resolver.resolve(conv_id, conv["audio_url"], force=True)
    return audio.payload(conv, state.resolver, state.calibration, calibrate=state.calibrate_audio)
