from sqlalchemy import Column, Integer, String, JSON, BigInteger, ForeignKey, DateTime
from sqlalchemy.orm import relationship
from database.db import Base
from datetime import datetime


class World(Base):
    __tablename__ = "worlds"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False, unique=False)
    s3URL = Column(String(255), nullable=False)
    status = Column(String(255), nullable=False)
    admins = Column(JSON, nullable=False, default=list)
    players = Column(JSON, nullable=False, default=list)