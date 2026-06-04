from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

# Import the RAM-disk state to grab the live hostname
from app.core.state import LIVE_FLEET_STATE

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

@router.get("/")
async def root(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")

@router.get("/experiments")
async def view_experiments(request: Request):
    return templates.TemplateResponse(request=request, name="experiments.html")

# --- REMOTE WRAPPER ROUTE ---
@router.get("/remote/{mac}")
async def remote_view(request: Request, mac: str, dest: str = ""): # <-- Add dest here
    if mac not in LIVE_FLEET_STATE:
        return templates.TemplateResponse(
            request=request, 
            name="remote.html", 
            context={
                "mac": mac, 
                "hostname": "Offline Module", 
                "is_offline": True,
                "dest": dest
            }
        )
        
    node = LIVE_FLEET_STATE[mac]
    hostname = node["identity"]["hostname"]
    
    return templates.TemplateResponse(
        request=request, 
        name="remote.html", 
        context={
            "mac": mac, 
            "hostname": hostname, 
            "is_offline": False,
            "dest": dest # <-- Pass it to the template here
        }
    )