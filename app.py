import uuid
import shutil
import subprocess
from pathlib import Path
from datetime import datetime
from typing import Dict, Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles


# =========================
# CONFIG
# =========================

# FREE limits (tu versión actual)
FREE_MAX_FILE_SIZE_MB = 100
FREE_MAX_FILE_SIZE_BYTES = FREE_MAX_FILE_SIZE_MB * 1024 * 1024
FREE_MAX_DURATION_SECONDS = 6 * 60  # 6 min

# (Opcional) límites futuros por plan (puedes ajustar)
PLUS_MAX_FILE_SIZE_MB = 250
PLUS_MAX_FILE_SIZE_BYTES = PLUS_MAX_FILE_SIZE_MB * 1024 * 1024
PLUS_MAX_DURATION_SECONDS = 15 * 60

PRO_MAX_FILE_SIZE_MB = 500
PRO_MAX_FILE_SIZE_BYTES = PRO_MAX_FILE_SIZE_MB * 1024 * 1024
PRO_MAX_DURATION_SECONDS = 30 * 60

BASE_DIR = Path(__file__).parent

TMP_DIR = BASE_DIR / "tmp"
TMP_DIR.mkdir(exist_ok=True)

PUBLIC_DIR = BASE_DIR / "public"
PUBLIC_DIR.mkdir(exist_ok=True)

# Persistencia
DATA_DIR = BASE_DIR / "data"
MASTERS_DIR = DATA_DIR / "masters"
DATA_DIR.mkdir(exist_ok=True)
MASTERS_DIR.mkdir(exist_ok=True)

app = FastAPI()

# =========================
# STORAGE (MEMORIA)
# =========================
# jobs[master_id] -> info para debug/UI
jobs: Dict[str, dict] = {}

# purchases[master_id] -> {paid: True, quality: "PLUS"/"PRO", paid_at: "..."}
purchases: Dict[str, dict] = {}

# =========================
# CORS
# =========================
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =========================
# UTILS
# =========================

def run_ffmpeg(cmd: list[str]) -> None:
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    if proc.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=f"FFmpeg error:\n{proc.stderr[-8000:]}"
        )


def safe_filename(name: str) -> str:
    clean = "".join(c for c in (name or "") if c.isalnum() or c in "._-").strip("._-")
    return clean or "audio.wav"


def get_audio_duration_seconds(path: Path) -> float:
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path)
    ]
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    if proc.returncode != 0:
        raise HTTPException(status_code=400, detail="No se pudo analizar duración.")

    try:
        return float(proc.stdout.strip() or 0.0)
    except Exception:
        return 0.0


def preset_chain(preset: str, intensity: int) -> str:
    intensity = max(0, min(100, int(intensity)))

    thr = -18.0 - (intensity * 0.10)
    ratio = 2.0 + (intensity * 0.04)
    makeup = 2.0 + (intensity * 0.06)

    preset = (preset or "clean").lower()

    if preset == "club":
        eq = "bass=g=4:f=90,treble=g=2:f=9000"
    elif preset == "warm":
        eq = "bass=g=3:f=160,treble=g=-2:f=4500"
    elif preset == "bright":
        eq = "bass=g=-1:f=120,treble=g=4:f=8500"
    elif preset == "heavy":
        eq = "bass=g=5:f=90,treble=g=2:f=3500"
    else:
        eq = "bass=g=2:f=120,treble=g=1:f=8000"

    return (
        f"{eq},"
        f"acompressor=threshold={thr}dB:ratio={ratio}:attack=12:release=120:makeup={makeup},"
        f"alimiter=limit=-1.0dB"
    )


def master_path(master_id: str) -> Path:
    return MASTERS_DIR / f"master_{master_id}.wav"


def orig_path(master_id: str, safe_name: str) -> Path:
    return MASTERS_DIR / f"orig_{master_id}_{safe_name}"


def preview_path(master_id: str) -> Path:
    return MASTERS_DIR / f"preview_{master_id}_20s.mp3"


def is_paid(master_id: str) -> bool:
    p = purchases.get(master_id)
    return bool(p and p.get("paid") is True)


def get_paid_quality(master_id: str) -> Optional[str]:
    p = purchases.get(master_id)
    if not p:
        return None
    q = (p.get("quality") or "").upper()
    return q if q in ("PLUS", "PRO") else None


def resolve_plan(req: Request) -> str:
    """
    Plan actual: por ahora FREE por defecto.
    (Más adelante lo conectamos a login/suscripción real)
    Para test, puedes enviar header: X-WM-Plan: PLUS/PRO
    """
    hdr = (req.headers.get("X-WM-Plan") or "").upper().strip()
    if hdr in ("FREE", "PLUS", "PRO"):
        return hdr
    return "FREE"


