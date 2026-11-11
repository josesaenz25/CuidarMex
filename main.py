import logging
import math
import os
import random
import unicodedata
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alcaldias import ALCALDIAS, coordenadas
from auth import (
    HASH_RELLENO,
    LimitadorIntentos,
    crear_token,
    hash_password,
    leer_token,
    secreto_definido,
    verificar_password,
)
from database import SessionLocal
from schemas import (
    ActivoUpdate,
    ApplicationCreate,
    CaregiverCreate,
    CaregiverUpdate,
    ConsentCreate,
    LoginCreate,
    RatingCreate,
    RegisterCreate,
    RequestCreate,
    TrainingCourseCreate,
)

logger = logging.getLogger("cuidarmex")

# =========================================================
# CONFIGURACIÓN (variables de entorno)
# =========================================================
# ENTORNO=produccion   -> apaga /docs y exige JWT_SECRET
# JWT_SECRET           -> clave larga y secreta para firmar las sesiones
# ALLOWED_ORIGINS      -> URL(s) de la app web separadas por coma
# ADMIN_EMAIL / ADMIN_PASSWORD -> crea el administrador la primera vez
EN_PRODUCCION = os.environ.get("ENTORNO", "desarrollo").lower() == "produccion"

ORIGENES = [
    o.strip()
    for o in os.environ.get("ALLOWED_ORIGINS", "*").split(",")
    if o.strip()
] or ["*"]

# Semáforo de cobertura: cuidadores registrados / cuidadores requeridos
COBERTURA_ROJO = 10      # menos de 10 %  -> Rojo
COBERTURA_AMARILLO = 50  # de 10 a 49 %   -> Amarillo; 50 % o más -> Verde

VELOCIDAD_KMH = 22  # velocidad media urbana para estimar minutos de traslado

# Caja que contiene a la CDMX (valida ubicaciones enviadas por el cliente)
LAT_MIN, LAT_MAX = 19.0, 19.65
LNG_MIN, LNG_MAX = -99.40, -98.90

limitador_login = LimitadorIntentos()


# =========================================================
# BASE DE DATOS Y UTILIDADES
# =========================================================

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def consulta(db: Session, sql: str, params: Optional[dict] = None) -> list:
    """Ejecuta un SELECT y devuelve una lista de diccionarios."""
    resultado = db.execute(text(sql), params or {})
    return [dict(r._mapping) for r in resultado]


def valor(db: Session, sql: str, params: Optional[dict] = None):
    """Ejecuta un SELECT y devuelve el primer valor (o None)."""
    return db.execute(text(sql), params or {}).scalar()


def auditar(db: Session, accion: str, entidad: str, entidad_id=None, usuario_id=None):
    """Bitácora: QUÉ pasó, sobre QUÉ id y QUIÉN. Nunca guarda datos personales."""
    db.execute(
        text("""
            INSERT INTO bitacora_auditoria (accion, entidad, entidad_id, usuario_id)
            VALUES (:accion, :entidad, :entidad_id, :usuario_id)
        """),
        {
            "accion": accion,
            "entidad": entidad,
            "entidad_id": entidad_id,
            "usuario_id": usuario_id,
        },
    )


def validar_alcaldia(alcaldia: str):
    if alcaldia not in ALCALDIAS:
        raise HTTPException(status_code=422, detail=f"Alcaldía no válida: {alcaldia}")


def clave_alcaldia(nombre) -> str:
    """Normaliza el nombre para cruzar tablas aunque cambien acentos o 'La'/'de Morelos'."""
    t = unicodedata.normalize("NFKD", str(nombre or ""))
    t = t.encode("ascii", "ignore").decode().lower().strip()
    t = t.replace(" de morelos", "")
    if t.startswith("la "):
        t = t[3:]
    return " ".join(t.split())


