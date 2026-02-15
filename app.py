import re
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

BASE_DIR = Path(__file__).parent
TMP_DIR = BASE_DIR / "tmp"
TMP_DIR.mkdir(exist_ok=True)

PUBLIC_DIR = BASE_DIR / "public"
PUBLIC_DIR.mkdir(exist_ok=True)

app = FastAPI()

# Targets “pro” (gratis) — ajustables
TARGETS = {
    # I = Integrated LUFS, TP = True Peak dBTP, LRA = Loudness Range
    "spotify":        {"I": -14.0, "TP": -1.0, "LRA": 11.0},
    "youtube":        {"I": -14.0, "TP": -1.0, "LRA": 11.0},
    "apple":          {"I": -16.0, "TP": -1.0, "LRA": 11.0},
    "streaming_safe": {"I": -14.0, "TP": -1.0, "LRA":  8.0},
    "club":           {"I":  -9.0, "TP": -1.0, "LRA":  7.0},
    "radio":          {"I": -10.0, "TP": -1.0, "LRA":  7.0},
}

DEFAULT_TARGET = "spotify"


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


def run_proc(cmd: list[str]) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return proc


def run_ffmpeg_or_500(cmd: list[str], label: str = "FFmpeg") -> subprocess.CompletedProcess:
    proc = run_proc(cmd)
    if proc.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=f"{label} error:\n{proc.stderr[-8000:]}",
        )
    return proc


def safe_filename(name: str) -> str:
    clean = "".join(c for c in (name or "") if c.isalnum() or c in "._-").strip("._-")
    return clean or "audio.wav"


def get_audio_duration_seconds(path: Path) -> float:
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    proc = run_proc(cmd)
    if proc.returncode != 0:
        raise HTTPException(status_code=400, detail="No se pudo analizar la duración del archivo.")
    try:
        return float(proc.stdout.strip())
    except Exception:
        raise HTTPException(status_code=400, detail="Duración inválida.")


_LUFS_RE = re.compile(r"\bI:\s*(-?\d+(?:\.\d+)?)\s*LUFS\b", re.IGNORECASE)

def measure_lufs_ebur128(path: Path) -> float:
    # ebur128 escribe en STDERR
    cmd = [
        "ffmpeg", "-hide_banner", "-nostats",
        "-i", str(path),
        "-filter_complex", "ebur128=peak=true",
        "-f", "null", "-"
    ]
    proc = run_proc(cmd)
    if proc.returncode != 0:
        raise HTTPException(status_code=500, detail="No se pudo medir LUFS (ffmpeg ebur128).")
    matches = _LUFS_RE.findall(proc.stderr)
    if not matches:
        raise HTTPException(status_code=500, detail="No se pudo leer medición LUFS (ebur128).")
    last = matches[-1]
    try:
        return float(last)
    except Exception:
        raise HTTPException(status_code=500, detail="No se pudo parsear LUFS (ebur128).")


def preset_chain_pro(preset: str, intensity: int) -> dict:
    """
    Devuelve parámetros “pro” (multibanda + saturación + stereo width) guiados por preset/intensity.
    """
    intensity = max(0, min(100, int(intensity)))
    p = (preset or "clean").lower()

    # Compresión multibanda (ratios suaves; suben con intensidad)
    # Umbrales aproximados en dB (más negativo = más compresión)
    thr_base = -22.0 - (intensity * 0.08)  # de -22 a ~-30
    ratio_base = 1.6 + (intensity * 0.02)  # de 1.6 a 3.6

    # Saturación (softclip “drive” suave)
    drive = 0.15 + (intensity / 100) * 0.55  # 0.15..0.70

    # Stereo width (controlado)
    width = 1.00 + (intensity / 100) * 0.20  # 1.00..1.20

    # EQ tilt por preset (muy sutil para no romper mixes)
    if p == "club":
        eq = "bass=g=4:f=90,treble=g=2:f=9000"
        width = min(1.25, width + 0.05)
    elif p == "warm":
        eq = "bass=g=3:f=160,treble=g=-2:f=4500"
        width = max(0.95, width - 0.03)
    elif p == "bright":
        eq = "bass=g=-1:f=120,treble=g=4:f=8500"
        width = min(1.25, width + 0.03)
    elif p == "heavy":
        eq = "bass=g=5:f=90,treble=g=1:f=3500"
        width = max(0.95, width - 0.02)
    else:
        eq = "bass=g=2:f=120,treble=g=1:f=8000"

    # Ajustes por banda (low/mid/high)
    # Low un poco más controlado, high más suave para evitar “harsh”
    mb = {
        "low":  {"thr": thr_base - 2.0, "ratio": min(5.0, ratio_base + 0.3)},
        "mid":  {"thr": thr_base,       "ratio": ratio_base},
        "high": {"thr": thr_base + 1.0, "ratio": max(1.2, ratio_base - 0.2)},
    }

    return {"eq": eq, "mb": mb, "drive": drive, "width": width}


