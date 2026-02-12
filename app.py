import uuid
import subprocess
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

# ==============================
# CONFIG
# ==============================

MAX_FILE_SIZE = 100 * 1024 * 1024  # 100MB

BASE_DIR = Path(__file__).parent
TMP_DIR = BASE_DIR / "tmp"
TMP_DIR.mkdir(exist_ok=True)

PUBLIC_DIR = BASE_DIR / "public"
PUBLIC_DIR.mkdir(exist_ok=True)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==============================
# UTILS
# ==============================

def cleanup_files(*paths: Path) -> None:
    for p in paths:
        try:
            if p and p.exists():
                p.unlink()
        except Exception:
            pass

def safe_filename(name: str) -> str:
    clean = "".join(c for c in (name or "") if c.isalnum() or c in "._-").strip("._-")
    return clean or "audio"

def run_ffmpeg(cmd: list[str]) -> None:
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if proc.returncode != 0:
        # recorta stderr para no reventar respuesta
        err = (proc.stderr or "")[-8000:]
        raise HTTPException(status_code=500, detail=f"FFmpeg error:\n{err}")

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

def save_upload_with_limit(upload: UploadFile, dst: Path, max_bytes: int) -> int:
    """
    Guarda el upload por chunks y corta si supera max_bytes.
    Retorna bytes escritos.
    """
    written = 0
    chunk_size = 1024 * 1024  # 1MB

    try:
        with dst.open("wb") as out:
            while True:
                chunk = upload.file.read(chunk_size)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    # borrar parcial
                    try:
                        out.close()
                    except Exception:
                        pass
                    cleanup_files(dst)
                    raise HTTPException(
                        status_code=413,
                        detail="El archivo supera el límite máximo permitido (100MB)."
                    )
                out.write(chunk)
    finally:
        try:
            upload.file.close()
        except Exception:
            pass

    return written

# ==============================
# ROUTES
# ==============================

@app.get("/api/health")
def health():
    return {"ok": True}

@app.post("/api/master")
async def master(
    file: UploadFile = File(...),
    preset: str = Form("clean"),
    intensity: int = Form(55),
):
    if not file or not file.filename:
        raise HTTPException(status_code=400, detail="Archivo inválido")

    job_id = uuid.uuid4().hex[:8]
    base = safe_filename(file.filename)
    ext = Path(file.filename).suffix.lower()[:10]  # conserva extensión corta

    in_path = TMP_DIR / f"in_{job_id}_{base}{ext}"
    clean_path = TMP_DIR / f"clean_{job_id}.wav"
    out_path = TMP_DIR / f"master_{job_id}.wav"

    # 1) Guardar con límite real
    save_upload_with_limit(file, in_path, MAX_FILE_SIZE)

    # 2) Normalizar a WAV (acepta más formatos)
    cmd_clean = [
        "ffmpeg", "-y",
        "-hide_banner",
        "-i", str(in_path),
        "-vn",
        "-ac", "2",
        "-ar", "44100",
        "-af", "aresample=44100:async=1:first_pts=0",
        str(clean_path)
    ]
    run_ffmpeg(cmd_clean)

    # 3) Master
    filters = preset_chain(preset, intensity)
    cmd_master = [
        "ffmpeg", "-y",
        "-hide_banner",
        "-i", str(clean_path),
        "-vn",
        "-af", filters,
        "-ac", "2",
        "-ar", "44100",
        "-sample_fmt", "s16",
        str(out_path)
    ]
    run_ffmpeg(cmd_master)

    if not out_path.exists() or out_path.stat().st_size < 1024:
        cleanup_files(in_path, clean_path, out_path)
        raise HTTPException(status_code=500, detail="Master no generado o vacío")

    return FileResponse(
        path=str(out_path),
        media_type="audio/wav",
        filename="warmaster_master.wav",
        background=BackgroundTask(cleanup_files, in_path, clean_path, out_path),
    )

@app.get("/")
def root():
    return RedirectResponse(url="/index.html")

# Sirve /public
app.mount("/", StaticFiles(directory=str(PUBLIC_DIR), html=True), name="public")
