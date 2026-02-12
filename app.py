import os
import shutil
import tempfile
import subprocess
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

APP_DIR = Path(__file__).resolve().parent
PUBLIC_DIR = APP_DIR / "public"

app = FastAPI()

# CORS (por si llamas con ?api=... desde otro dominio)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Servir estáticos desde /public
app.mount("/", StaticFiles(directory=str(PUBLIC_DIR), html=True), name="public")


@app.get("/api/health")
def health():
    return {"ok": True}


def run_ffmpeg(cmd: list[str]) -> None:
    # Render a veces necesita PATH explícito si cambias la imagen base; con Debian suele estar ok.
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-2000:])


@app.post("/api/master")
async def master_audio(
    file: UploadFile = File(...),
    # parámetros simples para “beta”
    target_lufs: float = Form(-12.0),
    true_peak: float = Form(-1.0),
    output_format: str = Form("wav"),  # "wav" o "mp3"
):
    if not file.filename:
        return JSONResponse({"error": "Archivo inválido."}, status_code=400)

    output_format = (output_format or "wav").lower().strip()
    if output_format not in {"wav", "mp3"}:
        output_format = "wav"

    # Crear workspace temporal
    workdir = Path(tempfile.mkdtemp(prefix="wm_"))
    try:
        in_path = workdir / f"input_{Path(file.filename).name}"
        with open(in_path, "wb") as f:
            shutil.copyfileobj(file.file, f)

        out_ext = "mp3" if output_format == "mp3" else "wav"
        out_path = workdir / f"mastered.{out_ext}"

        # Cadena “segura” y estable para beta:
        # - Loudness normalization EBU R128 (loudnorm) a target LUFS
        # - Limiter con true peak
        # - Resample a 48k para consistencia (puedes cambiar a 44.1k si quieres)
        #
        # Nota: loudnorm con medición en 2-pass sería más preciso, pero 1-pass es más simple/rápido.
        af = (
            f"loudnorm=I={target_lufs}:TP={true_peak}:LRA=11,"
            f"alimiter=limit={true_peak}:level_in=1:level_out=1"
        )

        cmd = ["ffmpeg", "-y", "-i", str(in_path), "-vn", "-af", af, "-ar", "48000"]

        if output_format == "mp3":
            cmd += ["-codec:a", "libmp3lame", "-b:a", "320k", str(out_path)]
            mime = "audio/mpeg"
        else:
            cmd += ["-codec:a", "pcm_s16le", str(out_path)]
            mime = "audio/wav"

        run_ffmpeg(cmd)

        return FileResponse(
            path=str(out_path),
            media_type=mime,
            filename=f"mastered.{out_ext}",
        )

    except Exception as e:
        return JSONResponse({"error": "Error al masterizar.", "detail": str(e)}, status_code=500)
    finally:
        try:
            shutil.rmtree(workdir, ignore_errors=True)
        except Exception:
            pass