def enforce_limits(plan_or_quality: str, file_size_bytes: int, duration_seconds: float) -> None:
    q = (plan_or_quality or "FREE").upper()

    if q == "PRO":
        max_bytes = PRO_MAX_FILE_SIZE_BYTES
        max_sec = PRO_MAX_DURATION_SECONDS
    elif q == "PLUS":
        max_bytes = PLUS_MAX_FILE_SIZE_BYTES
        max_sec = PLUS_MAX_DURATION_SECONDS
    else:
        max_bytes = FREE_MAX_FILE_SIZE_BYTES
        max_sec = FREE_MAX_DURATION_SECONDS

    if file_size_bytes > max_bytes:
        raise HTTPException(status_code=400, detail=f"Supera el máximo de {max_bytes // (1024*1024)}MB.")

    if duration_seconds > max_sec:
        raise HTTPException(status_code=400, detail=f"Supera el máximo de {max_sec // 60} minutos.")


# =========================
# ENDPOINTS
# =========================

@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/me")
def me(request: Request):
    # Para el master.html dinámico
    plan = resolve_plan(request)
    return {"plan": plan}


@app.get("/api/jobs")
def list_jobs():
    return {"jobs": jobs}


@app.get("/api/job/{job_id}")
def get_job(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job no encontrado.")
    return job


# -------------------------
# MASTER (genera WAV completo)
# -------------------------
@app.post("/api/master")
async def master(
    request: Request,
    file: UploadFile = File(...),
    preset: str = Form("clean"),
    intensity: int = Form(55),
    # compat con tu master.html nuevo
    target: Optional[str] = Form(None),
    requested_quality: str = Form("FREE"),
):
    if not file or not file.filename:
        raise HTTPException(status_code=400, detail="Archivo inválido.")

    # Compat: si viene "target", lo usamos como preset
    if target:
        preset = target

    master_id = uuid.uuid4().hex[:10]
    safe_name = safe_filename(file.filename)

    tmp_in = TMP_DIR / f"in_{master_id}_{safe_name}"
    out = master_path(master_id)
    orig = orig_path(master_id, safe_name)

    jobs[master_id] = {
        "status": "processing",
        "filename": safe_name,
        "preset": preset,
        "intensity": int(intensity),
        "created_at": datetime.utcnow().isoformat(),
        "master_id": master_id,
    }

    # Guardar upload temporal
    try:
        with tmp_in.open("wb") as f:
            shutil.copyfileobj(file.file, f)
    finally:
        try:
            file.file.close()
        except Exception:
            pass

    # Validaciones (por ahora: según plan o requested_quality)
    plan = resolve_plan(request)
    quality = (requested_quality or plan or "FREE").upper()
    if quality not in ("FREE", "PLUS", "PRO"):
        quality = plan

    file_bytes = tmp_in.stat().st_size
    duration = get_audio_duration_seconds(tmp_in)

    # Mantén tus límites FREE, pero dejamos preparado PLUS/PRO
    enforce_limits(quality, file_bytes, duration)

    # Persistimos original (para poder re-render/descargas)
    try:
        shutil.move(str(tmp_in), str(orig))
    except Exception:
        shutil.copy2(str(tmp_in), str(orig))
        tmp_in.unlink(missing_ok=True)

    filters = preset_chain(preset, intensity)
    cmd = [
        "ffmpeg", "-y",
        "-hide_banner",
        "-i", str(orig),
        "-vn",
        "-af", filters,
        "-ar", "44100",
        "-ac", "2",
        "-sample_fmt", "s16",
        str(out)
    ]

    try:
        run_ffmpeg(cmd)
    except Exception:
        jobs[master_id]["status"] = "error"
        out.unlink(missing_ok=True)
        raise

    if not out.exists() or out.stat().st_size < 1024:
        jobs[master_id]["status"] = "error"
        out.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail="Master vacío.")

    jobs[master_id]["status"] = "done"
    jobs[master_id]["duration_sec"] = duration
    jobs[master_id]["stream"] = f"/api/masters/{master_id}/stream"
    jobs[master_id]["preview_download"] = f"/api/master/preview?master_id={master_id}"
    jobs[master_id]["full_download"] = f"/api/masters/{master_id}/download"

    # Respuesta: WAV para escuchar + header master_id
    resp = FileResponse(
        path=str(out),
        media_type="audio/wav",
        filename="warmaster_master.wav",
    )
    resp.headers["X-Master-Id"] = master_id
    return resp


