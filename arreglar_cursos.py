from sqlalchemy import text

from database import SessionLocal

# (nombre, descripción, categoría), en el mismo orden en que aparecen en la app
CURSOS = [
    ("Cuidado del adulto mayor", "Atención integral para personas adultas mayores", "Salud"),
    ("Primeros auxilios", "Atención básica de emergencias y lesiones", "Emergencias"),
    ("Movilización segura", "Técnicas de traslado y apoyo físico", "Cuidados"),
    ("Atención a personas con discapacidad", "Apoyo y acompañamiento especializado", "Inclusión"),
    ("Comunicación empática", "Comunicación efectiva con usuarios y familias", "Habilidades blandas"),
]

db = SessionLocal()

try:
    ids = [r[0] for r in db.execute(text("SELECT id FROM training_courses ORDER BY id"))]

    if len(ids) != len(CURSOS):
        raise SystemExit(f"Hay {len(ids)} cursos y esperaba {len(CURSOS)}. No se cambió nada.")

    for curso_id, (nombre, descripcion, categoria) in zip(ids, CURSOS):
        db.execute(
            text("""
                UPDATE training_courses
                SET nombre = :n, descripcion = :d, categoria = :c
                WHERE id = :id
            """),
            {"n": nombre, "d": descripcion, "c": categoria, "id": curso_id},
        )

    db.commit()
    print("Cursos corregidos.")
finally:
    db.close()