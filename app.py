import uuid
import shutil
import subprocess
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse, RedirectResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask


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

app = FastAPI()


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

def cleanup_files(*paths: Path) -> None:
    for p in paths:
        try:
            if p and p.exists():
                p.unlink()
        except Exception:
            pass


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
    """
    Usa ffprobe para obtener duración real del audio.
    """
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
        raise HTTPException(
            status_code=400,
            detail="No se pudo analizar la duración del archivo."
        )

    try:
        return float(proc.stdout.strip())
    except Exception:
        raise HTTPException(
            status_code=400,
            detail="Duración inválida."
        )


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


# =========================
# ENDPOINTS
# =========================

@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/master")
def master_get_hint():
    return JSONResponse(
        {"detail": "Method Not Allowed. Usa POST /api/master con form-data: file, preset, intensity."},
        status_code=405
    )


@app.post("/api/master")
async def master(
    file: UploadFile = File(...),
    preset: str = Form("clean"),
    intensity: int = Form(55),
):
    if not file or not file.filename:
        raise HTTPException(status_code=400, detail="Archivo inválido.")

    job_id = uuid.uuid4().hex[:8]
    safe_name = safe_filename(file.filename)

    in_path = TMP_DIR / f"in_{job_id}_{safe_name}"
    out_path = TMP_DIR / f"master_{job_id}.wav"

    # Guardar archivo
    try:
        with in_path.open("wb") as f:
            shutil.copyfileobj(file.file, f)
    finally:
        try:
            file.file.close()
        except Exception:
            pass

    # Validar tamaño
    size_bytes = in_path.stat().st_size
    if size_bytes > MAX_FILE_SIZE_BYTES:
        cleanup_files(in_path)
        raise HTTPException(
            status_code=400,
            detail=f"El archivo supera el límite de {MAX_FILE_SIZE_MB}MB."
        )

    # Validar duración
    duration = get_audio_duration_seconds(in_path)
    if duration > MAX_DURATION_SECONDS:
        cleanup_files(in_path)
        raise HTTPException(
            status_code=400,
            detail="El audio supera el límite de 6 minutos."
        )

    # Procesar master
    filters = preset_chain(preset, intensity)

    cmd = [
        "ffmpeg", "-y",
        "-hide_banner",
        "-i", str(in_path),
        "-vn",
        "-af", filters,
        "-ar", "44100",
        "-ac", "2",
        "-sample_fmt", "s16",
        str(out_path)
    ]

    run_ffmpeg(cmd)

    if not out_path.exists() or out_path.stat().st_size < 1024:
        cleanup_files(in_path, out_path)
        raise HTTPException(
            status_code=500,
            detail="Master no generado o vacío."
        )

    return FileResponse(
        path=str(out_path),
        media_type="audio/wav",
        filename="warmaster_master.wav",
        background=BackgroundTask(cleanup_files, in_path, out_path),
    )


@app.get("/")
def root():
    return RedirectResponse(url="/index.html")

# =========================
# STATIC
# =========================

app.mount("/", StaticFiles(directory=str(PUBLIC_DIR), html=True), name="public")
