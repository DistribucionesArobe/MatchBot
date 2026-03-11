from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from app.core.database import get_db
from app.core.security import verify_password, create_access_token, create_refresh_token, decode_token
from app.models import Admin, Club
from app.schemas import LoginRequest, TokenResponse, RefreshRequest

router = APIRouter(prefix="/auth", tags=["auth"])

@router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest, db: Session = Depends(get_db)):
    admin = db.query(Admin).filter(Admin.email == body.email).first()
    if not admin or not verify_password(body.password, admin.password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Credenciales incorrectas")
    if not admin.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cuenta desactivada")
    club = db.query(Club).filter(Club.id == admin.club_id).first()
    payload = {"sub": str(admin.id), "club_id": str(admin.club_id)}
    return TokenResponse(
        access_token=create_access_token(payload),
        refresh_token=create_refresh_token(payload),
        club={"id": str(club.id), "nombre": club.nombre, "logo_url": club.logo_url}
    )

@router.post("/refresh", response_model=TokenResponse)
def refresh(body: RefreshRequest, db: Session = Depends(get_db)):
    payload = decode_token(body.refresh_token)
    if payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Token inválido")
    admin = db.query(Admin).filter(Admin.id == payload.get("sub")).first()
    if not admin or not admin.is_active:
        raise HTTPException(status_code=401, detail="Usuario no encontrado")
    club = db.query(Club).filter(Club.id == admin.club_id).first()
    new_payload = {"sub": str(admin.id), "club_id": str(admin.club_id)}
    return TokenResponse(
        access_token=create_access_token(new_payload),
        refresh_token=create_refresh_token(new_payload),
        club={"id": str(club.id), "nombre": club.nombre, "logo_url": club.logo_url}
    )
