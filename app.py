from __future__ import annotations

import uuid
import shutil
import subprocess
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Query
from fastapi.responses import FileResponse, RedirectResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles


# ========================
# CONFIG
# =========================
MAX_FILE_SIZE_MB_FREE = 100
MAX_FILE_SIZE_BYTES_FREE = MAX_FILE_SIZE_MB_FREE * 1024 * 1024

FREE_PREVIEW_SECONDS = 30

DEFAULT_PREVIEW_SECONDS = 15
MAX_PREVIEW_SECONDS = 30

FFMPEG_TIMEOUT_SECONDS = 240  # 4 min

BASE_DIR = Path(__file__).parent
TMP_DIR = BASE_DIR / "tmp"
TMP_DIR.mkdir(exist_ok=True)

PUBLIC_DIR = BASE_DIR / "public"
PUBLIC_DIR.mkdir(exist_ok=True)

app = FastAPI()

# =========================
# STORAGE (MEMORIA)
# =========================
masters: Dict[str, Dict[str, Any]] = {}  # master_id -> metadata

# =========================
# CORS
# =========================
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
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


def run_cmd(cmd: list[str], timeout_s: int = FFMPEG_TIMEOUT_SECONDS) -> None:
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="FFmpeg timeout (proceso muy largo / Render cortó).")

    if proc.returncode != 0:
        raise HTTPException(status_code=500, detail=f"FFmpeg error:\n{proc.stderr[-8000:]}")


def safe_filename(name: str) -> str:
    clean = "".join(c for c in (name or "") if c.isalnum() or c in "._- ").strip().strip("._-")
    clean = clean.replace(" ", "_")
    return clean or "audio"


def clamp_float(x: Any, lo: float, hi: float, default: float) -> float:
    try:
        v = float(x)
        if v != v:
            return default
        return max(lo, min(hi, v))
    except Exception:
        return default


def clamp_int(x: Any, lo: int, hi: int, default: int) -> int:
    try:
        v = int(float(x))
        return max(lo, min(hi, v))
    except Exception:
        return default


def normalize_quality(q: Optional[str]) -> str:
    q = (q or "FREE").strip().upper()
    if q not in ("FREE", "PLUS", "PRO"):
        return "FREE"
    return q


def resolve_master_wav(master_id: str) -> Path:
    return TMP_DIR / f"master_{master_id}.wav"


def resolve_orig_path(master_id: str) -> Path:
    m = masters.get(master_id)
    if not m:
        raise HTTPException(status_code=404, detail="Master no encontrado.")
    title = safe_filename(m.get("title") or "audio")
    return TMP_DIR / f"orig_{master_id}_{title}"


def clamp_knobs(
    k_low: Any, k_mid: Any, k_pres: Any, k_air: Any,
    k_glue: Any, k_width: Any, k_sat: Any, k_out: Any
) -> dict[str, float]:
    return {
        "k_low": clamp_float(k_low, -12.0, 12.0, 0.0),
        "k_mid": clamp_float(k_mid, -12.0, 12.0, 0.0),
        "k_pres": clamp_float(k_pres, -12.0, 12.0, 0.0),
        "k_air": clamp_float(k_air, -12.0, 12.0, 0.0),
        "k_glue": clamp_float(k_glue, 0.0, 100.0, 0.0),
        "k_width": clamp_float(k_width, 50.0, 150.0, 100.0),
        "k_sat": clamp_float(k_sat, 0.0, 100.0, 0.0),
        "k_out": clamp_float(k_out, -12.0, 6.0, 0.0),
    }


