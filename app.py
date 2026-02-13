import json
import uuid
import shutil
import subprocess
from pathlib import Path
from typing import Optional, Dict, Any

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

# Targets (puedes ajustar)
TARGETS_I = {
    "spotify": -14.0,
    "youtube": -14.0,
    "apple": -16.0,
    "club": -9.0,
    "default": -14.0,
}
DEFAULT_TP = -1.0   # True Peak
DEFAULT_LRA = 11.0  # Loudness Range

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
        text=True,
        encoding="utf-8",
        errors="replace",
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

    # Nota: dejamos un limiter suave, y loudnorm hará el ajuste final si lo usas
    return (
        f"{eq},"
        f"acompressor=threshold={thr}dB:ratio={ratio}:attack=12:release=120:makeup={makeup},"
        f"alimiter=limit=-1.0dB"
    )


def extract_last_json_object(text: str) -> Optional[Dict[str, Any]]:
    """
    Extrae el ÚLTIMO objeto JSON válido desde un texto grande (stderr),
    de forma robusta (FFmpeg imprime el JSON de loudnorm mezclado con logs).
    """
    if not text:
        return None

    # Buscamos desde el final hacia atrás el inicio de un JSON "{"
    # y tratamos de parsear usando un stack de llaves.
    s = text
    for start in range(len(s) - 1, -1, -1):
        if s[start] != "{":
            continue
        depth = 0
        for end in range(start, len(s)):
            ch = s[end]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = s[start:end + 1].strip()
                    try:
                        return json.loads(candidate)
                    except Exception:
                        break
    return None


def loudnorm_pass1_measure(path: Path, target_i: float, tp: float, lra: float) -> Dict[str, Any]:
    """
    Pass 1: no genera archivo, solo mide.
    loudnorm imprime JSON en stderr cuando print_format=json.
    """
    af = f"loudnorm=I={target_i}:TP={tp}:LRA={lra}:print_format=json"
    cmd = [
        "ffmpeg", "-y",
        "-hide_banner", "-nostats",
        "-i", str(path),
        "-vn",
        "-af", af,
        "-f", "null", "-"
    ]
    proc = run_cmd(cmd)

    # ffmpeg en pass1 suele devolver 0 si ok
    if proc.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail="No se pudo medir loudnorm (pass1). FFmpeg falló.\n" + proc.stderr[-2000:]
        )

    data = extract_last_json_object(proc.stderr)
    if not data:
        raise HTTPException(
            status_code=500,
            detail='No se pudo leer medición loudnorm (pass1).'
                   '\nTip: revisa el log abajo:\n' + proc.stderr[-2000:]
        )

    return data


def loudnorm_second_pass(
    in_path: Path,
    out_path: Path,
    target_i: float,
    tp: float,
    lra: float,
    measured: Dict[str, Any]
) -> None:
    """
    Pass 2: aplica loudnorm usando mediciones del pass1.
    """
    # Algunos valores vienen como strings. Los normalizamos.
    def get_num(key: str) -> str:
        v = measured.get(key)
        if v is None:
            raise HTTPException(status_code=500, detail=f"Medición loudnorm incompleta: falta {key}")
        return str(v)

    measured_I = get_num("input_i")
    measured_TP = get_num("input_tp")
    measured_LRA = get_num("input_lra")
    measured_thresh = get_num("input_thresh")
    offset = get_num("target_offset")

    af = (
        "loudnorm="
        f"I={target_i}:TP={tp}:LRA={lra}:"
        f"measured_I={measured_I}:measured_TP={measured_TP}:measured_LRA={measured_LRA}:"
        f"measured_thresh={measured_thresh}:offset={offset}:linear=true:print_format=summary"
    )

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
    run_ffmpeg(cmd)


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

    # ✅ Target loudness (opcional). Si no mandas nada, default/spotify
    target: str = Form("default"),
):
    if not file or not file.filename:
        raise HTTPException(status_code=400, detail="Archivo inválido.")

    job_id = uuid.uuid4().hex[:8]
    safe_name = safe_filename(file.filename)

    in_path = TMP_DIR / f"in_{job_id}_{safe_name}"
    pre_path = TMP_DIR / f"pre_{job_id}.wav"     # audio pre-procesado
    out_path = TMP_DIR / f"master_{job_id}.wav"  # salida final

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
    # VALIDAR TAMAÑO
    # =========================
    size_bytes = in_path.stat().st_size
    if size_bytes > MAX_FILE_SIZE_BYTES:
        cleanup_files(in_path)
        raise HTTPException(status_code=400, detail=f"El archivo supera el límite de {MAX_FILE_SIZE_MB}MB.")

    # =========================
    # VALIDAR DURACIÓN
    # =========================
    duration = get_audio_duration_seconds(in_path)
    if duration > MAX_DURATION_SECONDS:
        cleanup_files(in_path)
        raise HTTPException(status_code=400, detail="El audio supera el límite de 6 minutos.")

    # =========================
    # 1) PRE-PROCESO (preset/comp/limiter) a WAV limpio
    # =========================
    filters = preset_chain(preset, intensity)
    cmd_pre = [
        "ffmpeg", "-y",
        "-hide_banner", "-nostats",
        "-i", str(in_path),
        "-vn",
        "-af", filters,
        "-ar", "44100",
        "-ac", "2",
        "-sample_fmt", "s16",
        str(pre_path)
    ]
    run_ffmpeg(cmd_pre)

    if not pre_path.exists() or pre_path.stat().st_size < 1024:
        cleanup_files(in_path, pre_path)
        raise HTTPException(status_code=500, detail="No se pudo preparar el audio para normalización.")

    # =========================
    # 2) LOUDNORM 2-PASS (profesional)
    # =========================
    t = (target or "default").strip().lower()
    target_i = TARGETS_I.get(t, TARGETS_I["default"])
    tp = DEFAULT_TP
    lra = DEFAULT_LRA

    measured = loudnorm_pass1_measure(pre_path, target_i=target_i, tp=tp, lra=lra)
    loudnorm_second_pass(pre_path, out_path, target_i=target_i, tp=tp, lra=lra, measured=measured)

    if not out_path.exists() or out_path.stat().st_size < 1024:
        cleanup_files(in_path, pre_path, out_path)
        raise HTTPException(status_code=500, detail="Master no generado o vacío.")

    # Limpieza (incluye pre_path)
    bg = BackgroundTask(cleanup_files, in_path, pre_path, out_path)

    return FileResponse(
        path=str(out_path),
        media_type="audio/wav",
        filename="warmaster_master.wav",
        background=bg,
    )


@app.get("/")
def root():
    return RedirectResponse(url="/index.html")


# =========================
# STATIC
# =========================

app.mount("/", StaticFiles(directory=str(PUBLIC_DIR), html=True), name="public")
