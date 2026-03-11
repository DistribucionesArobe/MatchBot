"""
Script de seed — crea Club Victoria con admin y 6 canchas.
Uso: python seed.py
"""
import sys
sys.path.append(".")

from app.core.database import SessionLocal
from app.core.security import hash_password
from app.models import Club, Admin, Cancha
import uuid

HORARIO_DEFAULT = {
    dia: {"activo": True, "apertura": "08:00", "cierre": "22:00"}
    for dia in ["lunes","martes","miercoles","jueves","viernes","sabado","domingo"]
}

def seed():
    db = SessionLocal()
    try:
        # Club
        club = Club(
            id=uuid.uuid4(),
            nombre="Club Victoria",
            direccion="Ciudad Victoria, Tamaulipas, México",
            telefono="+52 834 000 0000",
            zona_horaria="America/Monterrey",
            whatsapp_from="+52 834 247 2640",  # tu número de prueba
        )
        db.add(club)
        db.flush()

        # Admin
        admin = Admin(
            club_id=club.id,
            nombre="Alejandro Admin",
            email="admin@clubvictoria.mx",
            password=hash_password("Victoria2024!"),
        )
        db.add(admin)

        # 6 Canchas
        canchas_data = [
            {"nombre": "Cancha 1", "precio_hora": 200},
            {"nombre": "Cancha 2", "precio_hora": 200},
            {"nombre": "Cancha 3", "precio_hora": 200},
            {"nombre": "Cancha 4", "precio_hora": 250},
            {"nombre": "Cancha 5", "precio_hora": 250},
            {"nombre": "Cancha 6 — VIP", "precio_hora": 350, "precio_hora_finde": 400},
        ]

        for i, data in enumerate(canchas_data):
            cancha = Cancha(
                club_id=club.id,
                tipo="padel",
                duracion_minima=60,
                estado="activa",
                horario=HORARIO_DEFAULT,
                orden=i,
                **data,
            )
            db.add(cancha)

        db.commit()
        print("✅ Seed completado:")
        print(f"   Club:  {club.nombre} ({club.id})")
        print(f"   Admin: {admin.email} / Victoria2024!")
        print(f"   Canchas: 6 creadas")

    except Exception as e:
        db.rollback()
        print(f"❌ Error: {e}")
        raise
    finally:
        db.close()

if __name__ == "__main__":
    seed()
