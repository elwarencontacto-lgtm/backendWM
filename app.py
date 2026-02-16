import uuid
import shutil
import subprocess
from pathlib import Path
from datetime import datetime
from typing import Dict, Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles


# =========================
# CONFIG
# =========================

MAX_FILE_SIZE_MB = 100
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024
MAX_DURATION_SECONDS = 6 * 60  # 6 minutos

BASE_DIR = Path(__file__).parent
TMP_DIR = BASE_DIR / "tmp"
TMP_DIR.mkdir(exist_ok=True)

PUBLIC_DIR = BASE_DIR / "public"
PUBLIC_DIR.mkdir(exist_ok=True)

# Persistencia (para dashboard + re-descarga)
DATA_DIR = BASE_DIR / "data"
MASTERS_DIR = DATA_DIR / "masters"
DATA_DIR.mkdir(exist_ok=True)
MASTERS_DIR.mkdir(exist_ok=True)

app = FastAPI()

# =========================
# JOB STORAGE (MEMORIA)
# =========================
# job_id == master_id (para el front)
jobs: Dict[str, dict] = {}

# Compras (memoria). Más adelante lo pasas a DB.
# purchases[master_id] = {"paid": True, "quality": "PLUS"/"PRO", "paid_at": "..."}
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

    return float(proc.stdout.strip() or 0.0)


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


def master_path_for(master_id: str) -> Path:
    return MASTERS_DIR / f"master_{master_id}.wav"


def orig_path_for(master_id: str, safe_name: str) -> Path:
    return MASTERS_DIR / f"orig_{master_id}_{safe_name}"


def preview_path_for(master_id: str) -> Path:
    return MASTERS_DIR / f"preview_{master_id}_20s.mp3"


def is_paid(master_id: str) -> bool:
    p = purchases.get(master_id)
    return bool(p and p.get("paid") is True)


def paid_quality(master_id: str) -> Optional[str]:
    p = purchases.get(master_id)
    if not p:
        return None
    q = (p.get("quality") or "").upper()
    return q if q in ("PLUS", "PRO") else None


# =========================
# ENDPOINTS
# =========================

