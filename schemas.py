from typing import Optional

from pydantic import BaseModel, Field


class CaregiverCreate(BaseModel):
    nombre: str
    experiencia: str
    costo_hora: float = Field(gt=0)
    especialidad: str
    alcaldia: str


class RequestCreate(BaseModel):
    service_type: str
    alcaldia: str
    paciente: str
    edad: Optional[int] = None
    prioridad: str = "Media"
    estado_salud: Optional[str] = None
    caregiver_id: Optional[int] = None
    status: str = "Pendiente"


class TrainingCourseCreate(BaseModel):
    nombre: str
    categoria: str
    descripcion: str
    duracion_horas: int
    modalidad: str


class ApplicationCreate(BaseModel):
    caregiver_id: int
    request_id: int


class RatingCreate(BaseModel):
    request_id: int
    estrellas: int = Field(ge=1, le=5)
    comentario: Optional[str] = None


class ConsentCreate(BaseModel):
    rol: str
    version: str
    datos_sensibles: bool = False