# =========================
# AUDIO FILTER CHAIN
# =========================
def preset_chain(
    preset: str,
    intensity: Any,
    k_low: float,
    k_mid: float,
    k_pres: float,
    k_air: float,
    k_glue: float,
    k_width: float,
    k_sat: float,
    k_out: float,
) -> str:
    intensity_i = clamp_int(intensity, 0, 100, 55)

    thr = -18.0 - (intensity_i * 0.10)
    ratio = 2.0 + (intensity_i * 0.04)

    makeup = 2.0 + (intensity_i * 0.06)
    if makeup != makeup:
        makeup = 2.0
    makeup = max(1.0, min(64.0, makeup))

    preset = (preset or "clean").lower()

    if preset == "club":
        eq_base = "bass=g=4:f=90,treble=g=2:f=9000"
    elif preset == "warm":
        eq_base = "bass=g=3:f=160,treble=g=-2:f=4500"
    elif preset == "bright":
        eq_base = "bass=g=-1:f=120,treble=g=4:f=8500"
    elif preset == "heavy":
        eq_base = "bass=g=5:f=90,treble=g=2:f=3500"
    else:
        eq_base = "bass=g=2:f=120,treble=g=1:f=8000"

    eq_live = (
        f"equalizer=f=120:width_type=h:width=1:g={k_low},"
        f"equalizer=f=630:width_type=h:width=1:g={k_mid},"
        f"equalizer=f=1760:width_type=h:width=1:g={k_pres},"
        f"equalizer=f=8500:width_type=h:width=1:g={k_air}"
    )

    glue_p = max(0.0, min(100.0, k_glue)) / 100.0
    glue_thr = -12.0 - glue_p * 18.0
    glue_ratio = 1.2 + glue_p * 3.8
    glue_attack = max(0.001, 0.012 - glue_p * 0.007)
    glue_release = 0.20 + glue_p * 0.10
    glue_comp = (
        f"acompressor=threshold={glue_thr}dB:"
        f"ratio={glue_ratio}:attack={glue_attack}:release={glue_release}:makeup=1"
    )

    k = max(50.0, min(150.0, k_width)) / 100.0
    a = (1.0 + k) / 2.0
    b = (1.0 - k) / 2.0
    width_fx = f"pan=stereo|c0={a:.6f}*c0+{b:.6f}*c1|c1={b:.6f}*c0+{a:.6f}*c1"

    sat_p = max(0.0, min(100.0, k_sat)) / 100.0
    drive_db = sat_p * 6.0
    back_db = -sat_p * 4.0
    sat_comp_thr = -14.0 + sat_p * 6.0
    sat_comp_ratio = 1.2 + sat_p * 2.8
    sat_fx = (
        f"volume={drive_db}dB,"
        f"acompressor=threshold={sat_comp_thr}dB:ratio={sat_comp_ratio}:attack=2:release=80:makeup=1,"
        f"volume={back_db}dB"
    )

    out_fx = f"volume={k_out}dB"
    limiter = "alimiter=limit=-1.0dB"

    return (
        f"{eq_base},"
        f"{eq_live},"
        f"acompressor=threshold={thr}dB:ratio={ratio}:attack=12:release=120:makeup={makeup},"
        f"{glue_comp},"
        f"{width_fx},"
        f"{sat_fx},"
        f"{out_fx},"
        f"{limiter}"
    )


# =========================
# ENDPOINTS
# =========================
@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/me")
def me():
    return {"plan": "FREE"}


@app.get("/api/masters")
def list_masters():
    items = []
    for mid, m in masters.items():
        items.append({
            "id": mid,
            "title": m.get("title") or f"Master {mid}",
            "quality": normalize_quality(m.get("quality", "FREE")),
            "preset": m.get("preset", "clean"),
            "intensity": m.get("intensity", 55),
            "created_at": m.get("created_at"),
        })
    items.sort(key=lambda x: str(x.get("created_at") or ""), reverse=True)
    return items


@app.get("/api/masters/{master_id}/stream")
def api_stream_master(master_id: str):
    out_path = resolve_master_wav(master_id)
    if not out_path.exists():
        raise HTTPException(status_code=404, detail="Archivo no encontrado.")
    return FileResponse(
        path=str(out_path),
        media_type="audio/wav",
        filename="warmaster_master.wav",
        headers={"Content-Disposition": 'inline; filename="warmaster_master.wav"'}
    )


@app.post("/api/masters/{master_id}/render")
def render_final_from_master(
    master_id: str,
    k_low: float = Form(0.0),
    k_mid: float = Form(0.0),
    k_pres: float = Form(0.0),
    k_air: float = Form(0.0),
    k_glue: float = Form(0.0),
    k_width: float = Form(100.0),
    k_sat: float = Form(0.0),
    k_out: float = Form(0.0),
):
    m = masters.get(master_id)
    if not m:
        raise HTTPException(status_code=404, detail="Master no encontrado.")

    orig_path = resolve_orig_path(master_id)
    if not orig_path.exists():
        raise HTTPException(status_code=404, detail="Original no disponible en servidor.")

    rq = normalize_quality(m.get("quality", "FREE"))
    clip_seconds: Optional[int] = FREE_PREVIEW_SECONDS if rq == "FREE" else None

    knobs = clamp_knobs(k_low, k_mid, k_pres, k_air, k_glue, k_width, k_sat, k_out)

    filters = preset_chain(
        preset=m.get("preset", "clean"),
        intensity=m.get("intensity", 55),
        k_low=knobs["k_low"], k_mid=knobs["k_mid"], k_pres=knobs["k_pres"], k_air=knobs["k_air"],
        k_glue=knobs["k_glue"], k_width=knobs["k_width"], k_sat=knobs["k_sat"], k_out=knobs["k_out"]
    )

    out_path = resolve_master_wav(master_id)

    cmd = ["ffmpeg", "-y", "-hide_banner", "-i", str(orig_path)]
    if clip_seconds:
        cmd += ["-t", str(int(clip_seconds))]

    cmd += [
        "-vn",
        "-af", filters,
        "-ar", "44100",
        "-ac", "2",
        "-sample_fmt", "s16",
        str(out_path)
    ]
    run_cmd(cmd)

    if not out_path.exists() or out_path.stat().st_size < 1024:
        cleanup_files(out_path)
        raise HTTPException(status_code=500, detail="Master vacío.")

    fname = "warmaster_master.wav" if rq in ("PLUS", "PRO") else f"warmaster_preview_{FREE_PREVIEW_SECONDS}s.wav"
    return FileResponse(path=str(out_path), media_type="audio/wav", filename=fname)


