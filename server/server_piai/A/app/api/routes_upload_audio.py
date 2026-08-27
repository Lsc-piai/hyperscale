# app/api/routes_upload_audio.py  (서버 A)
from pathlib import Path
from fastapi import APIRouter, UploadFile, File, HTTPException
from ..core.worker import enqueue

router = APIRouter()
ALLOWED_EXTS = {".mkv", ".mka", ".webm", ".mp4", ".wav", ".mp3", ".m4a", ".aac", ".flac"}


@router.post("/upload_audio")
async def upload(file: UploadFile = File(...)):
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTS:
        raise HTTPException(status_code=415, detail=f"Unsupported extension: {ext}")

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")

    job_id = enqueue(file.filename, data)  # 디코드하지 않고 raw bytes만 큐에 적재
    return {"ok": True, "job_id": job_id, "status": "queued"}
