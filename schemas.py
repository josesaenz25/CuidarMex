from typing import Optional

from pydantic import BaseModel, Field

PATRON_CORREO = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


# ---------- SESIÓN ----------

class RegisterCreate(BaseModel):
    email: str = Field(max_length=120, pattern=PATRON_CORREO)
    password: str = Field(min_length=8, max_length=128)
    nombre: str = Field(min_length=2, max_length=120)
    rol: str  # 'familia' o 'cuidador'
    acepta_aviso: bool = False
    acepta_datos_sensibles: bool = False
    version_aviso: str = Field(default="1.0", max_length=10)


class LoginCreate(BaseModel):
    email: str = Field(max_length=120)
    password: str = Field(max_length=128)


class ConsentCreate(BaseModel):
    version: str = Field(default="1.0", max_length=10)
    datos_sensibles: bool = False
    rol: Optional[str] = None  # se ignora: el rol sale de la sesión


class ActivoUpdate(BaseModel):
    activo: bool


# ---------- CUIDADORES Y SOLICITUDES ----------

class CaregiverCreate(BaseModel):
    nombre: str = Field(min_length=2, max_length=100)
    experiencia: str = Field(min_length=1, max_length=100)
    costo_hora: float = Field(gt=0, le=5000)
    especialidad: str = Field(min_length=1, max_length=100)
    alcaldia: str = Field(min_length=1, max_length=80)


class CaregiverUpdate(BaseModel):
    """Edición por el administrador: solo se cambian los campos enviados."""
    nombre: Optional[str] = Field(default=None, min_length=2, max_length=100)
    experiencia: Optional[str] = Field(default=None, min_length=1, max_length=100)
    costo_hora: Optional[float] = Field(default=None, gt=0, le=5000)
    especialidad: Optional[str] = Field(default=None, min_length=1, max_length=100)
    alcaldia: Optional[str] = Field(default=None, min_length=1, max_length=80)


class RequestCreate(BaseModel):
    service_type: str = Field(min_length=1, max_length=100)
    alcaldia: str = Field(min_length=1, max_length=80)
    paciente: str = Field(min_length=1, max_length=80)  # nombre o iniciales
    edad: Optional[int] = Field(default=None, ge=0, le=120)
    prioridad: str = Field(default="Media", pattern="^(Alta|Media|Baja)$")
    estado_salud: Optional[str] = Field(default=None, max_length=300)
    cuidador_preferido_id: Optional[int] = None
    latitud: Optional[float] = None
    longitud: Optional[float] = None


class ApplicationCreate(BaseModel):
    caregiver_id: int
    request_id: int


class RatingCreate(BaseModel):
    request_id: int
    estrellas: int = Field(ge=1, le=5)
    comentario: Optional[str] = Field(default=None, max_length=500)


class TrainingCourseCreate(BaseModel):
    nombre: str = Field(max_length=120)
    categoria: str = Field(max_length=60)
    descripcion: str = Field(max_length=500)
    duracion_horas: int = Field(gt=0, le=500)
    modalidad: str = Field(max_length=40)
