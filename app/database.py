import os
from typing import Optional, List
from datetime import datetime
from sqlalchemy import inspect, text
from sqlmodel import SQLModel, Field, Relationship, create_engine, Session, select

# --- MODELS ---
class RobotModule(SQLModel, table=True):
    mac_address: str = Field(primary_key=True)
    hostname: str
    ip_address: str
    last_seen: datetime
    
    # --- Quality of Life & Time Tracking ---
    alias: Optional[str] = Field(default=None)
    description: Optional[str] = Field(default=None) 
    use_ntp: bool = Field(default=False)
    ntp_server: str = Field(default="pool.ntp.org")
    
    selector_type: str = Field(default="UNKNOWN")
    camera_type: str = Field(default="UNKNOWN")

    # Last-known sync identity (Pi never returns these; password stays on the module)
    sync_remote_type: Optional[str] = Field(default=None)
    sync_host: Optional[str] = Field(default=None)
    sync_port: Optional[int] = Field(default=None)
    sync_user: Optional[str] = Field(default=None)
    sync_destination: Optional[str] = Field(default=None)
    sync_interval: Optional[int] = Field(default=None)

    experiment_runs: List["ExperimentRun"] = Relationship(back_populates="module")

class ExperimentalBatch(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    launched_at: datetime = Field(default_factory=datetime.utcnow)
    interval_minutes: int
    ir_enabled: bool
    
    experiment_runs: List["ExperimentRun"] = Relationship(
        back_populates="batch",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"}
    )

class ExperimentRun(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    batch_id: int = Field(foreign_key="experimentalbatch.id")
    module_mac: str = Field(foreign_key="robotmodule.mac_address")
    local_exp_id: str
    status: str = Field(default="RUNNING")
    
    # --- YIELD METRICS ---
    expected_total: int
    taken_so_far: int = Field(default=0)
    missed_frames: int = Field(default=0)
    
    # --- NEW: EXACT TIMESTAMPS & MESSAGES ---
    start_time: str = Field(default="")
    end_time: str = Field(default="")
    message: str = Field(default="")
    
    batch: Optional[ExperimentalBatch] = Relationship(back_populates="experiment_runs")
    module: Optional[RobotModule] = Relationship(back_populates="experiment_runs")

# --- ENGINE ---
DB_PATH = "/data/fleet_data.db" if os.path.exists("/data") else "./fleet_data.db"
engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})

def create_db_and_tables():
    SQLModel.metadata.create_all(engine)
    table_name = RobotModule.__tablename__
    existing = {col["name"] for col in inspect(engine).get_columns(table_name)}
    with engine.begin() as conn:
        for name, coltype in (
            ("sync_remote_type", "VARCHAR"),
            ("sync_host", "VARCHAR"),
            ("sync_port", "INTEGER"),
            ("sync_user", "VARCHAR"),
            ("sync_destination", "VARCHAR"),
            ("sync_interval", "INTEGER"),
        ):
            if name not in existing:
                conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {name} {coltype}"))


def find_robot_module(session: Session, mac: str) -> Optional[RobotModule]:
    """Case-insensitive MAC lookup (legacy DB rows may use mixed case PKs)."""
    from app.core.state import normalize_mac

    norm = normalize_mac(mac)
    mod = session.get(RobotModule, norm)
    if mod:
        return mod
    mod = session.get(RobotModule, mac.strip())
    if mod:
        return mod
    for m in session.exec(select(RobotModule)).all():
        if normalize_mac(m.mac_address) == norm:
            return m
    return None