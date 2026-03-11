from pydantic import BaseModel, EmailStr, field_validator
from typing import Optional, List
from datetime import datetime
from uuid import UUID
from decimal import Decimal

class LoginRequest(BaseModel):
    email: EmailStr
    password: str

class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    club: dict

class RefreshRequest(BaseModel):
    refresh_token: str

class CanchaBase(BaseModel):
    nombre: str
    tipo: str = "padel"
    precio_hora: Decimal
    precio_hora_finde: Optional[Decimal] = None
    duracion_minima: int = 60
    estado: str = "activa"
    foto_url: Optional[str] = None
    notas: Optional[str] = None
    horario: Optional[dict] = None
    orden: int = 0

class CanchaCreate(CanchaBase):
    pass

class CanchaUpdate(BaseModel):
    nombre: Optional[str] = None
    tipo: Optional[str] = None
    precio_hora: Optional[Decimal] = None
    precio_hora_finde: Optional[Decimal] = None
    duracion_minima: Optional[int] = None
    estado: Optional[str] = None
    foto_url: Optional[str] = None
    notas: Optional[str] = None
    horario: Optional[dict] = None
    orden: Optional[int] = None

class CanchaOut(CanchaBase):
    id: UUID
    club_id: UUID
    created_at: datetime
    model_config = {"from_attributes": True}

class CanchaEstadoUpdate(BaseModel):
    estado: str

class BloqueoCreate(BaseModel):
    cancha_ids: List[UUID]
    fecha_inicio: datetime
    fecha_fin: datetime
    motivo: Optional[str] = None

class BloqueoOut(BaseModel):
    id: UUID
    cancha_id: UUID
    cancha_nombre: Optional[str] = None
    fecha_inicio: datetime
    fecha_fin: datetime
    motivo: Optional[str] = None
    created_at: datetime
    model_config = {"from_attributes": True}

class ClienteCreate(BaseModel):
    nombre: str
    telefono: str

    @field_validator("telefono")
    @classmethod
    def normalize_phone(cls, v):
        v = v.strip().replace(" ", "").replace("-", "")
        if not v.startswith("+"):
            v = "+52" + v.lstrip("0")
        return v

class ClienteUpdate(BaseModel):
    nombre: Optional[str] = None
    notas_internas: Optional[str] = None

class ClienteBloqueoUpdate(BaseModel):
    esta_bloqueado: bool
    motivo_bloqueo: Optional[str] = None

class ClienteOut(BaseModel):
    id: UUID
    nombre: str
    telefono: str
    esta_bloqueado: bool
    motivo_bloqueo: Optional[str] = None
    notas_internas: Optional[str] = None
    created_at: datetime
    total_reservas: Optional[int] = 0
    ultima_reserva: Optional[datetime] = None
    model_config = {"from_attributes": True}

class ReservaCreate(BaseModel):
    cancha_id: UUID
    cliente_id: Optional[UUID] = None
    cliente_nombre: Optional[str] = None
    cliente_telefono: Optional[str] = None
    fecha_inicio: datetime
    duracion_min: int = 60
    nota_interna: Optional[str] = None
    origen: str = "manual"

class ReservaUpdate(BaseModel):
    estado: Optional[str] = None
    nota_interna: Optional[str] = None
    fecha_inicio: Optional[datetime] = None
    duracion_min: Optional[int] = None

class ReservaOut(BaseModel):
    id: UUID
    cancha_id: UUID
    cancha_nombre: Optional[str] = None
    cliente_id: UUID
    cliente_nombre: Optional[str] = None
    cliente_telefono: Optional[str] = None
    fecha_inicio: datetime
    fecha_fin: datetime
    duracion_min: int
    precio_total: Optional[Decimal] = None
    estado: str
    origen: str
    nota_interna: Optional[str] = None
    created_at: datetime
    model_config = {"from_attributes": True}
