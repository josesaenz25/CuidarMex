from sqlalchemy import Column
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import Numeric
from sqlalchemy.orm import declarative_base

Base = declarative_base()

class Caregiver(Base):
    __tablename__ = "caregivers"

    id = Column(Integer, primary_key=True, index=True)

    nombre = Column(String(100))

    experiencia = Column(String(255))

    costo_hora = Column(Numeric(10, 2))