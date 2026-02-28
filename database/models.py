from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.orm import relationship
from sqlalchemy import ForeignKey
from typing import List
from datetime import datetime

class Base(DeclarativeBase):
    pass

class Client(Base):
    __tablename__ = "clients"
    
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(nullable=False)
    password: Mapped[str] = mapped_column(nullable=False)
    is_admin: Mapped[bool] = mapped_column(nullable=False, default=False)
    
    refresh_tokens: Mapped[List["RefreshToken"]] = relationship("RefreshToken", back_populates="client", cascade="all, delete-orphan")

class RefreshToken(Base):
    __tablename__="refresh_tokens"
    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    token: Mapped[str] = mapped_column(unique=True, index=True)
    
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    client: Mapped["Client"] = relationship("Client", back_populates="refresh_tokens")
    
class VPNServer(Base):
    __tablename__ = "vpn_servers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(nullable=False)      
    country_code: Mapped[str] = mapped_column(default="DE") 
    
    ip_address: Mapped[str] = mapped_column(unique=True, nullable=False)
    marzban_port: Mapped[int] = mapped_column(default=8000)
    ssh_port: Mapped[int] = mapped_column(default=22)
   
    mar_admin_user: Mapped[str] = mapped_column(nullable=False)
    mar_admin_pass: Mapped[str] = mapped_column(nullable=False)
    
    is_active: Mapped[bool] = mapped_column(default=True)
    current_users_count: Mapped[int] = mapped_column(default=0) 
    configs: Mapped[List["Config"]] = relationship("Config", back_populates="server")
    

class Config(Base):
    __tablename__ = "configs"

    id: Mapped[int] = mapped_column(primary_key=True)
    
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"))
    server_id: Mapped[int] = mapped_column(ForeignKey("vpn_servers.id"))
    
   
    marzban_username: Mapped[str] = mapped_column(unique=True) 
    subscription_url: Mapped[str] = mapped_column(nullable=True)
    

    data_limit: Mapped[int] = mapped_column(nullable=True, default=0) 
    speed_limit: Mapped[int] = mapped_column(nullable=True) 
    expire_at: Mapped[datetime] = mapped_column(nullable=True)
    
    is_active: Mapped[bool] = mapped_column(default=True)

  
    server: Mapped["VPNServer"] = relationship("VPNServer", back_populates="configs")
    owner: Mapped["Client"] = relationship("Client")