def build_multiband_chain(eq: str, mb: dict, drive: float, width: float) -> str:
    """
    Cadena pro:
    - EQ suave (bass/treble)
    - Multibanda real (3 bandas) con compresión por banda
    - Saturación real (asoftclip)
    - Stereo tools (ancho controlado)
    """
    # 3 bandas: <120Hz, 120-4000Hz, >4000Hz
    low_thr = mb["low"]["thr"]
    low_ratio = mb["low"]["ratio"]

    mid_thr = mb["mid"]["thr"]
    mid_ratio = mb["mid"]["ratio"]

    high_thr = mb["high"]["thr"]
    high_ratio = mb["high"]["ratio"]

    # “Knee/attack/release” moderados
    low_comp = f"acompressor=threshold={low_thr}dB:ratio={low_ratio}:attack=15:release=120:knee=6"
    mid_comp = f"acompressor=threshold={mid_thr}dB:ratio={mid_ratio}:attack=12:release=110:knee=6"
    high_comp = f"acompressor=threshold={high_thr}dB:ratio={high_ratio}:attack=8:release=90:knee=6"

    # Saturación: asoftclip (drive/clip); valores moderados
    # “type=soft” existe en builds comunes; si no, igual funciona sin type.
    sat = f"asoftclip=type=soft:threshold={max(0.10, min(0.95, 1.0 - drive*0.35))}"

    # Stereo width controlado (mlev=mid, slev=side)
    # slev>1 aumenta “side”; conservador para no romper mono.
    slev = max(0.90, min(1.35, width))
    stereo = f"stereotools=mlev=1.0:slev={slev}:phasel=0"

    chain = (
        f"{eq},"
        # Multibanda real:
        "asplit=3[aL][aM][aH];"
        f"[aL]lowpass=f=120,{low_comp}[low];"
        f"[aM]highpass=f=120,lowpass=f=4000,{mid_comp}[mid];"
        f"[aH]highpass=f=4000,{high_comp}[high];"
        "[low][mid][high]amix=inputs=3:normalize=0,"
        # Glue compression suave (para pegar)
        "acompressor=threshold=-18dB:ratio=1.4:attack=10:release=120:knee=6,"
        # Saturación real
        f"{sat},"
        # Stereo ancho controlado
        f"{stereo}"
    )
    return chain


_JSON_OBJ_RE = re.compile(r"\{[\s\S]*?\}", re.MULTILINE)

def loudnorm_pass1_json(in_path: Path, pre_chain: str, I: float, TP: float, LRA: float) -> dict:
    """
    Pass 1: corre el mismo pre_chain + loudnorm(print_format=json) y extrae el JSON.
    """
    af = f"{pre_chain},loudnorm=I={I}:TP={TP}:LRA={LRA}:print_format=json"
    cmd = [
        "ffmpeg", "-hide_banner", "-nostats",
        "-i", str(in_path),
        "-vn",
        "-af", af,
        "-f", "null", "-"
    ]
    proc = run_proc(cmd)
    if proc.returncode != 0:
        raise HTTPException(status_code=500, detail="No se pudo medir loudnorm (pass1).")

    # Busca el último JSON válido que contenga input_i
    candidates = _JSON_OBJ_RE.findall(proc.stderr)
    for js in reversed(candidates):
        try:
            obj = json.loads(js)
            if "input_i" in obj and "input_tp" in obj and "input_lra" in obj:
                return obj
        except Exception:
            continue

    raise HTTPException(status_code=500, detail="No se pudo leer medición loudnorm (pass1).")


def loudnorm_pass2_render(in_path: Path, out_path: Path, pre_chain: str, target: dict, measured: dict) -> None:
    """
    Pass 2: aplica loudnorm con parámetros medidos para un resultado “pro”.
    """
    I = target["I"]; TP = target["TP"]; LRA = target["LRA"]

    # measured params
    mi = measured.get("input_i")
    m_tp = measured.get("input_tp")
    m_lra = measured.get("input_lra")
    m_thresh = measured.get("input_thresh")
    m_offset = measured.get("target_offset")

    if any(v is None for v in [mi, m_tp, m_lra, m_thresh, m_offset]):
        raise HTTPException(status_code=500, detail="Medición loudnorm incompleta (pass1).")

    loudnorm2 = (
        f"loudnorm=I={I}:TP={TP}:LRA={LRA}:"
        f"measured_I={mi}:measured_TP={m_tp}:measured_LRA={m_lra}:"
        f"measured_thresh={m_thresh}:offset={m_offset}:"
        "linear=true:print_format=summary"
    )

    # True peak safety limiter al final
    af = f"{pre_chain},{loudnorm2},alimiter=limit={TP}dB"

    cmd = [
        "ffmpeg", "-y",
        "-hide_banner", "-nostats",
        "-i", str(in_path),
        "-vn",
        "-af", af,
        "-ar", "44100",
        "-ac", "2",
        "-sample_fmt", "s16",
        str(out_path)
    ]
    run_ffmpeg_or_500(cmd, label="FFmpeg render (pass2)")