def haversine_km(lat1, lng1, lat2, lng2) -> float:
    r = 6371.0
    f1, f2 = math.radians(lat1), math.radians(lat2)
    df = f2 - f1
    dl = math.radians(lng2 - lng1)
    a = math.sin(df / 2) ** 2 + math.cos(f1) * math.cos(f2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def minutos_estimados(km: float) -> int:
    return max(3, round(km / VELOCIDAD_KMH * 60))


def punto_valido(lat, lng) -> bool:
    return LAT_MIN <= lat <= LAT_MAX and LNG_MIN <= lng <= LNG_MAX


def a_float(x) -> Optional[float]:
    return None if x is None else float(x)


# ---------- Enmascarado de datos del paciente ----------

_PARTICULAS = {"de", "del", "la", "las", "los", "y", "e"}


def _palabras(nombre) -> list:
    return [p for p in str(nombre or "").strip().split() if p]


def iniciales(nombre) -> str:
    """'María Flores González' -> 'M. F. G.'"""
    letras = [
        p[0].upper() + "."
        for p in _palabras(nombre)
        if p.lower() not in _PARTICULAS
    ]
    return " ".join(letras) or "Paciente"


def nombre_corto(nombre) -> str:
    """'María Flores González' -> 'María F.'"""
    palabras = [p for p in _palabras(nombre) if p.lower() not in _PARTICULAS]
    if not palabras:
        return "Paciente"
    primero = palabras[0].capitalize()
    if len(palabras) == 1:
        return primero
    return f"{primero} {palabras[1][0].upper()}."


def rango_edad(edad) -> str:
    """72 -> '70-79'. Sin unidad: la pantalla agrega 'años'."""
    try:
        e = int(edad)
    except (TypeError, ValueError):
        return "No especificada"
    if e < 18:
        return "Menor de 18"
    if e < 20:
        return "18-19"
    base = (e // 10) * 10
    return f"{base}-{base + 9}"


# =========================================================
# SESIÓN Y PERMISOS
# =========================================================

bearer = HTTPBearer(auto_error=False)


def usuario_actual(
    cred: Optional[HTTPAuthorizationCredentials] = Depends(bearer),
    db: Session = Depends(get_db),
) -> dict:
    if cred is None:
        raise HTTPException(
            status_code=401,
            detail="Inicia sesión para continuar",
            headers={"WWW-Authenticate": "Bearer"},
        )

    datos = leer_token(cred.credentials)
    if not datos:
        raise HTTPException(
            status_code=401,
            detail="Tu sesión expiró. Inicia sesión de nuevo",
            headers={"WWW-Authenticate": "Bearer"},
        )

    fila = consulta(db, """
        SELECT u.id, u.email, u.nombre, u.rol, u.activo, c.id AS caregiver_id
        FROM users u
        LEFT JOIN caregivers c ON c.user_id = u.id
        WHERE u.id = :id
    """, {"id": datos["sub"]})

    if not fila or not fila[0]["activo"]:
        raise HTTPException(
            status_code=401,
            detail="Tu sesión expiró. Inicia sesión de nuevo",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return fila[0]


def requiere(*roles: str):
    """Dependencia: exige sesión y que el rol esté entre los permitidos."""

    def comprobar(u: dict = Depends(usuario_actual)) -> dict:
        if u["rol"] not in roles:
            raise HTTPException(
                status_code=403, detail="No tienes permiso para esta acción"
            )
        return u

    return comprobar


def cuidador_efectivo(u: dict, pedido: Optional[int], obligatorio: bool = False):
    """Un cuidador solo puede actuar como él mismo. El administrador elige."""
    if u["rol"] == "cuidador":
        if not u["caregiver_id"]:
            raise HTTPException(
                status_code=409, detail="Primero completa tu perfil de cuidador"
            )
        if pedido is not None and pedido != u["caregiver_id"]:
            raise HTTPException(
                status_code=403, detail="No tienes permiso sobre ese cuidador"
            )
        return u["caregiver_id"]

    if obligatorio and pedido is None:
        raise HTTPException(status_code=422, detail="Falta caregiver_id")
    return pedido


def usuario_publico(u: dict) -> dict:
    return {
        "id": u["id"],
        "email": u["email"],
        "nombre": u["nombre"],
        "rol": u["rol"],
        "caregiver_id": u.get("caregiver_id"),
    }


def _crear_admin_inicial():
    correo = os.environ.get("ADMIN_EMAIL", "").strip().lower()
    clave = os.environ.get("ADMIN_PASSWORD", "")
    if not correo or not clave:
        return
    if len(clave) < 10:
        logger.warning("ADMIN_PASSWORD debe tener al menos 10 caracteres; no se creó el administrador")
        return

    db = SessionLocal()
    try:
        existe = valor(db, "SELECT id FROM users WHERE lower(email) = :e", {"e": correo})
        if existe is None:
            nuevo = db.execute(
                text("""
                    INSERT INTO users (email, password_hash, nombre, rol)
                    VALUES (:e, :h, 'Administrador', 'admin')
                    RETURNING id
                """),
                {"e": correo, "h": hash_password(clave)},
            ).scalar()
            auditar(db, "CREAR_ADMIN", "users", nuevo, nuevo)
            db.commit()
            logger.info("Administrador inicial creado")
    finally:
        db.close()


@asynccontextmanager
async def ciclo_de_vida(app: FastAPI):
    if EN_PRODUCCION and not secreto_definido():
        raise RuntimeError("Falta la variable de entorno JWT_SECRET")
    try:
        _crear_admin_inicial()
    except Exception:
        logger.exception("No se pudo crear el administrador inicial (¿ya corriste la migración?)")
    yield


app = FastAPI(
    lifespan=ciclo_de_vida,
    docs_url=None if EN_PRODUCCION else "/docs",
    redoc_url=None,
    openapi_url=None if EN_PRODUCCION else "/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ORIGENES,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def cabeceras_de_seguridad(request: Request, call_next):
    """Evita cachés con datos de pacientes y endurece el navegador."""
    respuesta = await call_next(request)
    respuesta.headers["Cache-Control"] = "no-store"
    respuesta.headers["X-Content-Type-Options"] = "nosniff"
    respuesta.headers["Referrer-Policy"] = "no-referrer"
    respuesta.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return respuesta


@app.exception_handler(Exception)
async def error_generico(request: Request, exc: Exception):
    """Nunca se devuelven trazas ni detalles internos al cliente."""
    logger.exception("Error no controlado en %s", request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Error interno del servidor"})


# =========================================================
# PÚBLICO: inicio y salud
# =========================================================

@app.get("/")
def home():
    return {"mensaje": "Bienvenido a CuidarMéx"}


@app.get("/health")
def health(db: Session = Depends(get_db)):
    """Para monitoreo: confirma que la API y la base de datos responden."""
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        raise HTTPException(status_code=503, detail="Base de datos no disponible")
    return {"estado": "ok", "base_de_datos": "conectada"}


# =========================================================
# AUTENTICACIÓN
# =========================================================

def _respuesta_sesion(user_id, email, nombre, rol, caregiver_id):
    return {
        "token": crear_token(user_id, rol),
        "usuario": {
            "id": user_id,
            "email": email,
            "nombre": nombre,
            "rol": rol,
            "caregiver_id": caregiver_id,
        },
    }


@app.post("/auth/register")
def registrar(datos: RegisterCreate, db: Session = Depends(get_db)):
    if datos.rol not in ("familia", "cuidador"):
        raise HTTPException(status_code=422, detail="Rol no válido")
    if not datos.acepta_aviso:
        raise HTTPException(
            status_code=422, detail="Debes aceptar el aviso de privacidad"
        )
    if datos.rol == "familia" and not datos.acepta_datos_sensibles:
        raise HTTPException(
            status_code=422,
            detail="Debes autorizar el tratamiento de los datos de salud para solicitar cuidado",
        )

    correo = datos.email.strip().lower()
    if valor(db, "SELECT id FROM users WHERE lower(email) = :e", {"e": correo}) is not None:
        raise HTTPException(status_code=409, detail="Ese correo ya está registrado")

    try:
        nuevo_id = db.execute(
            text("""
                INSERT INTO users (email, password_hash, nombre, rol)
                VALUES (:e, :h, :n, :r)
                RETURNING id
            """),
            {
                "e": correo,
                "h": hash_password(datos.password),
                "n": datos.nombre.strip(),
                "r": datos.rol,
            },
        ).scalar()

        db.execute(
            text("""
                INSERT INTO consentimientos (rol, version_aviso, datos_sensibles, user_id)
                VALUES (:rol, :version, :sensibles, :uid)
            """),
            {
                "rol": "paciente" if datos.rol == "familia" else "cuidador",
                "version": datos.version_aviso,
                "sensibles": datos.rol == "familia" and datos.acepta_datos_sensibles,
                "uid": nuevo_id,
            },
        )
        auditar(db, "REGISTRO", "users", nuevo_id, nuevo_id)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Ese correo ya está registrado")

    return _respuesta_sesion(nuevo_id, correo, datos.nombre.strip(), datos.rol, None)


@app.post("/auth/login")
def iniciar_sesion(datos: LoginCreate, db: Session = Depends(get_db)):
    correo = datos.email.strip().lower()

    espera = limitador_login.segundos_de_bloqueo(correo)
    if espera:
        raise HTTPException(
            status_code=429,
            detail=f"Demasiados intentos. Intenta de nuevo en {math.ceil(espera / 60)} min",
            headers={"Retry-After": str(espera)},
        )

    fila = consulta(db, """
        SELECT u.id, u.email, u.nombre, u.rol, u.activo, u.password_hash,
               c.id AS caregiver_id
        FROM users u
        LEFT JOIN caregivers c ON c.user_id = u.id
        WHERE lower(u.email) = :e
    """, {"e": correo})

    usuario = fila[0] if fila else None
    # Se verifica siempre, aunque el correo no exista, para no revelar cuentas.
    password_ok = verificar_password(
        datos.password, usuario["password_hash"] if usuario else HASH_RELLENO
    )

    if not usuario or not password_ok or not usuario["activo"]:
        limitador_login.registrar_fallo(correo)
        auditar(db, "LOGIN_FALLIDO", "users", usuario["id"] if usuario else None, None)
        db.commit()
        raise HTTPException(status_code=401, detail="Correo o contraseña incorrectos")

    limitador_login.limpiar(correo)
    db.execute(
        text("UPDATE users SET ultimo_acceso = NOW() WHERE id = :id"),
        {"id": usuario["id"]},
    )
    auditar(db, "LOGIN", "users", usuario["id"], usuario["id"])
    db.commit()

    return _respuesta_sesion(
        usuario["id"], usuario["email"], usuario["nombre"], usuario["rol"],
        usuario["caregiver_id"],
    )


@app.get("/auth/me")
def quien_soy(u: dict = Depends(usuario_actual)):
    return usuario_publico(u)


@app.post("/consent")
def registrar_consentimiento(
    c: ConsentCreate,
    u: dict = Depends(usuario_actual),
    db: Session = Depends(get_db),
):
    """Vuelve a registrar la aceptación (p. ej. cuando cambie el aviso)."""
    if u["rol"] == "admin":
        raise HTTPException(status_code=422, detail="El administrador no firma consentimientos")

    nuevo_id = db.execute(
        text("""
            INSERT INTO consentimientos (rol, version_aviso, datos_sensibles, user_id)
            VALUES (:rol, :version, :sensibles, :uid)
            RETURNING id
        """),
        {
            "rol": "paciente" if u["rol"] == "familia" else "cuidador",
            "version": c.version,
            "sensibles": bool(c.datos_sensibles) and u["rol"] == "familia",
            "uid": u["id"],
        },
    ).scalar()
    auditar(db, "CONSENTIMIENTO", "consentimientos", nuevo_id, u["id"])
    db.commit()

    return {"mensaje": "Consentimiento registrado", "id": nuevo_id}


# =========================================================
# CUIDADORES
# =========================================================

@app.get("/caregivers")
def obtener_cuidadores(
    q: Optional[str] = Query(None, max_length=60),
    alcaldia: Optional[str] = Query(None, max_length=80),
    especialidad: Optional[str] = Query(None, max_length=100),
    orden: str = Query("calificacion", pattern="^(calificacion|costo|nombre)$"),
    limite: int = Query(30, ge=1, le=100),
    desde: int = Query(0, ge=0, le=100000),
    u: dict = Depends(requiere("familia", "admin")),
    db: Session = Depends(get_db),
):
    """Listado con búsqueda, filtros y paginación (hay cientos de cuidadores)."""
    condiciones, params = [], {"limite": limite, "desde": desde}
    if q and q.strip():
        buscado = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        condiciones.append("nombre ILIKE :q ESCAPE '\\'")
        params["q"] = f"%{buscado}%"
    if alcaldia:
        condiciones.append("alcaldia = :alcaldia")
        params["alcaldia"] = alcaldia
    if especialidad:
        condiciones.append("especialidad = :especialidad")
        params["especialidad"] = especialidad
    donde = ("WHERE " + " AND ".join(condiciones)) if condiciones else ""

    ordenes = {
        "calificacion": "(COALESCE(total_resenas, 0) > 0) DESC, calificacion DESC, "
                        "COALESCE(total_resenas, 0) DESC, id",
        "costo": "costo_hora ASC, id",
        "nombre": "nombre ASC, id",
    }

    total = valor(db, f"SELECT COUNT(*) FROM caregivers {donde}", params)
    datos = consulta(db, f"""
        SELECT
            id,
            nombre,
            experiencia,
            costo_hora,
            especialidad,
            calificacion,
            COALESCE(total_resenas, 0) AS total_resenas,
            alcaldia
        FROM caregivers
        {donde}
        ORDER BY {ordenes[orden]}
        LIMIT :limite OFFSET :desde
    """, params)

    for d in datos:
        d["costo_hora"] = float(d["costo_hora"] or 0)
        d["calificacion"] = float(d["calificacion"] or 0)

    return {"total": total, "datos": datos}


@app.post("/caregivers")
def crear_cuidador(
    caregiver: CaregiverCreate,
    u: dict = Depends(requiere("cuidador", "admin")),
    db: Session = Depends(get_db),
):
    validar_alcaldia(caregiver.alcaldia)

    if u["rol"] == "cuidador" and u["caregiver_id"]:
        raise HTTPException(status_code=409, detail="Ya tienes un perfil de cuidador")

    lat, lng = coordenadas(caregiver.alcaldia)
    lat = float(lat) + random.uniform(-0.02, 0.02)
    lng = float(lng) + random.uniform(-0.02, 0.02)

    nuevo_id = db.execute(
        text("""
            INSERT INTO caregivers
                (nombre, experiencia, costo_hora, especialidad,
                 alcaldia, latitud, longitud, user_id, ubicacion_ajustada)
            VALUES
                (:nombre, :experiencia, :costo_hora, :especialidad,
                 :alcaldia, :latitud, :longitud, :user_id, TRUE)
            RETURNING id
        """),
        {
            "nombre": caregiver.nombre.strip(),
            "experiencia": caregiver.experiencia.strip(),
            "costo_hora": caregiver.costo_hora,
            "especialidad": caregiver.especialidad,
            "alcaldia": caregiver.alcaldia,
            "latitud": lat,
            "longitud": lng,
            "user_id": u["id"] if u["rol"] == "cuidador" else None,
        },
    ).scalar()
    auditar(db, "CREAR", "caregivers", nuevo_id, u["id"])
    db.commit()

    return {"mensaje": "Perfil guardado correctamente", "id": nuevo_id}


@app.get("/caregivers/me")
def mi_perfil_cuidador(
    u: dict = Depends(requiere("cuidador")),
    db: Session = Depends(get_db),
):
    if not u["caregiver_id"]:
        return {"perfil": None}
    fila = consulta(db, """
        SELECT id, nombre, experiencia, costo_hora, especialidad, alcaldia,
               calificacion, COALESCE(total_resenas, 0) AS total_resenas
        FROM caregivers WHERE id = :id
    """, {"id": u["caregiver_id"]})[0]
    fila["costo_hora"] = float(fila["costo_hora"] or 0)
    fila["calificacion"] = float(fila["calificacion"] or 0)
    return {"perfil": fila}


@app.get("/nearby-caregivers")
def cuidadores_cercanos(
    lat: Optional[float] = None,
    lng: Optional[float] = None,
    alcaldia: Optional[str] = None,
    servicio: Optional[str] = None,
    limite: int = Query(20, ge=1, le=50),
    u: dict = Depends(requiere("familia", "admin")),
    db: Session = Depends(get_db),
):
    """Estilo Uber/Didi: cuidadores ordenados por distancia al paciente."""
    if lat is not None and lng is not None:
        if not punto_valido(lat, lng):
            raise HTTPException(status_code=422, detail="La ubicación está fuera de la CDMX")
        origen = (lat, lng)
    elif alcaldia:
        validar_alcaldia(alcaldia)
        a, b = coordenadas(alcaldia)
        origen = (float(a), float(b))
    else:
        raise HTTPException(status_code=422, detail="Indica tu ubicación o tu alcaldía")

    filas = consulta(db, """
        SELECT
            c.id, c.nombre, c.especialidad, c.experiencia, c.costo_hora,
            c.calificacion, COALESCE(c.total_resenas, 0) AS total_resenas,
            c.alcaldia, c.latitud, c.longitud,
            NOT EXISTS (
                SELECT 1 FROM requests r
                WHERE r.caregiver_id = c.id AND r.status = 'Asignada'
            ) AS disponible
        FROM caregivers c
        WHERE c.latitud IS NOT NULL AND c.longitud IS NOT NULL
    """)

    resultado = []
    for f in filas:
        clat, clng = float(f["latitud"]), float(f["longitud"])
        km = haversine_km(origen[0], origen[1], clat, clng)
        resultado.append({
            "id": f["id"],
            "nombre": f["nombre"],
            "especialidad": f["especialidad"],
            "experiencia": f["experiencia"],
            "costo_hora": float(f["costo_hora"] or 0),
            "calificacion": float(f["calificacion"] or 0),
            "total_resenas": f["total_resenas"],
            "alcaldia": f["alcaldia"],
            # Posición aproximada (~100 m), nunca el punto exacto.
            "latitud": round(clat, 3),
            "longitud": round(clng, 3),
            "distancia_km": round(km, 1),
            "minutos": minutos_estimados(km),
            "disponible": f["disponible"],
            "compatible": bool(servicio) and f["especialidad"] == servicio,
        })

    resultado.sort(key=lambda x: (x["distancia_km"], -x["calificacion"]))

    return {
        "origen": {"latitud": round(origen[0], 5), "longitud": round(origen[1], 5)},
        "datos": resultado[:limite],
    }


# =========================================================
# SOLICITUDES (pacientes)
# =========================================================

@app.post("/requests")
def crear_solicitud(
    request: RequestCreate,
    u: dict = Depends(requiere("familia", "admin")),
    db: Session = Depends(get_db),
):
    validar_alcaldia(request.alcaldia)

    if (request.latitud is None) != (request.longitud is None):
        raise HTTPException(status_code=422, detail="Envía latitud y longitud juntas")

    if request.latitud is not None:
        if not punto_valido(request.latitud, request.longitud):
            raise HTTPException(status_code=422, detail="La ubicación está fuera de la CDMX")
        lat, lng = request.latitud, request.longitud
    else:
        a, b = coordenadas(request.alcaldia)
        lat = float(a) + random.uniform(-0.01, 0.01)
        lng = float(b) + random.uniform(-0.01, 0.01)

    if request.cuidador_preferido_id is not None:
        existe = valor(
            db, "SELECT id FROM caregivers WHERE id = :id",
            {"id": request.cuidador_preferido_id},
        )
        if existe is None:
            raise HTTPException(status_code=404, detail="Cuidador no encontrado")

    nuevo_id = db.execute(
        text("""
            INSERT INTO requests
                (service_type, alcaldia, paciente, edad, prioridad, estado_salud,
                 status, latitud, longitud, user_id, cuidador_preferido_id,
                 ubicacion_ajustada)
            VALUES
                (:service_type, :alcaldia, :paciente, :edad, :prioridad, :estado_salud,
                 'Pendiente', :latitud, :longitud, :user_id, :preferido, TRUE)
            RETURNING id
        """),
        {
            "service_type": request.service_type,
            "alcaldia": request.alcaldia,
            "paciente": request.paciente.strip(),
            "edad": request.edad,
            "prioridad": request.prioridad,
            "estado_salud": request.estado_salud,
            "latitud": lat,
            "longitud": lng,
            "user_id": u["id"],
            "preferido": request.cuidador_preferido_id,
        },
    ).scalar()
    auditar(db, "CREAR", "requests", nuevo_id, u["id"])
    db.commit()

    return {"mensaje": "Solicitud creada correctamente", "id": nuevo_id}


@app.get("/requests")
def obtener_solicitudes(
    u: dict = Depends(requiere("familia", "cuidador", "admin")),
    db: Session = Depends(get_db),
):
    """Cada quien ve SOLO lo suyo. Nunca incluye el nombre del paciente."""
    params = {}
    if u["rol"] == "familia":
        filtro, params = "WHERE r.user_id = :uid", {"uid": u["id"]}
    elif u["rol"] == "cuidador":
        filtro, params = "WHERE r.caregiver_id = :cid", {"cid": u["caregiver_id"]}
    else:
        filtro = ""

    filas = consulta(db, f"""
        SELECT
            r.id,
            COALESCE(c.nombre, 'Sin asignar') AS cuidador,
            r.service_type AS servicio,
            r.status AS estado,
            r.request_date AS fecha,
            r.alcaldia,
            r.caregiver_id,
            sr.estrellas,
            r.latitud AS r_lat, r.longitud AS r_lng,
            c.latitud AS c_lat, c.longitud AS c_lng
        FROM requests r
        LEFT JOIN caregivers c ON r.caregiver_id = c.id
        LEFT JOIN service_ratings sr ON sr.request_id = r.id
        {filtro}
        ORDER BY r.id DESC
        LIMIT 200
    """, params)

    datos = []
    for f in filas:
        km = minutos = None
        if f["estado"] == "Asignada" and None not in (f["r_lat"], f["r_lng"], f["c_lat"], f["c_lng"]):
            km = round(haversine_km(
                float(f["r_lat"]), float(f["r_lng"]), float(f["c_lat"]), float(f["c_lng"])
            ), 1)
            minutos = minutos_estimados(km)

        datos.append({
            "id": f["id"],
            "cuidador": f["cuidador"],
            "servicio": f["servicio"],
            "estado": f["estado"],
            "fecha": str(f["fecha"]),
            "alcaldia": f["alcaldia"],
            "caregiver_id": f["caregiver_id"],
            "calificada": f["estrellas"] is not None,
            "estrellas": f["estrellas"] or 0,
            "distancia_km": km,
            "minutos": minutos,
        })

    return {"datos": datos}


@app.post("/ratings")
def calificar_servicio(
    rating: RatingCreate,
    u: dict = Depends(requiere("familia", "admin")),
    db: Session = Depends(get_db),
):
    solicitud = consulta(
        db,
        "SELECT caregiver_id, status, user_id FROM requests WHERE id = :id",
        {"id": rating.request_id},
    )
    # Una familia solo califica sus propios servicios (si no, "no existe").
    if not solicitud or (u["rol"] == "familia" and solicitud[0]["user_id"] != u["id"]):
        raise HTTPException(status_code=404, detail="Solicitud no encontrada")

    caregiver_id = solicitud[0]["caregiver_id"]
    if caregiver_id is None:
        raise HTTPException(
            status_code=409, detail="La solicitud no tiene cuidador asignado"
        )
    if solicitud[0]["status"] != "Completado":
        raise HTTPException(
            status_code=409,
            detail="Solo se pueden calificar servicios completados",
        )

    ya_calificada = valor(
        db,
        "SELECT id FROM service_ratings WHERE request_id = :id",
        {"id": rating.request_id},
    )
    if ya_calificada is not None:
        raise HTTPException(status_code=409, detail="Este servicio ya fue calificado")

    db.execute(
        text("""
            INSERT INTO service_ratings
                (request_id, caregiver_id, estrellas, comentario)
            VALUES (:request_id, :caregiver_id, :estrellas, :comentario)
        """),
        {
            "request_id": rating.request_id,
            "caregiver_id": caregiver_id,
            "estrellas": rating.estrellas,
            "comentario": rating.comentario,
        },
    )

    resumen = consulta(
        db,
        """
        SELECT ROUND(AVG(estrellas)::numeric, 1) AS promedio, COUNT(*) AS total
        FROM service_ratings
        WHERE caregiver_id = :caregiver_id
        """,
        {"caregiver_id": caregiver_id},
    )[0]

    db.execute(
        text("""
            UPDATE caregivers
            SET calificacion = :promedio, total_resenas = :total
            WHERE id = :caregiver_id
        """),
        {
            "promedio": resumen["promedio"],
            "total": resumen["total"],
            "caregiver_id": caregiver_id,
        },
    )
    auditar(db, "CALIFICAR", "requests", rating.request_id, u["id"])
    db.commit()

    return {
        "mensaje": "Calificación registrada",
        "calificacion": float(resumen["promedio"]),
        "total_resenas": resumen["total"],
    }


# =========================================================
# DASHBOARD (agregados: sin datos individuales)
# =========================================================

@app.get("/dashboard")
def dashboard(u: dict = Depends(usuario_actual), db: Session = Depends(get_db)):
    servicio = consulta(db, """
        SELECT service_type AS servicio, COUNT(*) AS cantidad
        FROM requests
        GROUP BY service_type
        ORDER BY cantidad DESC, service_type
        LIMIT 1
    """)

    cuidador = consulta(db, """
        SELECT c.nombre, COUNT(*) AS cantidad
        FROM requests r
        INNER JOIN caregivers c ON r.caregiver_id = c.id
        GROUP BY c.id, c.nombre
        ORDER BY cantidad DESC, c.nombre
        LIMIT 1
    """)

    return {
        "total_cuidadores": valor(db, "SELECT COUNT(*) FROM caregivers"),
        "total_solicitudes": valor(db, "SELECT COUNT(*) FROM requests"),
        "pendientes": valor(db, "SELECT COUNT(*) FROM requests WHERE status = 'Pendiente'"),
        "completadas": valor(db, "SELECT COUNT(*) FROM requests WHERE status = 'Completado'"),
        "servicio_popular": servicio[0]["servicio"] if servicio else "Sin datos",
        "cuidador_popular": (
            nombre_corto(cuidador[0]["nombre"]) if cuidador else "Sin datos"
        ),
    }


@app.get("/estadisticas-servicios")
def estadisticas_servicios(u: dict = Depends(usuario_actual), db: Session = Depends(get_db)):
    datos = consulta(db, """
        SELECT service_type AS servicio, COUNT(*) AS cantidad
        FROM requests
        GROUP BY service_type
        ORDER BY cantidad DESC, service_type
    """)
    return {"datos": datos}


@app.get("/estadisticas-estados")
def estadisticas_estados(u: dict = Depends(usuario_actual), db: Session = Depends(get_db)):
    datos = consulta(db, """
        SELECT status AS estado, COUNT(*) AS cantidad
        FROM requests
        GROUP BY status
    """)
    return {"datos": datos}


@app.get("/tendencia-solicitudes")
def tendencia_solicitudes(u: dict = Depends(usuario_actual), db: Session = Depends(get_db)):
    datos = consulta(db, """
        SELECT DATE(request_date) AS fecha, COUNT(*) AS cantidad
        FROM requests
        WHERE request_date IS NOT NULL
        GROUP BY DATE(request_date)
        ORDER BY DATE(request_date)
    """)
    for d in datos:
        d["fecha"] = str(d["fecha"])
    return {"datos": datos}


@app.get("/dashboard-ejecutivo")
def dashboard_ejecutivo(u: dict = Depends(usuario_actual), db: Session = Depends(get_db)):
    solicitudes = valor(db, "SELECT COUNT(*) FROM requests")
    completadas = valor(db, "SELECT COUNT(*) FROM requests WHERE status = 'Completado'")
    tasa_exito = round(completadas / solicitudes * 100, 2) if solicitudes else 0

    return {
        "cuidadores": valor(db, "SELECT COUNT(*) FROM caregivers"),
        "solicitudes": solicitudes,
        "postulaciones": valor(db, "SELECT COUNT(*) FROM applications"),
        "certificaciones": valor(
            db, "SELECT COUNT(*) FROM certifications WHERE certificado_generado = TRUE"
        ),
        "asignadas": valor(db, "SELECT COUNT(*) FROM requests WHERE status = 'Asignada'"),
        "completadas": completadas,
        "tasa_exito": tasa_exito,
    }


# =========================================================
# CAPACITACIÓN (cada cuidador solo ve y completa lo suyo)
# =========================================================

@app.get("/training-courses")
def obtener_cursos(
    caregiver_id: Optional[int] = None,
    u: dict = Depends(requiere("cuidador", "admin")),
    db: Session = Depends(get_db),
):
    cid = cuidador_efectivo(u, caregiver_id)
    datos = consulta(db, """
        SELECT
            tc.id, tc.nombre, tc.categoria, tc.descripcion,
            tc.duracion_horas, tc.modalidad, tc.activo,
            EXISTS (
                SELECT 1 FROM training_progress tp
                WHERE tp.course_id = tc.id
                  AND tp.caregiver_id = :caregiver_id
                  AND tp.completado = TRUE
            ) AS completado
        FROM training_courses tc
        ORDER BY tc.id
    """, {"caregiver_id": cid})
    return {"datos": datos}


@app.post("/training-courses")
def crear_curso(
    curso: TrainingCourseCreate,
    u: dict = Depends(requiere("admin")),
    db: Session = Depends(get_db),
):
    db.execute(
        text("""
            INSERT INTO training_courses
                (nombre, categoria, descripcion, duracion_horas, modalidad)
            VALUES (:nombre, :categoria, :descripcion, :duracion_horas, :modalidad)
        """),
        curso.model_dump(),
    )
    auditar(db, "CREAR", "training_courses", None, u["id"])
    db.commit()
    return {"mensaje": "Curso creado correctamente"}


@app.get("/training-progress")
def training_progress(
    caregiver_id: Optional[int] = None,
    u: dict = Depends(requiere("cuidador", "admin")),
    db: Session = Depends(get_db),
):
    cid = cuidador_efectivo(u, caregiver_id)
    total = valor(db, "SELECT COUNT(*) FROM training_courses")

    if cid is None:
        completados = valor(db, "SELECT COUNT(*) FROM training_progress WHERE completado = TRUE")
    else:
        completados = valor(
            db,
            """
            SELECT COUNT(DISTINCT course_id) FROM training_progress
            WHERE completado = TRUE AND caregiver_id = :cid
            """,
            {"cid": cid},
        )

    porcentaje = round(completados / total * 100, 2) if total else 0
    return {"total": total, "completados": completados, "porcentaje": porcentaje}


@app.get("/certifications")
def obtener_certificaciones(
    caregiver_id: Optional[int] = None,
    u: dict = Depends(requiere("cuidador", "admin")),
    db: Session = Depends(get_db),
):
    cid = cuidador_efectivo(u, caregiver_id)
    filtro = "AND c.caregiver_id = :cid" if cid is not None else ""
    params = {"cid": cid} if cid is not None else {}

    datos = consulta(db, f"""
        SELECT c.id, tc.nombre AS curso, c.fecha_generacion AS fecha
        FROM certifications c
        INNER JOIN training_courses tc ON c.course_id = tc.id
        WHERE c.certificado_generado = TRUE
        {filtro}
        ORDER BY c.id DESC
    """, params)

    for d in datos:
        d["fecha"] = str(d["fecha"])
    return {"datos": datos}


@app.post("/complete-course")
def completar_curso(
    course_id: int,
    caregiver_id: Optional[int] = None,
    u: dict = Depends(requiere("cuidador", "admin")),
    db: Session = Depends(get_db),
):
    cid = cuidador_efectivo(u, caregiver_id, obligatorio=True)

    if valor(db, "SELECT id FROM caregivers WHERE id = :id", {"id": cid}) is None:
        raise HTTPException(status_code=404, detail="Cuidador no encontrado")

    curso = consulta(db, "SELECT activo FROM training_courses WHERE id = :id", {"id": course_id})
    if not curso:
        raise HTTPException(status_code=404, detail="Curso no encontrado")
    if curso[0]["activo"] is False:
        raise HTTPException(status_code=409, detail="El curso no está disponible")

    ya = valor(
        db,
        """
        SELECT id FROM training_progress
        WHERE caregiver_id = :c AND course_id = :k AND completado = TRUE
        """,
        {"c": cid, "k": course_id},
    )
    if ya is not None:
        raise HTTPException(status_code=409, detail="Ya completaste este curso")

    existente = valor(
        db,
        "SELECT id FROM training_progress WHERE caregiver_id = :c AND course_id = :k",
        {"c": cid, "k": course_id},
    )
    if existente is None:
        db.execute(
            text("""
                INSERT INTO training_progress
                    (caregiver_id, course_id, completado, fecha_completado)
                VALUES (:c, :k, TRUE, NOW())
            """),
            {"c": cid, "k": course_id},
        )
    else:
        db.execute(
            text("""
                UPDATE training_progress
                SET completado = TRUE, fecha_completado = NOW()
                WHERE id = :id
            """),
            {"id": existente},
        )

    certificada = valor(
        db,
        "SELECT id FROM certifications WHERE caregiver_id = :c AND course_id = :k",
        {"c": cid, "k": course_id},
    )
    if certificada is None:
        db.execute(
            text("""
                INSERT INTO certifications
                    (caregiver_id, course_id, certificado_generado, fecha_generacion)
                VALUES (:c, :k, TRUE, NOW())
            """),
            {"c": cid, "k": course_id},
        )

    auditar(db, "COMPLETAR_CURSO", "training_courses", course_id, u["id"])
    db.commit()
    return {"mensaje": "Curso completado y certificación generada"}


# =========================================================
# OPORTUNIDADES, POSTULACIONES, ASIGNACIÓN Y CIERRE
# =========================================================

@app.get("/opportunities")
def obtener_oportunidades(
    u: dict = Depends(requiere("cuidador", "admin")),
    db: Session = Depends(get_db),
):
    """Solicitudes pendientes, con datos del paciente ENMASCARADOS."""
    cid = u["caregiver_id"]

    mi_pos = None
    if cid is not None:
        fila = consulta(
            db, "SELECT latitud, longitud FROM caregivers WHERE id = :id", {"id": cid}
        )
        if fila and fila[0]["latitud"] is not None:
            mi_pos = (float(fila[0]["latitud"]), float(fila[0]["longitud"]))

    filas = consulta(db, """
        SELECT
            r.id, r.paciente, r.edad, r.prioridad, r.estado_salud,
            r.service_type AS servicio, r.alcaldia, r.status AS estado,
            r.latitud, r.longitud, r.cuidador_preferido_id,
            EXISTS (
                SELECT 1 FROM applications a
                WHERE a.request_id = r.id AND a.caregiver_id = :cid
            ) AS ya_postulado
        FROM requests r
        WHERE r.status = 'Pendiente'
        ORDER BY r.id DESC
        LIMIT 200
    """, {"cid": cid})

    datos = []
    for f in filas:
        km = minutos = None
        if mi_pos and f["latitud"] is not None:
            km = round(haversine_km(mi_pos[0], mi_pos[1], float(f["latitud"]), float(f["longitud"])), 1)
            minutos = minutos_estimados(km)

        datos.append({
            "id": f["id"],
            "paciente": iniciales(f["paciente"]),
            "edad": rango_edad(f["edad"]),
            "prioridad": f["prioridad"],
            "estado_salud": "Se comparte al ser asignado",
            "servicio": f["servicio"],
            "alcaldia": f["alcaldia"],
            "estado": f["estado"],
            "distancia_km": km,
            "minutos": minutos,
            "dirigida_a_mi": cid is not None and f["cuidador_preferido_id"] == cid,
            "ya_postulado": f["ya_postulado"],
        })

    # Primero las dirigidas a mí, luego las más cercanas.
    datos.sort(key=lambda d: (
        not d["dirigida_a_mi"],
        d["distancia_km"] if d["distancia_km"] is not None else 9999,
        -d["id"],
    ))
    return {"datos": datos}


@app.post("/apply")
def postularse(
    application: ApplicationCreate,
    u: dict = Depends(requiere("cuidador", "admin")),
    db: Session = Depends(get_db),
):
    cid = cuidador_efectivo(u, application.caregiver_id, obligatorio=True)

    solicitud = consulta(
        db, "SELECT status FROM requests WHERE id = :id", {"id": application.request_id}
    )
    if not solicitud:
        raise HTTPException(status_code=404, detail="Solicitud no encontrada")
    if valor(db, "SELECT id FROM caregivers WHERE id = :id", {"id": cid}) is None:
        raise HTTPException(status_code=404, detail="Cuidador no encontrado")
    if solicitud[0]["status"] != "Pendiente":
        raise HTTPException(status_code=409, detail="La solicitud ya no está disponible")

    duplicada = valor(
        db,
        "SELECT id FROM applications WHERE caregiver_id = :c AND request_id = :r",
        {"c": cid, "r": application.request_id},
    )
    if duplicada is not None:
        raise HTTPException(status_code=409, detail="Ya te postulaste a esta solicitud")

    db.execute(
        text("INSERT INTO applications (caregiver_id, request_id) VALUES (:c, :r)"),
        {"c": cid, "r": application.request_id},
    )
    auditar(db, "POSTULAR", "requests", application.request_id, u["id"])
    db.commit()
    return {"mensaje": "Postulación registrada"}


@app.get("/my-applications")
def mis_postulaciones(
    u: dict = Depends(requiere("cuidador", "admin")),
    db: Session = Depends(get_db),
):
    if u["rol"] == "cuidador":
        filtro, params = "WHERE a.caregiver_id = :cid", {"cid": u["caregiver_id"]}
    else:
        filtro, params = "", {}

    datos = consulta(db, f"""
        SELECT
            a.id, a.estado, a.request_id, a.caregiver_id,
            r.service_type AS servicio, r.alcaldia,
            r.status AS estado_solicitud, c.nombre AS cuidador
        FROM applications a
        INNER JOIN requests r ON a.request_id = r.id
        LEFT JOIN caregivers c ON a.caregiver_id = c.id
        {filtro}
        ORDER BY a.id DESC
    """, params)
    return {"datos": datos}


@app.get("/matching/{request_id}")
def matching(
    request_id: int,
    u: dict = Depends(requiere("admin")),
    db: Session = Depends(get_db),
):
    solicitud = consulta(
        db,
        "SELECT service_type, alcaldia, latitud, longitud FROM requests WHERE id = :id",
        {"id": request_id},
    )
    if not solicitud:
        return {"datos": []}

    servicio = solicitud[0]["service_type"]
    alcaldia = solicitud[0]["alcaldia"]
    rlat, rlng = solicitud[0]["latitud"], solicitud[0]["longitud"]

    cuidadores = consulta(db, """
        SELECT id, nombre, especialidad, alcaldia, calificacion, costo_hora,
               latitud, longitud
        FROM caregivers
    """)

    resultados = []
    for c in cuidadores:
        calificacion = float(c["calificacion"] or 0)

        score = calificacion * 5          # hasta 25 puntos
        if c["especialidad"] == servicio:
            score += 40                   # misma especialidad
        if c["alcaldia"] == alcaldia:
            score += 30                   # misma alcaldía

        km = None
        if None not in (rlat, rlng, c["latitud"], c["longitud"]):
            km = round(haversine_km(
                float(rlat), float(rlng), float(c["latitud"]), float(c["longitud"])
            ), 1)

        resultados.append({
            "id": c["id"],
            "nombre": c["nombre"],
            "especialidad": c["especialidad"],
            "alcaldia": c["alcaldia"],
            "calificacion": calificacion,
            "costo_hora": float(c["costo_hora"] or 0),
            "distancia_km": km,
            "compatibilidad": round(score, 1),
        })

    resultados.sort(key=lambda x: x["compatibilidad"], reverse=True)
    return {"datos": resultados[:3]}


@app.post("/assign-caregiver")
def assign_caregiver(
    request_id: int,
    caregiver_id: int,
    u: dict = Depends(requiere("cuidador", "admin")),
    db: Session = Depends(get_db),
):
    cid = cuidador_efectivo(u, caregiver_id, obligatorio=True)

    solicitud = consulta(db, "SELECT status FROM requests WHERE id = :id", {"id": request_id})
    if not solicitud:
        raise HTTPException(status_code=404, detail="Solicitud no encontrada")
    if valor(db, "SELECT id FROM caregivers WHERE id = :id", {"id": cid}) is None:
        raise HTTPException(status_code=404, detail="Cuidador no encontrado")
    if solicitud[0]["status"] != "Pendiente":
        raise HTTPException(
            status_code=409, detail="La solicitud ya fue asignada o completada"
        )

    db.execute(
        text("""
            UPDATE requests SET caregiver_id = :c, status = 'Asignada'
            WHERE id = :r
        """),
        {"r": request_id, "c": cid},
    )

    ya_postulado = valor(
        db,
        "SELECT id FROM applications WHERE caregiver_id = :c AND request_id = :r",
        {"c": cid, "r": request_id},
    )
    if ya_postulado is None:
        db.execute(
            text("INSERT INTO applications (caregiver_id, request_id) VALUES (:c, :r)"),
            {"c": cid, "r": request_id},
        )

    auditar(db, "ASIGNAR", "requests", request_id, u["id"])
    db.commit()

    return {
        "mensaje": "Cuidador asignado correctamente",
        "request_id": request_id,
        "caregiver_id": cid,
    }


@app.post("/complete-service")
def complete_service(
    request_id: int,
    u: dict = Depends(requiere("cuidador", "admin")),
    db: Session = Depends(get_db),
):
    solicitud = consulta(
        db, "SELECT status, caregiver_id FROM requests WHERE id = :id", {"id": request_id}
    )
    # Un cuidador solo cierra servicios asignados a él (si no, "no existe").
    if not solicitud or (
        u["rol"] == "cuidador" and solicitud[0]["caregiver_id"] != u["caregiver_id"]
    ):
        raise HTTPException(status_code=404, detail="Solicitud no encontrada")

    estado = solicitud[0]["status"]
    if estado == "Completado":
        return {"mensaje": "El servicio ya estaba completado"}
    if estado != "Asignada":
        raise HTTPException(
            status_code=409, detail="Solo se pueden completar servicios asignados"
        )

    db.execute(
        text("UPDATE requests SET status = 'Completado' WHERE id = :id"),
        {"id": request_id},
    )
    auditar(db, "COMPLETAR", "requests", request_id, u["id"])
    db.commit()
    return {"mensaje": "Servicio completado correctamente"}


@app.get("/assigned-services")
def assigned_services(
    u: dict = Depends(requiere("cuidador", "admin")),
    db: Session = Depends(get_db),
):
    if u["rol"] == "cuidador":
        filtro, params = "AND r.caregiver_id = :cid", {"cid": u["caregiver_id"]}
    else:
        filtro, params = "", {}

    datos = consulta(db, f"""
        SELECT
            r.id, r.paciente, r.edad, r.estado_salud, r.prioridad,
            r.service_type AS servicio, r.alcaldia,
            r.status AS estado, r.caregiver_id, c.nombre AS cuidador
        FROM requests r
        LEFT JOIN caregivers c ON r.caregiver_id = c.id
        WHERE r.status = 'Asignada'
        {filtro}
        ORDER BY r.id DESC
    """, params)

    for d in datos:
        d["paciente"] = nombre_corto(d["paciente"])
    return {"datos": datos}


# =========================================================
# MAPA Y COBERTURA TERRITORIAL
# =========================================================

@app.get("/map-data")
def map_data(u: dict = Depends(usuario_actual), db: Session = Depends(get_db)):
    solicitudes = consulta(db, """
        SELECT alcaldia, COUNT(*) AS cantidad
        FROM requests
        WHERE alcaldia IS NOT NULL AND alcaldia <> ''
        GROUP BY alcaldia
        ORDER BY cantidad DESC
    """)
    cuidadores = consulta(db, """
        SELECT alcaldia, COUNT(*) AS cantidad
        FROM caregivers
        WHERE alcaldia IS NOT NULL AND alcaldia <> ''
        GROUP BY alcaldia
        ORDER BY cantidad DESC
    """)
    return {"solicitudes": solicitudes, "cuidadores": cuidadores}


@app.get("/map-markers")
def map_markers(u: dict = Depends(usuario_actual), db: Session = Depends(get_db)):
    """Las ubicaciones de pacientes se difuminan (~1 km) salvo para el admin."""
    es_admin = u["rol"] == "admin"
    decimales_solicitud = 5 if es_admin else 2

    solicitudes = consulta(db, """
        SELECT id, alcaldia, latitud, longitud, paciente,
               service_type AS servicio, prioridad, status AS estado
        FROM requests
        WHERE latitud IS NOT NULL AND longitud IS NOT NULL
          AND status <> 'Completado'
    """)
    cuidadores = consulta(db, """
        SELECT id, nombre, alcaldia, latitud, longitud, especialidad,
               calificacion, COALESCE(total_resenas, 0) AS total_resenas
        FROM caregivers
        WHERE latitud IS NOT NULL AND longitud IS NOT NULL
    """)

    for s in solicitudes:
        s["latitud"] = round(float(s["latitud"]), decimales_solicitud)
        s["longitud"] = round(float(s["longitud"]), decimales_solicitud)
        # En el mapa el paciente SOLO aparece con iniciales, incluso para el admin.
        s["paciente"] = iniciales(s["paciente"])

    for c in cuidadores:
        c["latitud"] = round(float(c["latitud"]), 3)
        c["longitud"] = round(float(c["longitud"]), 3)
        c["calificacion"] = float(c["calificacion"] or 0)
        if u["rol"] == "cuidador":
            c["nombre"] = nombre_corto(c["nombre"])

    return {"solicitudes": solicitudes, "cuidadores": cuidadores}


def semaforo_cobertura(pct: float) -> str:
    if pct < COBERTURA_ROJO:
        return "Rojo"
    if pct < COBERTURA_AMARILLO:
        return "Amarillo"
    return "Verde"


@app.get("/coverage-summary")
def coverage_summary(u: dict = Depends(usuario_actual), db: Session = Depends(get_db)):
    """Alcaldías ordenadas por MAYOR déficit de cuidadores.

    Déficit = cuidadores requeridos - cuidadores registrados.
    Cobertura = registrados / requeridos.
    """
    base = consulta(db, "SELECT * FROM demanda_alcaldia")

    registrados: dict = {}
    for r in consulta(db, """
        SELECT alcaldia, COUNT(*) AS total FROM caregivers
        WHERE alcaldia IS NOT NULL AND alcaldia <> '' GROUP BY alcaldia
    """):
        k = clave_alcaldia(r["alcaldia"])
        registrados[k] = registrados.get(k, 0) + r["total"]

    pendientes: dict = {}
    for r in consulta(db, """
        SELECT alcaldia, COUNT(*) AS total FROM requests
        WHERE status = 'Pendiente' AND alcaldia IS NOT NULL AND alcaldia <> ''
        GROUP BY alcaldia
    """):
        k = clave_alcaldia(r["alcaldia"])
        pendientes[k] = pendientes.get(k, 0) + r["total"]

    categorias: dict = {}
    for r in consulta(db, "SELECT alcaldia, categoria, personas FROM demanda_categoria"):
        categorias.setdefault(r["alcaldia"], {})[r["categoria"]] = r["personas"]

    resultado = []
    for b in base:
        k = clave_alcaldia(b["alcaldia"])
        requeridos = b["cuidadores_requeridos"]
        reg = registrados.get(k, 0)
        deficit = requeridos - reg
        cobertura = round(reg / requeridos * 100, 2) if requeridos else 100.0

        resultado.append({
            "alcaldia": b["alcaldia"],
            "personas_requieren_cuidado": b["personas_requieren_cuidado"],
            "adultos_mayores": b["adultos_mayores"],
            "medicos_generales": b["medicos_generales"],
            "especialistas": b["especialistas"],
            "cuidadores_requeridos": requeridos,
            "cuidadores_registrados": reg,
            "deficit": deficit,
            "cobertura_pct": cobertura,
            "semaforo": semaforo_cobertura(cobertura),
            "categorias": categorias.get(b["alcaldia"], {}),
            # Campos que ya usaba el mapa anterior:
            "solicitudes": pendientes.get(k, 0),
            "cuidadores": reg,
        })

    resultado.sort(key=lambda x: (-x["deficit"], x["alcaldia"]))
    return {"datos": resultado}


# =========================================================
# ADMINISTRACIÓN (único lugar con datos completos del paciente)
# =========================================================

@app.get("/admin/patients")
def admin_pacientes(
    q: Optional[str] = Query(None, max_length=60),
    alcaldia: Optional[str] = Query(None, max_length=80),
    estado: Optional[str] = Query(None, pattern="^(Pendiente|Asignada|Completado)$"),
    limite: int = Query(50, ge=1, le=200),
    desde: int = Query(0, ge=0),
    u: dict = Depends(requiere("admin")),
    db: Session = Depends(get_db),
):
    condiciones, params = [], {"limite": limite, "desde": desde}
    if q:
        condiciones.append("r.paciente ILIKE :q")
        params["q"] = f"%{q.strip()}%"
    if alcaldia:
        condiciones.append("r.alcaldia = :alcaldia")
        params["alcaldia"] = alcaldia
    if estado:
        condiciones.append("r.status = :estado")
        params["estado"] = estado
    donde = ("WHERE " + " AND ".join(condiciones)) if condiciones else ""

    total = valor(db, f"SELECT COUNT(*) FROM requests r {donde}", params)
    datos = consulta(db, f"""
        SELECT
            r.id, r.paciente, r.edad, r.estado_salud, r.prioridad, r.alcaldia,
            r.service_type AS servicio, r.status AS estado, r.request_date AS fecha,
            c.nombre AS cuidador
        FROM requests r
        LEFT JOIN caregivers c ON r.caregiver_id = c.id
        {donde}
        ORDER BY r.id DESC
        LIMIT :limite OFFSET :desde
    """, params)

    for d in datos:
        d["fecha"] = str(d["fecha"])

    # Cada consulta de datos completos de pacientes queda registrada.
    auditar(db, "CONSULTAR_PACIENTES", "requests", None, u["id"])
    db.commit()

    return {"total": total, "datos": datos}


def _recalcular_calificacion(db: Session, caregiver_id: int):
    r = consulta(db, """
        SELECT ROUND(AVG(estrellas)::numeric, 1) AS promedio, COUNT(*) AS total
        FROM service_ratings WHERE caregiver_id = :c
    """, {"c": caregiver_id})[0]
    db.execute(
        text("UPDATE caregivers SET calificacion = :p, total_resenas = :t WHERE id = :c"),
        {"p": r["promedio"] or 0, "t": r["total"], "c": caregiver_id},
    )


@app.put("/admin/caregivers/{caregiver_id}")
def admin_editar_cuidador(
    caregiver_id: int,
    cambios: CaregiverUpdate,
    u: dict = Depends(requiere("admin")),
    db: Session = Depends(get_db),
):
    datos = {k: v for k, v in cambios.model_dump().items() if v is not None}
    if not datos:
        raise HTTPException(status_code=422, detail="No enviaste ningún cambio")
    if "alcaldia" in datos:
        validar_alcaldia(datos["alcaldia"])
    for k in ("nombre", "experiencia"):
        if k in datos:
            datos[k] = datos[k].strip()
    if valor(db, "SELECT id FROM caregivers WHERE id = :id", {"id": caregiver_id}) is None:
        raise HTTPException(status_code=404, detail="Cuidador no encontrado")

    # Los nombres de columna salen de una lista fija (nunca del cliente).
    columnas = [c for c in ("nombre", "experiencia", "costo_hora", "especialidad", "alcaldia") if c in datos]
    asignaciones = ", ".join(f"{c} = :{c}" for c in columnas)
    db.execute(
        text(f"UPDATE caregivers SET {asignaciones} WHERE id = :id"),
        {**{c: datos[c] for c in columnas}, "id": caregiver_id},
    )
    auditar(db, "EDITAR", "caregivers", caregiver_id, u["id"])
    db.commit()
    return {"mensaje": "Cuidador actualizado"}


@app.delete("/admin/caregivers/{caregiver_id}")
def admin_borrar_cuidador(
    caregiver_id: int,
    u: dict = Depends(requiere("admin")),
    db: Session = Depends(get_db),
):
    if valor(db, "SELECT id FROM caregivers WHERE id = :id", {"id": caregiver_id}) is None:
        raise HTTPException(status_code=404, detail="Cuidador no encontrado")

    servicios = valor(
        db, "SELECT COUNT(*) FROM requests WHERE caregiver_id = :id", {"id": caregiver_id}
    )
    if servicios:
        raise HTTPException(
            status_code=409,
            detail=f"No se puede borrar: tiene {servicios} servicio(s) en su historial. "
                   "Puedes editar sus datos en su lugar.",
        )

    p = {"id": caregiver_id}
    db.execute(text("DELETE FROM applications WHERE caregiver_id = :id"), p)
    db.execute(text("DELETE FROM training_progress WHERE caregiver_id = :id"), p)
    db.execute(text("DELETE FROM certifications WHERE caregiver_id = :id"), p)
    db.execute(text("DELETE FROM service_ratings WHERE caregiver_id = :id"), p)
    db.execute(
        text("UPDATE requests SET cuidador_preferido_id = NULL WHERE cuidador_preferido_id = :id"), p
    )
    db.execute(text("DELETE FROM caregivers WHERE id = :id"), p)
    auditar(db, "BORRAR", "caregivers", caregiver_id, u["id"])
    db.commit()
    return {"mensaje": "Cuidador eliminado"}


@app.delete("/admin/requests/{request_id}")
def admin_borrar_solicitud(
    request_id: int,
    u: dict = Depends(requiere("admin")),
    db: Session = Depends(get_db),
):
    fila = consulta(
        db, "SELECT caregiver_id FROM requests WHERE id = :id", {"id": request_id}
    )
    if not fila:
        raise HTTPException(status_code=404, detail="Solicitud no encontrada")

    p = {"id": request_id}
    db.execute(text("DELETE FROM service_ratings WHERE request_id = :id"), p)
    db.execute(text("DELETE FROM applications WHERE request_id = :id"), p)
    db.execute(text("DELETE FROM requests WHERE id = :id"), p)
    if fila[0]["caregiver_id"] is not None:
        _recalcular_calificacion(db, fila[0]["caregiver_id"])
    auditar(db, "BORRAR", "requests", request_id, u["id"])
    db.commit()
    return {"mensaje": "Solicitud eliminada"}


@app.get("/admin/users")
def admin_usuarios(
    u: dict = Depends(requiere("admin")),
    db: Session = Depends(get_db),
):
    datos = consulta(db, """
        SELECT id, email, nombre, rol, activo, creado, ultimo_acceso
        FROM users ORDER BY id
    """)
    for d in datos:
        d["creado"] = str(d["creado"])
        d["ultimo_acceso"] = str(d["ultimo_acceso"]) if d["ultimo_acceso"] else None
    return {"datos": datos}


@app.put("/admin/users/{user_id}/activo")
def admin_activar_usuario(
    user_id: int,
    cambio: ActivoUpdate,
    u: dict = Depends(requiere("admin")),
    db: Session = Depends(get_db),
):
    if user_id == u["id"]:
        raise HTTPException(status_code=409, detail="No puedes desactivar tu propia cuenta")
    if valor(db, "SELECT id FROM users WHERE id = :id", {"id": user_id}) is None:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")

    db.execute(
        text("UPDATE users SET activo = :a WHERE id = :id"),
        {"a": cambio.activo, "id": user_id},
    )
    auditar(db, "ACTIVAR" if cambio.activo else "DESACTIVAR", "users", user_id, u["id"])
    db.commit()
    return {"mensaje": "Usuario actualizado"}


@app.get("/admin/audit")
def admin_bitacora(
    limite: int = Query(100, ge=1, le=500),
    u: dict = Depends(requiere("admin")),
    db: Session = Depends(get_db),
):
    datos = consulta(db, """
        SELECT id, accion, entidad, entidad_id, usuario_id, fecha
        FROM bitacora_auditoria
        ORDER BY id DESC
        LIMIT :limite
    """, {"limite": limite})
    for d in datos:
        d["fecha"] = str(d["fecha"])
    return {"datos": datos}
