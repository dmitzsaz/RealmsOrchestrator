from sqlalchemy import create_engine, inspect, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from config import settings

SQLALCHEMY_DATABASE_URL = \
    (f"mysql+pymysql://{settings.DB_USER_NAME}:{settings.DB_ROOT_PASSWORD.replace('@', '%40')}"
     f"@{settings.DB_HOST}:{settings.DB_PORT}/{settings.DB_NAME}")

engine = create_engine(SQLALCHEMY_DATABASE_URL,
                       pool_pre_ping=True,
                       pool_recycle=300)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

# create_all() never alters existing tables, so columns added after the first
# deployment are created here.
WORLD_COLUMNS = {
    "provider": "VARCHAR(64) NULL",
    "ports": "JSON NULL",
    "ips": "JSON NULL",
    "instanceId": "VARCHAR(255) NULL",
    "sessionId": "VARCHAR(64) NULL",
}


def migrate_worlds_table():
    existing = {column["name"] for column in inspect(engine).get_columns("worlds")}
    with engine.begin() as conn:
        for name, ddl in WORLD_COLUMNS.items():
            if name not in existing:
                conn.execute(text(f"ALTER TABLE worlds ADD COLUMN `{name}` {ddl}"))


def create_database():
    from database import models  # noqa: F401  (registers the tables)
    Base.metadata.create_all(bind=engine)
    migrate_worlds_table()
    db = SessionLocal()
    return db
