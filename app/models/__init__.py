from sqlalchemy import Column, String, Integer, Boolean, DateTime, ForeignKey, Text, Numeric, JSON
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.core.database import Base
import uuid

class Club(Base):
    __tablename__ = "clubs"
    id            = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    nombre        = Column(String(120), nullable=False)
    direccion     = Column(String(255))
    telefono      = Column(String(20))
    logo_url      = Column(String(500))
    zona_horaria  = Column(String(60), default="America/Monterrey")
    whatsapp_from = Column(String(30))
    is_active     = Column(Boolean, default=True)
    created_at    = Column(DateTime(timezone=True), server_default=func.now())
    admins   = relationship("Admin",   back_populates="club")
    canchas  = relationship("Cancha",  back_populates="club")
    clientes = relationship("Cliente", back_populates="club")

class Admin(Base):
    __tablename__ = "admins"
    id         = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    club_id    = Column(UUID(as_uuid=True), ForeignKey("clubs.id"), nullable=False)
    nombre     = Column(String(100), nullable=False)
    email      = Column(String(150), unique=True, nullable=False, index=True)
    password   = Column(String(255), nullable=False)
    is_active  = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    club = relationship("Club", back_populates="admins")

class Cancha(Base):
    __tablename__ = "canchas"
    id                = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    club_id           = Column(UUID(as_uuid=True), ForeignKey("clubs.id"), nullable=False)
    nombre            = Column(String(80), nullable=False)
    tipo              = Column(String(40), default="padel")
    precio_hora       = Column(Numeric(10, 2), nullable=False)
    precio_hora_finde = Column(Numeric(10, 2))
    duracion_minima   = Column(Integer, default=60)
    estado            = Column(String(20), default="activa")
    foto_url          = Column(String(500))
    notas             = Column(Text)
    horario           = Column(JSON, default=dict)
    orden             = Column(Integer, default=0)
    created_at        = Column(DateTime(timezone=True), server_default=func.now())
    club     = relationship("Club",    back_populates="canchas")
    reservas = relationship("Reserva", back_populates="cancha")
    bloqueos = relationship("Bloqueo", back_populates="cancha")

class Bloqueo(Base):
    __tablename__ = "bloqueos"
    id           = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    cancha_id    = Column(UUID(as_uuid=True), ForeignKey("canchas.id"), nullable=False)
    fecha_inicio = Column(DateTime(timezone=True), nullable=False)
    fecha_fin    = Column(DateTime(timezone=True), nullable=False)
    motivo       = Column(String(255))
    created_at   = Column(DateTime(timezone=True), server_default=func.now())
    cancha = relationship("Cancha", back_populates="bloqueos")

class Cliente(Base):
    __tablename__ = "clientes"
    id             = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    club_id        = Column(UUID(as_uuid=True), ForeignKey("clubs.id"), nullable=False)
    nombre         = Column(String(120), nullable=False)
    telefono       = Column(String(20), nullable=False, index=True)
    esta_bloqueado = Column(Boolean, default=False)
    motivo_bloqueo = Column(String(255))
    notas_internas = Column(Text)
    created_at     = Column(DateTime(timezone=True), server_default=func.now())
    club     = relationship("Club",    back_populates="clientes")
    reservas = relationship("Reserva", back_populates="cliente")

class Reserva(Base):
    __tablename__ = "reservas"
    id           = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    cancha_id    = Column(UUID(as_uuid=True), ForeignKey("canchas.id"), nullable=False)
    cliente_id   = Column(UUID(as_uuid=True), ForeignKey("clientes.id"), nullable=False)
    fecha_inicio = Column(DateTime(timezone=True), nullable=False, index=True)
    fecha_fin    = Column(DateTime(timezone=True), nullable=False)
    duracion_min = Column(Integer, nullable=False)
    precio_total = Column(Numeric(10, 2))
    estado       = Column(String(20), default="pendiente", index=True)
    origen       = Column(String(20), default="whatsapp")
    nota_interna = Column(Text)
    message_sid  = Column(String(60), unique=True)
    created_at   = Column(DateTime(timezone=True), server_default=func.now())
    updated_at   = Column(DateTime(timezone=True), onupdate=func.now())
    cancha  = relationship("Cancha",  back_populates="reservas")
    cliente = relationship("Cliente", back_populates="reservas")
