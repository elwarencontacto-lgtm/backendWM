importar uuid
importar shutil
subproceso de importación
desde pathlib importar Path
desde datetime importar datetime
desde escribir importar Dict, Cualquiera, Opcional

desde fastapi importar FastAPI, UploadFile, Archivo, Formulario, HTTPException, Consulta
desde fastapi.responses importar FileResponse, RedirectResponse
desde fastapi.middleware.cors importar CORSMiddleware
desde fastapi.staticfiles importar StaticFiles


# ========================
# CONFIG
# =========================
TAMAÑO MÁXIMO DE ARCHIVO MB = 100
TAMAÑO MÁXIMO DE ARCHIVO EN BYTES = TAMAÑO MÁXIMO DE ARCHIVO EN MB * 1024 * 1024
DURACIÓN MÁXIMA_SEGUNDOS = 6 * 60 # 6 minutos

FREE_PREVIEW_SECONDS = 30 # ✅ Descarga gratuita 30s

BASE_DIR = Ruta(__archivo__).padre
TMP_DIR = BASE_DIR / "tmp"
TMP_DIR.mkdir(exist_ok=Verdadero)

PÚBLICO_DIR = BASE_DIR / "público"
PUBLIC_DIR.mkdir(exist_ok=Verdadero)

aplicación = FastAPI()

# =========================
# ALMACENAMIENTO (MEMORIA)
# =========================
maestros: Dict[str, Dict[str, Any]] = {} # master_id -> metadatos

# =========================
# CORS
# =========================
aplicación.add_middleware(
    CORSMiddleware,
    permitir_orígenes=["*"],
    allow_credentials=Verdadero,
    permitir_métodos=["*"],
    permitir_encabezados=["*"],
)

# =========================
# UTILES
# =========================
def cleanup_files(*paths: Ruta) -> Ninguno:
    para p en rutas:
        intentar:
            si p y p.existen():
                p.desvincular()
        excepto Excepción:
            aprobar


def run_cmd(cmd: lista[str]) -> Ninguno:
    proc = subproceso.run(
        comando,
        stdout=subproceso.PIPE,
        stderr=subproceso.PIPE,
        texto=Verdadero
    )
    si proc.returncode != 0:
        generar HTTPException(
            código de estado=500,
            detalle=f"Error de FFmpeg:\n{proc.stderr[-8000:]}"
        )


def safe_filename(nombre: str) -> str:
    clean = "".join(c para c en (nombre o "") si c.isalnum() o c en "._- ").strip().strip("._-")
    limpiar = limpiar.reemplazar(" ", "_")
    devolver limpio o "audio"


def get_audio_duration_seconds(ruta: Ruta) -> float:
    comando = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "formato=duración",
        "-de", "predeterminado=noprint_wrappers=1:nokey=1",
        str(ruta)
    ]
    proc = subproceso.run(cmd, stdout=subproceso.PIPE, stderr=subproceso.PIPE, texto=True)
    si proc.returncode != 0:
        rise HTTPException(status_code=400, Detail="No se pudo analizar la duración.")
    intentar:
        devolver float(proc.stdout.strip())
    excepto Excepción:
        levantar HTTPException(status_code=400, detalle="Duración inválida.")


def clamp_float(x: Cualquiera, lo: flotante, hi: flotante, predeterminado: flotante) -> flotante:
    intentar:
        v = flotante(x)
        si v != v: # NaN
            devolver el valor predeterminado
        devuelve max(lo, min(hi, v))
    excepto Excepción:
        devolver el valor predeterminado


def clamp_int(x: Cualquiera, lo: int, hi: int, predeterminado: int) -> int:
    intentar:
        v = int(flotante(x))
        devuelve max(lo, min(hi, v))
    excepto Excepción:
        devolver el valor predeterminado


def normalizar_calidad(q: Opcional[str]) -> str:
    q = (q o "GRATIS").strip().upper()
    si q no está en ("GRATIS", "PLUS", "PRO"):
        devolver "GRATIS"
    devolver q


