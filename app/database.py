import os
from typing import Optional, List
from datetime import datetime
from sqlmodel import SQLModel, Field, Relationship, create_engine

# --- MODELS ---
class RobotModule(SQLModel, table=True):
    mac_address: str = Field(primary_key=True)
    hostname: str
    ip_address: str
    last_seen: datetime
    
    # --- NEW: Quality of Life & Time Tracking ---
    alias: Optional[str] = Field(default=None)
    use_ntp: bool = Field(default=True)
    ntp_server: str = Field(default="pool.ntp.org")
    
    selector_type: str = Field(default="UNKNOWN")
    camera_type: str = Field(default="UNKNOWN")
    experiment_runs: List["ExperimentRun"] = Relationship(back_populates="module")

class FleetCohort(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    launched_at: datetime = Field(default_factory=datetime.utcnow)
    interval_minutes: int
    ir_enabled: bool
    experiment_runs: List["ExperimentRun"] = Relationship(back_populates="cohort")

class ExperimentRun(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    cohort_id: int = Field(foreign_key="fleetcohort.id")
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
    
    cohort: Optional[FleetCohort] = Relationship(back_populates="experiment_runs")
    module: Optional[RobotModule] = Relationship(back_populates="experiment_runs")

# --- ENGINE ---
DB_PATH = "/data/fleet_data.db" if os.path.exists("/data") else "./fleet_data.db"
engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})

def create_db_and_tables():
    SQLModel.metadata.create_all(engine)