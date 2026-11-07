import argparse
import random
from datetime import datetime, timedelta

from sqlalchemy import text

from alcaldias import ALCALDIAS, coordenadas
from database import SessionLocal

SERVICIOS = [
    "Persona adulta mayor",
    "Persona con discapacidad",
    "Niño o niña",
    "Acompañamiento",
    "Cuidado personal",
]

NOMBRES = ["Ana", "Luis", "María", "Jorge", "Carmen", "Pedro", "Laura",
           "Ricardo", "Sofía", "Miguel", "Lucía", "Andrés", "Elena", "Raúl"]
APELLIDOS = ["García", "Hernández", "López", "Martínez", "González",
             "Pérez", "Sánchez", "Ramírez", "Castillo", "Flores", "Torres"]
EXPERIENCIA = ["1 año", "2 años", "3 años", "5 años", "8 años", "10 años"]
ESTADOS_SALUD = ["Estable", "Movilidad reducida", "Requiere supervisión",
                 "Postoperatorio", "Condición crónica"]
EDADES = {
    "Persona adulta mayor": (65, 92),
    "Persona con discapacidad": (18, 70),
    "Niño o niña": (2, 12),
    "Acompañamiento": (30, 85),
    "Cuidado personal": (25, 90),
}


def nombre_completo():
    return f"{random.choice(NOMBRES)} {random.choice(APELLIDOS)} {random.choice(APELLIDOS)}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--limpiar",
        action="store_true",
        help="Borra postulaciones, solicitudes y cuidadores antes de cargar",
    )
    args = parser.parse_args()

    random.seed(42)
    db = SessionLocal()

    try:
        if args.limpiar:
            for tabla in ("applications", "requests", "caregivers"):
                db.execute(text(f"DELETE FROM {tabla}"))

        cuidadores_por_alcaldia = {}

        for alcaldia in ALCALDIAS:
            ids = []
            for _ in range(random.randint(2, 6)):
                lat, lng = coordenadas(alcaldia)
                nuevo = db.execute(
                    text("""
                        INSERT INTO caregivers
                            (nombre, experiencia, costo_hora, especialidad,
                             calificacion, alcaldia, latitud, longitud)
                        VALUES
                            (:nombre, :experiencia, :costo, :especialidad,
                             :calificacion, :alcaldia, :lat, :lng)
                        RETURNING id
                    """),
                    {
                        "nombre": nombre_completo(),
                        "experiencia": random.choice(EXPERIENCIA),
                        "costo": random.choice(range(80, 201, 10)),
                        "especialidad": random.choice(SERVICIOS),
                        "calificacion": round(random.uniform(3.5, 5.0), 1),
                        "alcaldia": alcaldia,
                        "lat": lat,
                        "lng": lng,
                    },
                ).scalar()
                ids.append(nuevo)
            cuidadores_por_alcaldia[alcaldia] = ids

        total_solicitudes = 0

        for alcaldia in ALCALDIAS:
            for _ in range(random.randint(3, 12)):
                servicio = random.choice(SERVICIOS)
                estado = random.choices(
                    ["Pendiente", "Asignada", "Completado"],
                    weights=[60, 15, 25],
                )[0]
                caregiver_id = (
                    None
                    if estado == "Pendiente"
                    else random.choice(cuidadores_por_alcaldia[alcaldia])
                )
                edad_min, edad_max = EDADES[servicio]
                lat, lng = coordenadas(alcaldia)

                db.execute(
                    text("""
                        INSERT INTO requests
                            (service_type, alcaldia, paciente, edad, prioridad,
                             estado_salud, caregiver_id, status, latitud,
                             longitud, request_date)
                        VALUES
                            (:servicio, :alcaldia, :paciente, :edad, :prioridad,
                             :estado_salud, :caregiver_id, :status, :lat,
                             :lng, :fecha)
                    """),
                    {
                        "servicio": servicio,
                        "alcaldia": alcaldia,
                        "paciente": nombre_completo(),
                        "edad": random.randint(edad_min, edad_max),
                        "prioridad": random.choice(["Alta", "Media", "Baja"]),
                        "estado_salud": random.choice(ESTADOS_SALUD),
                        "caregiver_id": caregiver_id,
                        "status": estado,
                        "lat": lat,
                        "lng": lng,
                        "fecha": datetime.now()
                        - timedelta(
                            days=random.randint(0, 13),
                            hours=random.randint(0, 23),
                        ),
                    },
                )
                total_solicitudes += 1

        db.commit()

        total_cuidadores = sum(len(v) for v in cuidadores_por_alcaldia.values())
        print(f"Listo: {total_cuidadores} cuidadores y {total_solicitudes} solicitudes "
              f"en {len(ALCALDIAS)} alcaldias.")

    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()