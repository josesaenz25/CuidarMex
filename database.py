import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# La URL de la base NUNCA se escribe en el código (el repositorio es público).
# Local (PowerShell):
#   $env:DATABASE_URL = "postgresql://postgres:TU_CLAVE@localhost:5432/cuidarmex"
# En Render se define en Environment -> DATABASE_URL.
DATABASE_URL = os.environ.get("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError(
        "Falta la variable de entorno DATABASE_URL. "
        'En PowerShell: $env:DATABASE_URL = '
        '"postgresql://postgres:TU_CLAVE@localhost:5432/cuidarmex"'
    )

# Render entrega "postgres://..." o "postgresql://..."; SQLAlchemy necesita
# indicar el driver.
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg2://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://", 1)

# pool_pre_ping: reconecta si la conexión se cortó (la base de Render reinicia).
engine = create_engine(DATABASE_URL, pool_pre_ping=True)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
)
