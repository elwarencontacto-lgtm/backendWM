import json
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

# Target loudness presets (LUFS integrados)
TARGETS = {
    "spotify":  {"I": -14.0, "TP": -1.0, "LRA": 11.0},
    "youtube":  {"I": -14.0, "TP": -1.0, "LRA": 11.0},
    "apple":    {"I": -16.0, "TP": -1.0, "LRA": 11.0},
    "club":     {"I": -9.0,  "TP": -1.0, "LRA": 8.0},
    "radio":    {"I": -12.0, "TP": -1.0, "LRA": 10.0},
    "default":  {"I": -14.0, "TP": -1.0, "LRA": 11.0},
}

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


def run_cmd(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )


def run_ffmpeg(cmd: list[str]) -> None:
    proc = run_cmd(cmd)
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
    Usa ffprobe para obtener duración real del audio
    """
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path)
    ]
    proc = run_cmd(cmd)

    if proc.returncode != 0:
        raise HTTPException(status_code=400, detail="No se pudo analizar la duración del archivo.")

    try:
        return float(proc.stdout.strip())
    except Exception:
        raise HTTPException(status_code=400, detail="Duración inválida.")


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

    # Nota: dejamos el "alimiter" fuera porque loudnorm ya controla el true peak.
    return (
        f"{eq},"
        f"acompressor=threshold={thr}dB:ratio={ratio}:attack=12:release=120:makeup={makeup}"
    )


def pick_target(target: str | None) -> dict:
    t = (target or "default").strip().lower()
    return TARGETS.get(t, TARGETS["default"])


def loudnorm_two_pass(in_path: Path, out_path: Path, pre_filters: str, target: dict) -> None:
    """
    2-pass EBU R128 loudnorm:
    Pass 1: analiza y obtiene measured_* en JSON
    Pass 2: aplica loudnorm con measured_* para llegar al target (I/TP/LRA)
    """
    I = float(target["I"])
    TP = float(target["TP"])
    LRA = float(target["LRA"])

    # 1) PASS 1 (analysis)
    # -af: pre_filters + loudnorm print_format=json
    pass1_filter = f"{pre_filters},loudnorm=I={I}:TP={TP}:LRA={LRA}:print_format=json"
    cmd1 = [
        "ffmpeg", "-y",
        "-hide_banner",
        "-i", str(in_path),
        "-vn",
        "-af", pass1_filter,
        "-f", "null", "-"
    ]

    p1 = run_cmd(cmd1)
    if p1.returncode != 0:
        raise HTTPException(status_code=500, detail=f"FFmpeg (pass1) error:\n{p1.stderr[-8000:]}")

    # Extraer el JSON que imprime loudnorm desde stderr
    measured = None
    lines = p1.stderr.splitlines()
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip().startswith("{"):
            blob = "\n".join(lines[i:])
            try:
                measured = json.loads(blob)
                break
            except Exception:
                measured = None
                break

    if not measured:
        raise HTTPException(
            status_code=500,
            detail="No se pudo leer medición loudnorm (pass1)."
        )

    # 2) PASS 2 (apply)
    # Usamos measured_* y offset
    m_I = measured.get("input_i")
    m_TP = measured.get("input_tp")
    m_LRA = measured.get("input_lra")
    m_thresh = measured.get("input_thresh")
    m_offset = measured.get("target_offset")

    if any(v is None for v in [m_I, m_TP, m_LRA, m_thresh, m_offset]):
        raise HTTPException(
            status_code=500,
            detail="Medición loudnorm incompleta (pass1)."
        )

    pass2_filter = (
        f"{pre_filters},"
        f"loudnorm=I={I}:TP={TP}:LRA={LRA}:"
        f"measured_I={m_I}:measured_TP={m_TP}:measured_LRA={m_LRA}:measured_thresh={m_thresh}:"
        f"offset={m_offset}:linear=true:print_format=summary"
    )

    cmd2 = [
        "ffmpeg", "-y",
        "-hide_banner",
        "-i", str(in_path),
        "-vn",
        "-af", pass2_filter,
        "-ar", "44100",
        "-ac", "2",
        "-sample_fmt", "s16",
        str(out_path)
    ]

    run_ffmpeg(cmd2)


# =========================
# ENDPOINTS
# =========================

@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/master")
def master_get_hint():
    return JSONResponse(
        {"detail": "Method Not Allowed. Usa POST /api/master con form-data: file, preset, intensity, (opcional) target."},
        status_code=405
    )


@app.post("/api/master")
async def master(
    file: UploadFile = File(...),
    preset: str = Form("clean"),
    intensity: int = Form(55),
    target: str = Form("default"),  # NUEVO (opcional): spotify/youtube/apple/club/radio/default
):
    if not file or not file.filename:
        raise HTTPException(status_code=400, detail="Archivo inválido.")

    job_id = uuid.uuid4().hex[:8]
    safe_name = safe_filename(file.filename)

    in_path = TMP_DIR / f"in_{job_id}_{safe_name}"
    out_path = TMP_DIR / f"master_{job_id}.wav"

    # =========================
    # GUARDAR ARCHIVO
    # =========================
    try:
        with in_path.open("wb") as f:
            shutil.copyfileobj(file.file, f)
    finally:
        try:
            file.file.close()
        except Exception:
            pass

    # =========================
    # VALIDAR TAMAÑO REAL
    # =========================
    size_bytes = in_path.stat().st_size
    if size_bytes > MAX_FILE_SIZE_BYTES:
        cleanup_files(in_path)
        raise HTTPException(
            status_code=400,
            detail=f"El archivo supera el límite de {MAX_FILE_SIZE_MB}MB."
        )

    # =========================
    # VALIDAR DURACIÓN REAL
    # =========================
    duration = get_audio_duration_seconds(in_path)
    if duration > MAX_DURATION_SECONDS:
        cleanup_files(in_path)
        raise HTTPException(
            status_code=400,
            detail="El audio supera el límite de 6 minutos."
        )

    # =========================
    # PROCESAR MASTER (con Target Loudness real)
    # =========================
    pre_filters = preset_chain(preset, intensity)
    t = pick_target(target)

    try:
        loudnorm_two_pass(in_path, out_path, pre_filters, t)
    except HTTPException:
        cleanup_files(in_path, out_path)
        raise
    except Exception as e:
        cleanup_files(in_path, out_path)
        raise HTTPException(status_code=500, detail=f"Error procesando master: {e}")

    if not out_path.exists() or out_path.stat().st_size < 1024:
        cleanup_files(in_path, out_path)
        raise HTTPException(status_code=500, detail="Master no generado o vacío.")

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