def loudnorm_onepass_render(in_path: Path, out_path: Path, pre_chain: str, target: dict) -> None:
    """
    Fallback: loudnorm en una pasada (menos “pro” pero muy estable).
    """
    I = target["I"]; TP = target["TP"]; LRA = target["LRA"]
    af = f"{pre_chain},loudnorm=I={I}:TP={TP}:LRA={LRA}:linear=true:print_format=summary,alimiter=limit={TP}dB"
    cmd = [
        "ffmpeg", "-y",
        "-hide_banner", "-nostats",
        "-i", str(in_path),
        "-vn",
        "-af", af,
        "-ar", "44100",
        "-ac", "2",
        "-sample_fmt", "s16",
        str(out_path)
    ]
    run_ffmpeg_or_500(cmd, label="FFmpeg render (onepass)")


# =========================
# ENDPOINTS
# =========================
@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/master")
def master_get_hint():
    return JSONResponse(
        {"detail": "Method Not Allowed. Usa POST /api/master con form-data: file, preset, intensity, target."},
        status_code=405
    )


@app.post("/api/master")
async def master(
    file: UploadFile = File(...),
    preset: str = Form("clean"),
    intensity: int = Form(55),
    target: str = Form(DEFAULT_TARGET),  # NUEVO
):
    if not file or not file.filename:
        raise HTTPException(status_code=400, detail="Archivo inválido.")

    job_id = uuid.uuid4().hex[:8]
    safe_name = safe_filename(file.filename)

    in_path = TMP_DIR / f"in_{job_id}_{safe_name}"
    clean_path = TMP_DIR / f"clean_{job_id}.wav"
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
        raise HTTPException(status_code=400, detail=f"El archivo supera el límite de {MAX_FILE_SIZE_MB}MB.")

    # =========================
    # VALIDAR DURACIÓN REAL
    # =========================
    duration = get_audio_duration_seconds(in_path)
    if duration > MAX_DURATION_SECONDS:
        cleanup_files(in_path)
        raise HTTPException(status_code=400, detail="El audio supera el límite de 6 minutos.")

    # =========================
    # NORMALIZAR INPUT A WAV LIMPIO (robusto)
    # =========================
    cmd_clean = [
        "ffmpeg", "-y",
        "-hide_banner", "-nostats",
        "-i", str(in_path),
        "-vn",
        "-ac", "2",
        "-ar", "44100",
        "-af", "aresample=44100:async=1:first_pts=0",
        "-sample_fmt", "s16",
        str(clean_path)
    ]
    run_ffmpeg_or_500(cmd_clean, label="FFmpeg clean")

    # =========================
    # TARGET
    # =========================
    tkey = (target or DEFAULT_TARGET).strip().lower()
    tconf = TARGETS.get(tkey, TARGETS[DEFAULT_TARGET])

    # =========================
    # MEDIR LUFS ORIGINAL (EBUR128)
    # =========================
    try:
        lufs_original = measure_lufs_ebur128(clean_path)
    except HTTPException:
        lufs_original = None

    # =========================
    # CADENA PRO (multibanda + saturación + stereo)
    # =========================
    pro = preset_chain_pro(preset, intensity)
    pre_chain = build_multiband_chain(eq=pro["eq"], mb=pro["mb"], drive=pro["drive"], width=pro["width"])

    # =========================
    # LOUDNORM 2-PASS (PRO) con fallback 1-pass
    # =========================
    used_two_pass = True
    try:
        measured = loudnorm_pass1_json(clean_path, pre_chain, tconf["I"], tconf["TP"], tconf["LRA"])
        loudnorm_pass2_render(clean_path, out_path, pre_chain, tconf, measured)
    except HTTPException:
        used_two_pass = False
        loudnorm_onepass_render(clean_path, out_path, pre_chain, tconf)

    if not out_path.exists() or out_path.stat().st_size < 1024:
        cleanup_files(in_path, clean_path, out_path)
        raise HTTPException(status_code=500, detail="Master no generado o vacío.")

    # =========================
    # MEDIR LUFS MASTER (EBUR128)
    # =========================
    try:
        lufs_master = measure_lufs_ebur128(out_path)
    except HTTPException:
        lufs_master = None

    headers = {
        "X-Target": tkey,
        "X-Two-Pass": "1" if used_two_pass else "0",
    }
    if lufs_original is not None:
        headers["X-LUFS-Original"] = f"{lufs_original:.1f}"
    if lufs_master is not None:
        headers["X-LUFS-Master"] = f"{lufs_master:.1f}"

    return FileResponse(
        path=str(out_path),
        media_type="audio/wav",
        filename="warmaster_master.wav",
        headers=headers,
        background=BackgroundTask(cleanup_files, in_path, clean_path, out_path),
    )


@app.get("/")
def root():
    return RedirectResponse(url="/index.html")


# STATIC (final)
app.mount("/", StaticFiles(directory=str(PUBLIC_DIR), html=True), name="public")
