from sqlalchemy import Column, Integer, String, JSON
from database.db import Base


class World(Base):
    __tablename__ = "worlds"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False, unique=False)
    s3URL = Column(String(255), nullable=False)
    status = Column(String(255), nullable=False)
    admins = Column(JSON, nullable=False, default=list)
    players = Column(JSON, nullable=False, default=list)
    params = Column(JSON, nullable=False, default={})
    domainPrefix = Column(String(255), nullable=True, unique=False)

    # Runtime placement: set while the world runs, SQL NULL once it is stopped.
    # none_as_null stores Python None as NULL instead of the JSON literal 'null'.
    provider = Column(String(64), nullable=True)
    ports = Column(JSON(none_as_null=True), nullable=True)
    ips = Column(JSON(none_as_null=True), nullable=True)
    instanceId = Column(String(255), nullable=True)
    sessionId = Column(String(64), nullable=True)