@app.post("/api/master")
async def master(
    file: UploadFile = File(...),
    preset: str = Form("clean"),
    intensity: int = Form(55),

    k_low: float = Form(0.0),
    k_mid: float = Form(0.0),
    k_pres: float = Form(0.0),
    k_air: float = Form(0.0),
    k_glue: float = Form(0.0),
    k_width: float = Form(100.0),
    k_sat: float = Form(0.0),
    k_out: float = Form(0.0),

    requested_quality: Optional[str] = Form(None),
    target: Optional[str] = Form(None),
):
    if not file or not file.filename:
        raise HTTPException(status_code=400, detail="Archivo inválido.")

    master_id = uuid.uuid4().hex[:8]
    name = safe_filename(file.filename)

    in_path = TMP_DIR / f"in_{master_id}_{name}"
    orig_path = TMP_DIR / f"orig_{master_id}_{name}"
    out_path = resolve_master_wav(master_id)

    rq = normalize_quality(requested_quality)

    masters[master_id] = {
        "id": master_id,
        "title": name,
        "preset": preset,
        "intensity": int(clamp_int(intensity, 0, 100, 55)),
        "quality": rq,
        "created_at": datetime.utcnow().isoformat(),
        "knobs": {
            "low": k_low, "mid": k_mid, "pres": k_pres, "air": k_air,
            "glue": k_glue, "width": k_width, "sat": k_sat, "out": k_out
        }
    }

    # guardar upload
    try:
        with in_path.open("wb") as f:
            shutil.copyfileobj(file.file, f)
    finally:
        try:
            file.file.close()
        except Exception:
            pass

    # FREE: límite de subida 100MB
    if rq == "FREE" and in_path.stat().st_size > MAX_FILE_SIZE_BYTES_FREE:
        cleanup_files(in_path)
        raise HTTPException(status_code=400, detail="FREE: supera 100MB.")

    # guardar original persistente
    try:
        if orig_path.exists():
            orig_path.unlink()
    except Exception:
        pass

    try:
        in_path.rename(orig_path)
    except Exception:
        try:
            shutil.copyfile(str(in_path), str(orig_path))
        finally:
            cleanup_files(in_path)

    knobs = clamp_knobs(k_low, k_mid, k_pres, k_air, k_glue, k_width, k_sat, k_out)

    filters = preset_chain(
        preset=preset,
        intensity=intensity,
        k_low=knobs["k_low"], k_mid=knobs["k_mid"], k_pres=knobs["k_pres"], k_air=knobs["k_air"],
        k_glue=knobs["k_glue"], k_width=knobs["k_width"], k_sat=knobs["k_sat"], k_out=knobs["k_out"]
    )

    clip_seconds: Optional[int] = FREE_PREVIEW_SECONDS if rq == "FREE" else None

    cmd = ["ffmpeg", "-y", "-hide_banner", "-i", str(orig_path)]
    if clip_seconds:
        cmd += ["-t", str(int(clip_seconds))]

    cmd += [
        "-vn",
        "-af", filters,
        "-ar", "44100",
        "-ac", "2",
        "-sample_fmt", "s16",
        str(out_path)
    ]
    run_cmd(cmd)

    if not out_path.exists() or out_path.stat().st_size < 1024:
        cleanup_files(out_path)
        raise HTTPException(status_code=500, detail="Master vacío.")

    # ✅ CAMBIO CLAVE: devolvemos JSON con master_id
    return JSONResponse({
        "ok": True,
        "master_id": master_id,
        "quality": rq,
        "message": "Master generado"
    })


@app.get("/")
def root():
    return RedirectResponse(url="/index.html")


app.mount("/", StaticFiles(directory=str(PUBLIC_DIR), html=True), name="public")