def cadena_predeterminada(
    preajuste: str,
    intensidad: Cualquiera,
    k_low: flotador,
    k_mid: flotante,
    k_pres: flotador,
    k_air: flotador,
    k_glue: flotador,
    k_width: flotante,
    k_sat: flotador,
    k_out: flotador,
) -> cadena:
    """
    Devuelve cadena de filtros FFmpeg -af

    ✅ Maquillaje FIX: siempre en rango [1..64]
    ✅ Ancho FIX: sin stereowiden (compatible con panorámica)
    ✅ FIX sat: sin asoftclip (evita incompatibilidades)
    """

    intensidad_i = abrazadera_int(intensidad, 0, 100, 55)

    thr = -18.0 - (intensidad_i * 0.10)
    relación = 2,0 + (intensidad_i * 0,04)

    # ✅ maquillaje siempre válido [1..64]
    maquillaje = 2.0 + (intensidad_i * 0.06)
    si maquillaje != maquillaje:
        maquillaje = 2.0
    maquillaje = máx(1.0, mín(64.0, maquillaje))

    preset = (preset o "limpio").lower()

    # EQ base por preajuste
    si el valor preestablecido == "club":
        base_ecual = "graves=sol=4:fa=90,agudos=sol=2:fa=9000"
    elif preset == "cálido":
        base_ecual = "graves=sol=3:fa=160,agudos=sol=-2:fa=4500"
    elif preestablecido == "brillante":
        base_ecual = "graves=sol=-1:fa=120,agudos=sol=4:fa=8500"
    elif preset == "pesado":
        base_ecual = "graves=sol=5:fa=90,agudos=sol=2:fa=3500"
    demás:
        base_ecual = "graves=sol=2:fa=120,agudos=sol=1:fa=8000"

    # ===== PERILLAS (aplican al audio final) =====
    # EQ Live: 4 bandas
    eq_live = (
        f"ecualizador=f=120:tipo_de_ancho=h:ancho=1:g={k_bajo},"
        f"ecualizador=f=630:tipo_de_ancho=h:ancho=1:g={k_mid},"
        f"ecualizador=f=1760:tipo_de_ancho=h:ancho=1:g={k_pres},"
        f"ecualizador=f=8500:tipo_de_ancho=h:ancho=1:g={k_aire}"
    )

    # Pegamento 0..100 (compresión suave adicional)
    pegamento_p = máx(0.0, mín(100.0, k_pegamento)) / 100.0
    pegamento_thr = -12.0 - pegamento_p * 18.0
    proporción de pegamento = 1,2 + pegamento_p * 3,8
    ataque_de_pegamento = máx.(0,001, 0,012 - pegamento_p * 0,007)
    liberación_de_pegamento = 0,20 + pegamento_p * 0,10
    pegamento_comp = (
        f"acompresor=umbral={glue_thr}dB:"
        f"ratio={proporción_de_pegamento}:ataque={ataque_de_pegamento}:liberación={liberación_de_pegamento}:maquillaje=1"
    )

    # ✅ ANCHO (50..150) Compatible con PAN
    # L = a*L + b*R ; R = b*L + a*R
    # k = ancho/100
    k = máx(50.0, mín(150.0, k_ancho)) / 100.0
    a = (1.0 + k) / 2.0
    b = (1,0 - k) / 2,0
    ancho_fx = f"pan=estéreo|c0={a:.6f}*c0+{b:.6f}*c1|c1={b:.6f}*c0+{a:.6f}*c1"

    # ✅ SAT (0..100): “densidad” segura sin asoftclip
    # Conducir -> comp suave -> atrás
    sat_p = máx(0.0, mín(100.0, k_sat)) / 100.0
    unidad_db = sat_p * 6.0
    back_db = -sat_p * 4.0
    sat_comp_thr = -14.0 + sat_p * 6.0
    relación sat_comp = 1,2 + sat_p * 2,8
    sat_fx = (
        f"volumen={unidad_db}dB,"
        f"acompresor=umbral={sat_comp_thr}dB:ratio={sat_comp_ratio}:ataque=2:liberación=80:recuperación=1,"
        f"volumen={back_db}dB"
    )

    # Producción
    salida_fx = f"volumen={k_salida}dB"

    # Limitador final
    limitador = "alimiter=limit=-1.0dB"

    devolver (
        f"{base_eq},"
        f"{eq_live},"
        f"acompressor=umbral={thr}dB:ratio={ratio}:ataque=12:liberación=120:recuperación={recuperación},"
        f"{pegamento_comp},"
        f"{ancho_fx},"
        f"{sat_fx},"
        f"{out_fx},"
        f"{limitador}"
    )


def build_preview_wav(master_id: str, segundos: int) -> Ruta:
    ruta_de_entrada = TMP_DIR / f"master_{master_id}.wav"
    si no in_path.exists():
        raise HTTPException(status_code=404, detail="Archivo no encontrado.")

    segundos = int(max(5, min(60, segundos)))
    ruta_anterior = TMP_DIR / f"vista_previa_{id_maestro}_{segundos}.wav"

    comando = [
        "ffmpeg", "-y",
        "-ocultar_banner",
        "-i", str(en_ruta),
        "-t", str(segundos),
        "-vn",
        "-ar", "44100",
        "-ac", "2",
        "-sample_fmt", "s16",
        str(ruta_anterior)
    ]
    ejecutar_cmd(cmd)

    si no prev_path.exists() o prev_path.stat().st_size < 1024:
        archivos_de_limpieza(ruta_anterior)
        generar HTTPException(código_de_estado=500, detalle="Vista previa vacía.")
    devolver ruta_anterior