@app.get("/api/health")
def health():
    return {"ok": True}


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
    # Estos campos extra NO rompen tu front actual, y ayudan con el nuevo
    target: str = Form(None),              # alias si tu nuevo front envía "target"
    requested_quality: str = Form("FREE"), # FREE/PLUS/PRO (por ahora lo ignoramos si no hay pagos)
):
    if not file or not file.filename:
        raise HTTPException(status_code=400, detail="Archivo inválido.")

    # Compatibilidad: si viene "target" lo usamos como preset
    if target and not preset:
        preset = target
    elif target and preset == "clean":
        # si tu UI nueva manda target, úsalo
        preset = target

    master_id = uuid.uuid4().hex[:10]
    safe_name = safe_filename(file.filename)

    # Guardamos original temporal primero
    tmp_in_path = TMP_DIR / f"in_{master_id}_{safe_name}"

    jobs[master_id] = {
        "status": "processing",
        "filename": safe_name,
        "preset": preset,
        "intensity": int(intensity),
        "created_at": datetime.utcnow().isoformat(),
        "master_id": master_id
    }

    try:
        with tmp_in_path.open("wb") as f:
            shutil.copyfileobj(file.file, f)
    finally:
        try:
            file.file.close()
        except Exception:
            pass

    # VALIDAR TAMAÑO
    if tmp_in_path.stat().st_size > MAX_FILE_SIZE_BYTES:
        tmp_in_path.unlink(missing_ok=True)
        jobs[master_id]["status"] = "error"
        raise HTTPException(status_code=400, detail="Supera 100MB.")

    # VALIDAR DURACIÓN
    duration = get_audio_duration_seconds(tmp_in_path)
    if duration > MAX_DURATION_SECONDS:
        tmp_in_path.unlink(missing_ok=True)
        jobs[master_id]["status"] = "error"
        raise HTTPException(status_code=400, detail="Supera 6 minutos.")

    # Persistimos original para poder re-render en unlock/upgrade (si quieres)
    persisted_orig = orig_path_for(master_id, safe_name)
    try:
        shutil.move(str(tmp_in_path), str(persisted_orig))
    except Exception:
        # fallback: si move falla, copiamos
        shutil.copy2(str(tmp_in_path), str(persisted_orig))
        tmp_in_path.unlink(missing_ok=True)

    out_path = master_path_for(master_id)

    filters = preset_chain(preset, intensity)

    cmd = [
        "ffmpeg", "-y",
        "-hide_banner",
        "-i", str(persisted_orig),
        "-vn",
        "-af", filters,
        "-ar", "44100",
        "-ac", "2",
        "-sample_fmt", "s16",
        str(out_path)
    ]

    try:
        run_ffmpeg(cmd)
    except Exception:
        jobs[master_id]["status"] = "error"
        # No borramos persisted_orig automáticamente para poder debug, pero si quieres lo borramos:
        # persisted_orig.unlink(missing_ok=True)
        out_path.unlink(missing_ok=True)
        raise

    if not out_path.exists() or out_path.stat().st_size < 1024:
        jobs[master_id]["status"] = "error"
        out_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail="Master vacío.")

    jobs[master_id]["status"] = "done"
    jobs[master_id]["duration_sec"] = duration
    jobs[master_id]["stream"] = f"/api/masters/{master_id}/stream"
    jobs[master_id]["preview_download"] = f"/api/master/preview?master_id={master_id}"
    jobs[master_id]["full_download"] = f"/api/masters/{master_id}/download"  # requiere paid

    # Respuesta: devolvemos el WAV para escuchar en la UI + header master_id
    resp = FileResponse(
        path=str(out_path),
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
    out_path = master_path_for(master_id)
    if not out_path.exists():
        raise HTTPException(status_code=404, detail="Master no encontrado.")

    prev = preview_path_for(master_id)
    # Generar si no existe
    if not prev.exists() or prev.stat().st_mtime < out_path.stat().st_mtime:
        cmd = [
            "ffmpeg", "-y",
            "-hide_banner",
            "-i", str(out_path),
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
# (por ahora sin pasarela, solo marca paid para test)
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

    out_path = master_path_for(master_id)
    if not out_path.exists():
        raise HTTPException(status_code=404, detail="Master no encontrado.")

    purchases[master_id] = {
        "paid": True,
        "quality": quality,
        "paid_at": datetime.utcnow().isoformat()
    }

    return {"ok": True, "master_id": master_id, "quality": quality}


# -------------------------
# Upgrade PLUS -> PRO (paga diferencia más adelante)
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
# (luego lo hacemos por usuario con login real)
# -------------------------
@app.get("/api/masters")
def list_masters():
    # Por ahora: devolvemos todos los masters pagados en memoria
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
            "quality": paid_quality(mid) or "PLUS",
            "paid": True,
            "created_at": meta.get("created_at"),
        })

    # Orden por fecha (desc)
    items.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    return items


# -------------------------
# Stream (escuchar completo siempre, incluso FREE)
# -------------------------
@app.get("/api/masters/{master_id}/stream")
def stream_master(master_id: str):
    out_path = master_path_for(master_id)
    if not out_path.exists():
        raise HTTPException(status_code=404, detail="Master no encontrado.")
    return FileResponse(
        path=str(out_path),
        media_type="audio/wav",
        filename="warmaster_master.wav"
    )


# -------------------------
# Download FULL (solo si paid)
# -------------------------
@app.get("/api/masters/{master_id}/download")
def download_master(master_id: str):
    out_path = master_path_for(master_id)
    if not out_path.exists():
        raise HTTPException(status_code=404, detail="Master no encontrado.")

    if not is_paid(master_id):
        # Modelo: FREE no descarga completo
        raise HTTPException(status_code=402, detail="Debes desbloquear (PLUS/PRO) para descargar completo.")

    q = paid_quality(master_id) or "PLUS"
    return FileResponse(
        path=str(out_path),
        media_type="audio/wav",
        filename=f"warmaster_master_{q.lower()}.wav"
    )


# -------------------------
# Compatibilidad con tu ruta antigua /download/{job_id}
# (ahora exige paid para descargar completo)
# -------------------------
@app.get("/download/{job_id}")
def download_job(job_id: str):
    # Deja tu endpoint pero con reglas nuevas (FULL solo si paid)
    return download_master(job_id)


# -------------------------
# Root
# -------------------------
@app.get("/")
def root():
    return RedirectResponse(url="/index.html")


app.mount("/", StaticFiles(directory=str(PUBLIC_DIR), html=True), name="public")
