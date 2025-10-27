from typing import Optional, List
from .db import SessionLocal
from .models import World

def get_worlds() -> List[World]:
    with SessionLocal() as session:
        return session.query(World).all()

def get_world(world_id: int) -> Optional[World]:
    with SessionLocal() as session:
        return session.query(World).filter(World.id == world_id).first()

def create_world(name: str, s3URL: str, status: str = "stopped", params: dict = {}, domainPrefix: str = None) -> World:
    with SessionLocal() as session:
        world = World(name=name, s3URL=s3URL, status=status, params=params, domainPrefix=domainPrefix)
        session.add(world)
        session.commit()
        session.refresh(world)
        return world

def update_world(world_id: int, **kwargs) -> World:
    with SessionLocal() as session:
        world = session.query(World).filter(World.id == world_id).first()
        if not world:
            raise ValueError("World not found")
        for key, value in kwargs.items():
            if hasattr(world, key):
                setattr(world, key, value)
            else:
                raise ValueError(f"Invalid attribute: {key}")
        session.commit()
        session.refresh(world)
        return world

def delete_world(world_id: int) -> bool:
    with SessionLocal() as session:
        world = session.query(World).filter(World.id == world_id).first()
        if not world:
            return False
        session.delete(world)
        session.commit()
        return True 

def add_admin(world_id: int, admin_id: str) -> bool:
    with SessionLocal() as session:
        world = session.query(World).filter(World.id == world_id).first()
        if not world:
            return False
        world.admins.append(admin_id)
        session.commit()
        return True
    
def add_player(world_id: int, player_id: str) -> bool:
    with SessionLocal() as session:
        world = session.query(World).filter(World.id == world_id).first()
        if not world:
            return False
        world.players.append(player_id)
        session.commit()
        return True
    
def remove_admin(world_id: int, admin_id: str) -> bool:
    with SessionLocal() as session:
        world = session.query(World).filter(World.id == world_id).first()
        if not world:
            return False
        world.admins.remove(admin_id)
        session.commit()
        return True
    
def remove_player(world_id: int, player_id: str) -> bool:
    with SessionLocal() as session:
        world = session.query(World).filter(World.id == world_id).first()
        if not world:
            return False
        world.players.remove(player_id)
        session.commit()
        return True