def resolve_download_path(master_id: str) -> Ruta:
    m = maestros.get(master_id)
    si no m:
        raise HTTPException(status_code=404, detail="Master no encontrado.")

    calidad = normalizar_calidad(m.get("calidad"))
    si calidad == "GRATIS":
        # ✅ GRATIS: 30s
        devolver build_preview_wav(master_id, SEGUNDOS_DE_VISTA_PREVIA_GRATIS)

    # PLUS/PRO: completo
    ruta_de_salida = TMP_DIR / f"master_{master_id}.wav"
    si no out_path.exists():
        raise HTTPException(status_code=404, detail="Archivo no encontrado.")
    devolver ruta_de_salida


# =========================
# PUNTOS FINALES
# =========================
@app.get("/api/salud")
def salud():
    devuelve {"ok": Verdadero}


@app.get("/api/me")
defiéndeme():
    # beta: fijo. Luego lo haces real (auth/pagos)
    devolver {"plan": "GRATIS"}


@app.get("/api/masters")
definición lista_maestros():
    artículos = []
    para mid, m en masters.items():
        elementos.append({
            "id": medio,
            "título": m.get("título") o f"Master {mid}",
            "calidad": normalizar_calidad(m.get("calidad", "GRATIS")),
            "preestablecido": m.get("preestablecido", "limpio"),
            "intensidad": m.get("intensidad", 55),
            "creado_en": m.get("creado_en"),
        })
    items.sort(key=lambda x: str(x.get("created_at") o ""), reverse=True)
    artículos devueltos


# ✅ URL del panel (nuevo)
@app.get("/api/masters/{master_id}/stream")
def api_stream_master(id_maestro: str):
    devolver stream_master(master_id)


# ✅ URL del panel (nuevo) con aplicación GRATIS 30s
@app.get("/api/masters/{master_id}/descargar")
def api_download_master(id_maestro: str):
    devolver download_master(master_id)


# LEGACY (tu master.html actual puedes usar estos)
@app.get("/stream/{master_id}")
def stream_master(id_maestro: str):
    m = maestros.get(master_id)
    si no m:
        raise HTTPException(status_code=404, detail="Master no encontrado.")

    ruta_de_salida = TMP_DIR / f"master_{master_id}.wav"
    si no out_path.exists():
        raise HTTPException(status_code=404, detail="Archivo no encontrado.")

    devolver FileResponse(
        ruta=str(ruta_de_salida),
        tipo_de_medio="audio/wav",
        nombre de archivo="warmaster_master.wav",
        encabezados={"Disposición del contenido": 'en línea; nombre de archivo="warmaster_master.wav"'}
    )


@app.get("/descargar/{master_id}")
def download_master(master_id: str):
    dl_path = resolver_ruta_descarga(id_maestro)

    # nombre según plan
    m = masters.get(master_id) o {}
    q = normalizar_calidad(m.get("calidad"))
    fname = "warmaster_master.wav" si q está en ("PLUS", "PRO") de lo contrario f"warmaster_preview_{SEGUNDOS DE PREVISIÓN GRATUITA}s.wav"

    devolver FileResponse(
        ruta=str(dl_path),
        tipo_de_medio="audio/wav",
        nombre de archivo=fname
    )


@app.get("/api/master/vista previa")
def vista previa_master(
    master_id: str = Consulta(...),
    segundos: int = Consulta(30, ge=5, le=60),
):
    m = maestros.get(master_id)
    si no m:
        raise HTTPException(status_code=404, detail="Master no encontrado.")

    prev_path = build_preview_wav(master_id, segundos)

    devolver FileResponse(
        ruta=str(ruta_anterior),
        tipo_de_medio="audio/wav",
        nombre de archivo=f"warmaster_preview_{segundos}s.wav",
    )


