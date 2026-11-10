import os
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.orm import Session

from alcaldias import ALCALDIAS, coordenadas
from database import SessionLocal
from schemas import (
    ApplicationCreate,
    CaregiverCreate,
    ConsentCreate,
    RatingCreate,
    RequestCreate,
    TrainingCourseCreate,
)

# ---------- CONFIGURACIÓN DE SEGURIDAD (variables de entorno) ----------
# ENTORNO=produccion  -> apaga /docs, /redoc y /openapi.json (no exponer el mapa de la API)
# ALLOWED_ORIGINS     -> URL(s) de la app web separadas por coma, p. ej.
#                        https://cuidarmex-app.onrender.com
#                        Si no se define, permite todo (solo para desarrollo).
EN_PRODUCCION = os.environ.get("ENTORNO", "desarrollo").lower() == "produccion"

ORIGENES = [
    o.strip()
    for o in os.environ.get("ALLOWED_ORIGINS", "*").split(",")
    if o.strip()
] or ["*"]

app = FastAPI(
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
    """Evita que el navegador o un proxy guarden en caché datos de pacientes."""
    respuesta = await call_next(request)
    respuesta.headers["Cache-Control"] = "no-store"
    respuesta.headers["X-Content-Type-Options"] = "nosniff"
    respuesta.headers["Referrer-Policy"] = "no-referrer"
    return respuesta


# Umbrales del semáforo de cobertura (déficit = solicitudes pendientes - cuidadores)
UMBRAL_ROJO = 3
UMBRAL_AMARILLO = 1


# =========================
# UTILIDADES
# =========================

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


def semaforo_de(deficit: int) -> str:
    if deficit >= UMBRAL_ROJO:
        return "Rojo"
    if deficit >= UMBRAL_AMARILLO:
        return "Amarillo"
    return "Verde"


# =========================
# PRIVACIDAD: ENMASCARADO DE DATOS DEL PACIENTE
# =========================

_PARTICULAS = {"de", "del", "la", "las", "los", "y", "e"}


def _palabras(nombre) -> list:
    return [p for p in str(nombre or "").strip().split() if p]


def iniciales(nombre) -> str:
    """'María Flores González' -> 'M. F. G.'  (para quien solo ve la oferta)"""
    letras = [
        p[0].upper() + "."
        for p in _palabras(nombre)
        if p.lower() not in _PARTICULAS
    ]
    return " ".join(letras) or "Paciente"


def nombre_corto(nombre) -> str:
    """'María Flores González' -> 'María F.'  (para el cuidador ya asignado)"""
    palabras = [p for p in _palabras(nombre) if p.lower() not in _PARTICULAS]
    if not palabras:
        return "Paciente"
    primero = palabras[0].capitalize()
    if len(palabras) == 1:
        return primero
    return f"{primero} {palabras[1][0].upper()}."


def rango_edad(edad) -> str:
    """72 -> '70-79' ; 9 -> 'Menor de 18'. Sin unidad: la pantalla ya agrega 'años'."""
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


def validar_alcaldia(alcaldia: str):
    if alcaldia not in ALCALDIAS:
        raise HTTPException(
            status_code=422,
            detail=f"Alcaldía no válida: {alcaldia}",
        )


def auditar(db: Session, accion: str, entidad: str, entidad_id=None):
    """Registra en la bitácora QUÉ pasó y sobre QUÉ id (nunca datos personales)."""
    db.execute(
        text("""
            INSERT INTO bitacora_auditoria (accion, entidad, entidad_id)
            VALUES (:accion, :entidad, :entidad_id)
        """),
        {"accion": accion, "entidad": entidad, "entidad_id": entidad_id},
    )


@app.get("/")
def home():
    return {"mensaje": "Bienvenido a CuidarMéx"}


# =========================
# DISPONIBILIDAD Y PRIVACIDAD
# =========================

@app.get("/health")
def health(db: Session = Depends(get_db)):
    """Para monitoreo: confirma que la API y la base de datos responden."""
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        raise HTTPException(status_code=503, detail="Base de datos no disponible")
    return {"estado": "ok", "base_de_datos": "conectada"}


@app.post("/consent")
def registrar_consentimiento(c: ConsentCreate, db: Session = Depends(get_db)):
    if c.rol not in ("paciente", "cuidador"):
        raise HTTPException(status_code=422, detail="Rol no válido")

    nuevo_id = db.execute(
        text("""
            INSERT INTO consentimientos (rol, version_aviso, datos_sensibles)
            VALUES (:rol, :version, :sensibles)
            RETURNING id
        """),
        {"rol": c.rol, "version": c.version, "sensibles": c.datos_sensibles},
    ).scalar()
    auditar(db, "CONSENTIMIENTO", "consentimientos", nuevo_id)
    db.commit()

    return {"mensaje": "Consentimiento registrado", "id": nuevo_id}


# =========================
# CUIDADORES
# =========================

@app.get("/caregivers")
def obtener_cuidadores(db: Session = Depends(get_db)):
    datos = consulta(db, """
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
        ORDER BY id
    """)

    for d in datos:
        d["costo_hora"] = float(d["costo_hora"] or 0)
        d["calificacion"] = float(d["calificacion"] or 0)

    return {"datos": datos}


@app.post("/caregivers")
def crear_cuidador(caregiver: CaregiverCreate, db: Session = Depends(get_db)):
    validar_alcaldia(caregiver.alcaldia)
    lat, lng = coordenadas(caregiver.alcaldia)

    nuevo_id = db.execute(
        text("""
            INSERT INTO caregivers
                (nombre, experiencia, costo_hora, especialidad,
                 alcaldia, latitud, longitud)
            VALUES
                (:nombre, :experiencia, :costo_hora, :especialidad,
                 :alcaldia, :latitud, :longitud)
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
        },
    ).scalar()
    auditar(db, "CREAR", "caregivers", nuevo_id)
    db.commit()

    return {"mensaje": "Cuidador guardado correctamente", "id": nuevo_id}


# =========================
# SOLICITUDES
# =========================

@app.post("/requests")
def crear_solicitud(request: RequestCreate, db: Session = Depends(get_db)):
    validar_alcaldia(request.alcaldia)
    lat, lng = coordenadas(request.alcaldia)

    nuevo_id = db.execute(
        text("""
            INSERT INTO requests
                (service_type, alcaldia, paciente, edad, prioridad,
                 estado_salud, caregiver_id, status, latitud, longitud)
            VALUES
                (:service_type, :alcaldia, :paciente, :edad, :prioridad,
                 :estado_salud, :caregiver_id, :status, :latitud, :longitud)
            RETURNING id
        """),
        {
            "service_type": request.service_type,
            "alcaldia": request.alcaldia,
            "paciente": request.paciente.strip(),
            "edad": request.edad,
            "prioridad": request.prioridad,
            "estado_salud": request.estado_salud,
            "caregiver_id": request.caregiver_id,
            "status": request.status,
            "latitud": lat,
            "longitud": lng,
        },
    ).scalar()
    auditar(db, "CREAR", "requests", nuevo_id)
    db.commit()

    return {"mensaje": "Solicitud creada correctamente", "id": nuevo_id}


@app.get("/requests")
def obtener_solicitudes(db: Session = Depends(get_db)):
    datos = consulta(db, """
        SELECT
            r.id,
            COALESCE(c.nombre, 'Sin asignar') AS cuidador,
            r.service_type AS servicio,
            r.status AS estado,
            r.request_date AS fecha,
            r.alcaldia,
            r.caregiver_id,
            sr.estrellas
        FROM requests r
        LEFT JOIN caregivers c ON r.caregiver_id = c.id
        LEFT JOIN service_ratings sr ON sr.request_id = r.id
        ORDER BY r.id DESC
    """)

    for d in datos:
        d["fecha"] = str(d["fecha"])
        d["calificada"] = d["estrellas"] is not None
        d["estrellas"] = d["estrellas"] or 0

    return {"datos": datos}


@app.post("/ratings")
def calificar_servicio(rating: RatingCreate, db: Session = Depends(get_db)):
    solicitud = consulta(
        db,
        "SELECT caregiver_id, status FROM requests WHERE id = :id",
        {"id": rating.request_id},
    )
    if not solicitud:
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
        raise HTTPException(
            status_code=409, detail="Este servicio ya fue calificado"
        )

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

    # Promedio real de todas las reseñas del cuidador
    resumen = consulta(
        db,
        """
        SELECT ROUND(AVG(estrellas)::numeric, 1) AS promedio,
               COUNT(*) AS total
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
    auditar(db, "CALIFICAR", "requests", rating.request_id)
    db.commit()

    return {
        "mensaje": "Calificación registrada",
        "calificacion": float(resumen["promedio"]),
        "total_resenas": resumen["total"],
    }


# =========================
# DASHBOARD
# =========================

@app.get("/dashboard")
def dashboard(db: Session = Depends(get_db)):
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
        "pendientes": valor(
            db, "SELECT COUNT(*) FROM requests WHERE status = 'Pendiente'"
        ),
        "completadas": valor(
            db, "SELECT COUNT(*) FROM requests WHERE status = 'Completado'"
        ),
        "servicio_popular": servicio[0]["servicio"] if servicio else "Sin datos",
        "cuidador_popular": cuidador[0]["nombre"] if cuidador else "Sin datos",
    }


@app.get("/estadisticas-servicios")
def estadisticas_servicios(db: Session = Depends(get_db)):
    datos = consulta(db, """
        SELECT service_type AS servicio, COUNT(*) AS cantidad
        FROM requests
        GROUP BY service_type
        ORDER BY cantidad DESC, service_type
    """)
    return {"datos": datos}


@app.get("/estadisticas-estados")
def estadisticas_estados(db: Session = Depends(get_db)):
    datos = consulta(db, """
        SELECT status AS estado, COUNT(*) AS cantidad
        FROM requests
        GROUP BY status
    """)
    return {"datos": datos}


@app.get("/tendencia-solicitudes")
def tendencia_solicitudes(db: Session = Depends(get_db)):
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
def dashboard_ejecutivo(db: Session = Depends(get_db)):
    solicitudes = valor(db, "SELECT COUNT(*) FROM requests")
    completadas = valor(
        db, "SELECT COUNT(*) FROM requests WHERE status = 'Completado'"
    )

    tasa_exito = round(completadas / solicitudes * 100, 2) if solicitudes else 0

    return {
        "cuidadores": valor(db, "SELECT COUNT(*) FROM caregivers"),
        "solicitudes": solicitudes,
        "postulaciones": valor(db, "SELECT COUNT(*) FROM applications"),
        "certificaciones": valor(
            db,
            "SELECT COUNT(*) FROM certifications WHERE certificado_generado = TRUE",
        ),
        "asignadas": valor(
            db, "SELECT COUNT(*) FROM requests WHERE status = 'Asignada'"
        ),
        "completadas": completadas,
        "tasa_exito": tasa_exito,
    }


# =========================
# CAPACITACIÓN
# =========================

@app.get("/training-courses")
def obtener_cursos(
    caregiver_id: Optional[int] = None,
    db: Session = Depends(get_db),
):
    datos = consulta(db, """
        SELECT
            tc.id,
            tc.nombre,
            tc.categoria,
            tc.descripcion,
            tc.duracion_horas,
            tc.modalidad,
            tc.activo,
            EXISTS (
                SELECT 1
                FROM training_progress tp
                WHERE tp.course_id = tc.id
                  AND tp.caregiver_id = :caregiver_id
                  AND tp.completado = TRUE
            ) AS completado
        FROM training_courses tc
        ORDER BY tc.id
    """, {"caregiver_id": caregiver_id})
    return {"datos": datos}


@app.post("/training-courses")
def crear_curso(curso: TrainingCourseCreate, db: Session = Depends(get_db)):
    db.execute(
        text("""
            INSERT INTO training_courses
                (nombre, categoria, descripcion, duracion_horas, modalidad)
            VALUES
                (:nombre, :categoria, :descripcion, :duracion_horas, :modalidad)
        """),
        {
            "nombre": curso.nombre,
            "categoria": curso.categoria,
            "descripcion": curso.descripcion,
            "duracion_horas": curso.duracion_horas,
            "modalidad": curso.modalidad,
        },
    )
    db.commit()

    return {"mensaje": "Curso creado correctamente"}


@app.get("/training-progress")
def training_progress(
    caregiver_id: Optional[int] = None,
    db: Session = Depends(get_db),
):
    total = valor(db, "SELECT COUNT(*) FROM training_courses")

    if caregiver_id is None:
        completados = valor(
            db, "SELECT COUNT(*) FROM training_progress WHERE completado = TRUE"
        )
    else:
        completados = valor(
            db,
            """
            SELECT COUNT(DISTINCT course_id)
            FROM training_progress
            WHERE completado = TRUE AND caregiver_id = :caregiver_id
            """,
            {"caregiver_id": caregiver_id},
        )

    porcentaje = round(completados / total * 100, 2) if total else 0

    return {
        "total": total,
        "completados": completados,
        "porcentaje": porcentaje,
    }


@app.get("/certifications")
def obtener_certificaciones(
    caregiver_id: Optional[int] = None,
    db: Session = Depends(get_db),
):
    filtro = "AND c.caregiver_id = :caregiver_id" if caregiver_id is not None else ""
    params = {"caregiver_id": caregiver_id} if caregiver_id is not None else {}

    datos = consulta(db, f"""
        SELECT
            c.id,
            tc.nombre AS curso,
            c.fecha_generacion AS fecha
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
    caregiver_id: int,
    db: Session = Depends(get_db),
):
    cuidador = valor(
        db, "SELECT id FROM caregivers WHERE id = :id", {"id": caregiver_id}
    )
    if cuidador is None:
        raise HTTPException(status_code=404, detail="Cuidador no encontrado")

    curso = consulta(
        db, "SELECT activo FROM training_courses WHERE id = :id", {"id": course_id}
    )
    if not curso:
        raise HTTPException(status_code=404, detail="Curso no encontrado")
    if curso[0]["activo"] is False:
        raise HTTPException(status_code=409, detail="El curso no está disponible")

    ya_completado = valor(
        db,
        """
        SELECT id FROM training_progress
        WHERE caregiver_id = :caregiver_id
          AND course_id = :course_id
          AND completado = TRUE
        """,
        {"caregiver_id": caregiver_id, "course_id": course_id},
    )
    if ya_completado is not None:
        raise HTTPException(status_code=409, detail="Ya completaste este curso")

    existente = valor(
        db,
        """
        SELECT id FROM training_progress
        WHERE caregiver_id = :caregiver_id AND course_id = :course_id
        """,
        {"caregiver_id": caregiver_id, "course_id": course_id},
    )

    if existente is None:
        db.execute(
            text("""
                INSERT INTO training_progress
                    (caregiver_id, course_id, completado, fecha_completado)
                VALUES (:caregiver_id, :course_id, TRUE, NOW())
            """),
            {"caregiver_id": caregiver_id, "course_id": course_id},
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

    # Al terminar el curso se genera la certificación (una por curso y cuidador)
    certificada = valor(
        db,
        """
        SELECT id FROM certifications
        WHERE caregiver_id = :caregiver_id AND course_id = :course_id
        """,
        {"caregiver_id": caregiver_id, "course_id": course_id},
    )
    if certificada is None:
        db.execute(
            text("""
                INSERT INTO certifications
                    (caregiver_id, course_id, certificado_generado, fecha_generacion)
                VALUES (:caregiver_id, :course_id, TRUE, NOW())
            """),
            {"caregiver_id": caregiver_id, "course_id": course_id},
        )

    db.commit()

    return {"mensaje": "Curso completado y certificación generada"}


# =========================
# OPORTUNIDADES Y POSTULACIONES
# =========================

@app.get("/opportunities")
def obtener_oportunidades(db: Session = Depends(get_db)):
    datos = consulta(db, """
        SELECT
            id,
            paciente,
            edad,
            prioridad,
            estado_salud,
            service_type AS servicio,
            alcaldia,
            status AS estado
        FROM requests
        WHERE status = 'Pendiente'
        ORDER BY id DESC
    """)

    # Quien solo está viendo la oferta NO necesita identificar al paciente.
    for d in datos:
        d["paciente"] = iniciales(d["paciente"])
        d["edad"] = rango_edad(d["edad"])
        d["estado_salud"] = "Se comparte al ser asignado"

    return {"datos": datos}


@app.post("/apply")
def postularse(application: ApplicationCreate, db: Session = Depends(get_db)):
    existe = valor(
        db,
        "SELECT id FROM requests WHERE id = :id",
        {"id": application.request_id},
    )
    if existe is None:
        raise HTTPException(status_code=404, detail="Solicitud no encontrada")

    existe_cuidador = valor(
        db,
        "SELECT id FROM caregivers WHERE id = :id",
        {"id": application.caregiver_id},
    )
    if existe_cuidador is None:
        raise HTTPException(status_code=404, detail="Cuidador no encontrado")

    duplicada = valor(
        db,
        """
        SELECT id FROM applications
        WHERE caregiver_id = :caregiver_id AND request_id = :request_id
        """,
        {
            "caregiver_id": application.caregiver_id,
            "request_id": application.request_id,
        },
    )
    if duplicada is not None:
        raise HTTPException(
            status_code=409, detail="Ya te postulaste a esta solicitud"
        )

    db.execute(
        text("""
            INSERT INTO applications (caregiver_id, request_id)
            VALUES (:caregiver_id, :request_id)
        """),
        {
            "caregiver_id": application.caregiver_id,
            "request_id": application.request_id,
        },
    )
    auditar(db, "POSTULAR", "requests", application.request_id)
    db.commit()

    return {"mensaje": "Postulación registrada"}


@app.get("/my-applications")
def mis_postulaciones(db: Session = Depends(get_db)):
    datos = consulta(db, """
        SELECT
            a.id,
            a.estado,
            a.request_id,
            a.caregiver_id,
            r.service_type AS servicio,
            r.alcaldia,
            r.status AS estado_solicitud,
            c.nombre AS cuidador
        FROM applications a
        INNER JOIN requests r ON a.request_id = r.id
        LEFT JOIN caregivers c ON a.caregiver_id = c.id
        ORDER BY a.id DESC
    """)
    return {"datos": datos}


# =========================
# MATCHING, ASIGNACIÓN Y CIERRE
# =========================

@app.get("/matching/{request_id}")
def matching(request_id: int, db: Session = Depends(get_db)):
    solicitud = consulta(
        db,
        "SELECT service_type, alcaldia FROM requests WHERE id = :id",
        {"id": request_id},
    )

    if not solicitud:
        return {"datos": []}

    servicio = solicitud[0]["service_type"]
    alcaldia = solicitud[0]["alcaldia"]

    cuidadores = consulta(db, """
        SELECT id, nombre, especialidad, alcaldia, calificacion, costo_hora
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

        resultados.append({
            "id": c["id"],
            "nombre": c["nombre"],
            "especialidad": c["especialidad"],
            "alcaldia": c["alcaldia"],
            "calificacion": calificacion,
            "costo_hora": float(c["costo_hora"] or 0),
            "compatibilidad": round(score, 1),
        })

    resultados.sort(key=lambda x: x["compatibilidad"], reverse=True)

    return {"datos": resultados[:3]}


@app.post("/assign-caregiver")
def assign_caregiver(
    request_id: int,
    caregiver_id: int,
    db: Session = Depends(get_db),
):
    solicitud = consulta(
        db,
        "SELECT status FROM requests WHERE id = :id",
        {"id": request_id},
    )
    if not solicitud:
        raise HTTPException(status_code=404, detail="Solicitud no encontrada")

    cuidador = valor(
        db,
        "SELECT id FROM caregivers WHERE id = :id",
        {"id": caregiver_id},
    )
    if cuidador is None:
        raise HTTPException(status_code=404, detail="Cuidador no encontrado")

    if solicitud[0]["status"] != "Pendiente":
        raise HTTPException(
            status_code=409,
            detail="La solicitud ya fue asignada o completada",
        )

    db.execute(
        text("""
            UPDATE requests
            SET caregiver_id = :caregiver_id, status = 'Asignada'
            WHERE id = :request_id
        """),
        {"request_id": request_id, "caregiver_id": caregiver_id},
    )

    # Si la asignación no vino de una postulación (por ejemplo, desde
    # Matching), se registra una, para que la solicitud aparezca en
    # "Mis Postulaciones" y se pueda completar desde ahí.
    ya_postulado = valor(
        db,
        """
        SELECT id FROM applications
        WHERE caregiver_id = :caregiver_id AND request_id = :request_id
        """,
        {"caregiver_id": caregiver_id, "request_id": request_id},
    )
    if ya_postulado is None:
        db.execute(
            text("""
                INSERT INTO applications (caregiver_id, request_id)
                VALUES (:caregiver_id, :request_id)
            """),
            {"caregiver_id": caregiver_id, "request_id": request_id},
        )

    auditar(db, "ASIGNAR", "requests", request_id)
    db.commit()

    return {
        "mensaje": "Cuidador asignado correctamente",
        "request_id": request_id,
        "caregiver_id": caregiver_id,
    }


@app.post("/complete-service")
def complete_service(request_id: int, db: Session = Depends(get_db)):
    solicitud = consulta(
        db,
        "SELECT status FROM requests WHERE id = :id",
        {"id": request_id},
    )
    if not solicitud:
        raise HTTPException(status_code=404, detail="Solicitud no encontrada")

    estado = solicitud[0]["status"]

    if estado == "Completado":
        return {"mensaje": "El servicio ya estaba completado"}

    if estado != "Asignada":
        raise HTTPException(
            status_code=409,
            detail="Solo se pueden completar servicios asignados",
        )

    db.execute(
        text("UPDATE requests SET status = 'Completado' WHERE id = :id"),
        {"id": request_id},
    )
    auditar(db, "COMPLETAR", "requests", request_id)
    db.commit()

    return {"mensaje": "Servicio completado correctamente"}


@app.get("/assigned-services")
def assigned_services(db: Session = Depends(get_db)):
    datos = consulta(db, """
        SELECT
            r.id,
            r.paciente,
            r.edad,
            r.service_type AS servicio,
            r.alcaldia,
            r.status AS estado,
            r.caregiver_id,
            c.nombre AS cuidador
        FROM requests r
        LEFT JOIN caregivers c ON r.caregiver_id = c.id
        WHERE r.status = 'Asignada'
        ORDER BY r.id DESC
    """)

    # Cuidador asignado: nombre de pila + inicial; nunca el nombre completo.
    for d in datos:
        d["paciente"] = nombre_corto(d["paciente"])

    return {"datos": datos}


# =========================
# MAPA Y COBERTURA TERRITORIAL
# =========================

@app.get("/map-data")
def map_data(db: Session = Depends(get_db)):
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
def map_markers(db: Session = Depends(get_db)):
    solicitudes = consulta(db, """
        SELECT id, alcaldia, latitud, longitud
        FROM requests
        WHERE latitud IS NOT NULL
          AND longitud IS NOT NULL
          AND status <> 'Completado'
    """)

    cuidadores = consulta(db, """
        SELECT id, nombre, alcaldia, latitud, longitud
        FROM caregivers
        WHERE latitud IS NOT NULL
          AND longitud IS NOT NULL
    """)

    for fila in solicitudes + cuidadores:
        fila["latitud"] = float(fila["latitud"])
        fila["longitud"] = float(fila["longitud"])

    return {"solicitudes": solicitudes, "cuidadores": cuidadores}


@app.get("/coverage-summary")
def coverage_summary(db: Session = Depends(get_db)):
    # Demanda: solo solicitudes pendientes (las que aún necesitan cuidador)
    demanda = {
        r["alcaldia"]: r["total"]
        for r in consulta(db, """
            SELECT alcaldia, COUNT(*) AS total
            FROM requests
            WHERE status = 'Pendiente'
              AND alcaldia IS NOT NULL AND alcaldia <> ''
            GROUP BY alcaldia
        """)
    }

    # Oferta: cuidadores registrados por alcaldía
    oferta = {
        r["alcaldia"]: r["total"]
        for r in consulta(db, """
            SELECT alcaldia, COUNT(*) AS total
            FROM caregivers
            WHERE alcaldia IS NOT NULL AND alcaldia <> ''
            GROUP BY alcaldia
        """)
    }

    resultado = []

    for alcaldia in set(demanda) | set(oferta):
        total_solicitudes = demanda.get(alcaldia, 0)
        total_cuidadores = oferta.get(alcaldia, 0)
        deficit = total_solicitudes - total_cuidadores

        resultado.append({
            "alcaldia": alcaldia,
            "solicitudes": total_solicitudes,
            "cuidadores": total_cuidadores,
            "deficit": deficit,
            "semaforo": semaforo_de(deficit),
        })

    # Primero las alcaldías con mayor déficit
    resultado.sort(key=lambda x: (-x["deficit"], x["alcaldia"]))

    return {"datos": resultado}