# -------------------------
# PREVIEW 20s (FREE download)
# -------------------------
@app.get("/api/master/preview")
def preview_20s(master_id: str):
    out = master_path(master_id)
    if not out.exists():
        raise HTTPException(status_code=404, detail="Master no encontrado.")

    prev = preview_path(master_id)
    if (not prev.exists()) or (prev.stat().st_mtime < out.stat().st_mtime):
        cmd = [
            "ffmpeg", "-y",
            "-hide_banner",
            "-i", str(out),
            "-t", "20",
            "-vn",
            "-codec:a", "libmp3lame",
            "-q:a", "4",
            str(prev)
        ]
        run_ffmpeg(cmd)

    return FileResponse(
        path=str(prev),
        media_type="audio/mpeg",
        filename="warmaster_preview_20s.mp3"
    )


# -------------------------
# UNLOCK por canción (PLUS/PRO)
# (sin pasarela aún: marca pagado para test)
# -------------------------
@app.post("/api/unlock")
async def unlock(req: Request):
    body = await req.json()
    master_id = (body.get("master_id") or "").strip()
    quality = (body.get("quality") or "").strip().upper()

    if not master_id:
        raise HTTPException(status_code=400, detail="master_id requerido.")
    if quality not in ("PLUS", "PRO"):
        raise HTTPException(status_code=400, detail="quality debe ser PLUS o PRO.")

    out = master_path(master_id)
    if not out.exists():
        raise HTTPException(status_code=404, detail="Master no encontrado.")

    purchases[master_id] = {
        "paid": True,
        "quality": quality,
        "paid_at": datetime.utcnow().isoformat()
    }

    return {"ok": True, "master_id": master_id, "quality": quality}


# -------------------------
# Upgrade PLUS -> PRO (marca estado)
# -------------------------
@app.post("/api/upgrade")
async def upgrade(req: Request):
    body = await req.json()
    master_id = (body.get("master_id") or "").strip()
    to_q = (body.get("to") or "PRO").strip().upper()

    if not master_id:
        raise HTTPException(status_code=400, detail="master_id requerido.")
    if to_q != "PRO":
        raise HTTPException(status_code=400, detail="Solo upgrade a PRO permitido.")

    if not is_paid(master_id):
        raise HTTPException(status_code=402, detail="Primero debes desbloquear (PLUS) o pagar.")

    purchases[master_id]["quality"] = "PRO"
    purchases[master_id]["upgraded_at"] = datetime.utcnow().isoformat()

    return {"ok": True, "master_id": master_id, "quality": "PRO"}


# -------------------------
# Dashboard list (masters pagados)
# -------------------------
@app.get("/api/masters")
def list_masters():
    items = []
    for mid, meta in jobs.items():
        if meta.get("status") != "done":
            continue
        if not is_paid(mid):
            continue
        items.append({
            "id": mid,
            "title": meta.get("filename") or "Master",
            "preset": meta.get("preset") or "clean",
            "intensity": meta.get("intensity") or 55,
            "quality": get_paid_quality(mid) or "PLUS",
            "paid": True,
            "created_at": meta.get("created_at"),
        })
    items.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    return items


# -------------------------
# Stream (escuchar completo siempre)
# -------------------------
@app.get("/api/masters/{master_id}/stream")
def stream_master(master_id: str):
    out = master_path(master_id)
    if not out.exists():
        raise HTTPException(status_code=404, detail="Master no encontrado.")
    return FileResponse(
        path=str(out),
        media_type="audio/wav",
        filename="warmaster_master.wav"
    )


# -------------------------
# Download FULL (solo si pagado)
# -------------------------
@app.get("/api/masters/{master_id}/download")
def download_master(master_id: str):
    out = master_path(master_id)
    if not out.exists():
        raise HTTPException(status_code=404, detail="Master no encontrado.")

    if not is_paid(master_id):
        # FREE: no descarga completo
        raise HTTPException(status_code=402, detail="Debes desbloquear (PLUS/PRO) para descargar completo.")

    q = get_paid_quality(master_id) or "PLUS"
    return FileResponse(
        path=str(out),
        media_type="audio/wav",
        filename=f"warmaster_master_{q.lower()}.wav"
    )


# -------------------------
# Compatibilidad con tu ruta antigua
# -------------------------
@app.get("/download/{job_id}")
def download_job(job_id: str):
    return download_master(job_id)


@app.get("/")
def root():
    return RedirectResponse(url="/index.html")


app.mount("/", StaticFiles(directory=str(PUBLIC_DIR), html=True), name="public")