@app.post("/api/master")
asíncrono def master(
    archivo: UploadFile = Archivo(...),
    preajuste: str = Form("clean"),
    intensidad: int = Form(55),

    # perillas
    k_low: float = Formulario(0.0),
    k_mid: float = Formulario(0.0),
    k_pres: float = Formulario(0.0),
    k_air: float = Formulario(0.0),
    k_glue: float = Formulario(0.0),
    k_width: float = Formulario(100.0),
    k_sat: float = Formulario(0.0),
    k_out: float = Formulario(0.0),

    # compatibilidad
    calidad_solicitada: Opcional[str] = Formulario(Ninguno),
    objetivo: Opcional[str] = Formulario(Ninguno),
):
    si no es archivo o no es archivo.nombre_archivo:
        elevar HTTPException(status_code=400, detalle="Archivo inválido.")

    id_maestro = uuid.uuid4().hex[:8]
    nombre = nombre_de_archivo_seguro(archivo.nombre_de_archivo)

    in_path = TMP_DIR / f"in_{master_id}_{name}"
    ruta_de_salida = TMP_DIR / f"master_{master_id}.wav"

    rq = normalizar_calidad(calidad_solicitada)

    maestros[master_id] = {
        "id": id_maestro,
        "título": nombre,
        "preestablecido": preestablecido,
        "intensidad": int(clamp_int(intensidad, 0, 100, 55)),
        "calidad": rq,
        "creado_en": datetime.utcnow().isoformat(),
        "perillas": {
            "bajo": k_bajo, "medio": k_medio, "pres": k_pres, "aire": k_aire,
            "pegamento": k_pegamento, "ancho": k_ancho, "sat": k_sat, "fuera": k_fuera
        }
    }

    # Guardar archivo
    intentar:
        con in_path.open("wb") como f:
            Shutil.copyfileobj(archivo.archivo, f)
    finalmente:
        intentar:
            archivo.archivo.close()
        excepto Excepción:
            aprobar

    # Validar tamaño
    si in_path.stat().st_size > TAMAÑO_MÁXIMO_DE_ARCHIVO_BYTES:
        archivos_de_limpieza(en_ruta)
        generar HTTPException(código_de_estado=400, detalle="Supera 100MB.")

    # Validar duración
    duración = obtener_duración_de_audio_segundos(en_ruta)
    si duración > MAX_DURATION_SECONDS:
        archivos_de_limpieza(en_ruta)
        generar HTTPException(status_code=400, detail="Supera 6 minutos.")

    # Perillas de sujeción
    k_bajo = abrazadera_flotante(k_bajo, -12, 12, 0.0)
    k_mid = abrazadera_flotante(k_mid, -12, 12, 0.0)
    k_pres = abrazadera_flotante(k_pres, -12, 12, 0.0)
    k_aire = abrazadera_flotante(k_aire, -12, 12, 0.0)
    k_pegamento = abrazadera_flotante(k_pegamento, 0, 100, 0.0)
    k_ancho = abrazadera_flotante(k_ancho, 50, 150, 100.0)
    k_sat = abrazadera_flotante(k_sat, 0, 100, 0.0)
    k_out = abrazadera_flotante(k_out, -12, 6, 0.0)

    filtros = cadena_predeterminada(
        preset=preestablecido,
        intensidad=intensidad,
        k_bajo=k_bajo, k_medio=k_medio, k_pres=k_pres, k_aire=k_aire,
        k_pegamento=k_pegamento, k_ancho=k_ancho, k_sat=k_sat, k_salida=k_salida
    )

    comando = [
        "ffmpeg", "-y",
        "-ocultar_banner",
        "-i", str(en_ruta),
        "-vn",
        "-af", filtros,
        "-ar", "44100",
        "-ac", "2",
        "-sample_fmt", "s16",
        str(ruta_de_salida)
    ]

    intentar:
        ejecutar_cmd(cmd)
    excepto Excepción:
        limpieza_archivos(ruta_de_entrada, ruta_de_salida)
        aumentar

    si no out_path.exists() o out_path.stat().st_size < 1024:
        limpieza_archivos(ruta_de_entrada, ruta_de_salida)
        generar HTTPException(código_de_estado=500, detalle="Master vacío.")

    archivos_de_limpieza(en_ruta)

    devolver FileResponse(
        ruta=str(ruta_de_salida),
        tipo_de_medio="audio/wav",
        nombre de archivo="warmaster_master.wav",
        encabezados={"X-Master-Id": master_id}
    )


@app.get("/")
definición raíz():
    devolver RedirectResponse(url="/index.html")


# =========================
# ARCHIVOS ESTÁTICOS
# =========================
# Monta /public en la raíz para servir index.html, master.html, Dashboard.html, Assets, etc.
# IMPORTANTE: el paréntesis faltante acá te estaba rompiendo el despliegue.
app.mount("/", StaticFiles(directorio=str(PUBLIC_DIR), html=True), nombre="público")
