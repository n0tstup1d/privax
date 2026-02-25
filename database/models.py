from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.orm import relationship
from sqlalchemy import ForeignKey
from typing import List

class Base(DeclarativeBase):
    pass

class Client(Base):
    __tablename__ = "clients"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(nullable=False)
    password: Mapped[str] = mapped_column(nullable=False)

    refresh_tokens: Mapped[List["RefreshToken"]] = relationship("RefreshToken", back_populates="owner", cascade="all, delete-orphan")

class RefreshToken(Base):
    __tablename__="refresh_tokens"
    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    token: Mapped[str] = mapped_column(unique=True, index=True)
    
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    client: Mapped["Client"] = relationship("Client", back_populates="refresh_tokens")