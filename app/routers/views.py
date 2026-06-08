import os
import markdown
from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates
from sqlmodel import Session

# Import the DB engine and RobotModule schema to grab the alias
from app.database import engine, RobotModule
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
async def remote_view(request: Request, mac: str, dest: str = ""):
    if mac not in LIVE_FLEET_STATE:
        return templates.TemplateResponse(
            request=request, 
            name="remote.html", 
            context={
                "mac": mac, 
                "identifier": "Offline Module", 
                "is_offline": True,
                "dest": dest
            }
        )
        
    node = LIVE_FLEET_STATE[mac]
    raw_hostname = node["identity"]["hostname"]
    
    # Check the database for a custom ID Name (alias)
    with Session(engine) as session:
        db_mod = session.get(RobotModule, mac)
        # Use the alias if it exists, otherwise fall back to the raw hostname
        identifier = db_mod.alias if (db_mod and db_mod.alias) else raw_hostname
    
    return templates.TemplateResponse(
        request=request, 
        name="remote.html", 
        context={
            "mac": mac, 
            "identifier": identifier, 
            "is_offline": False,
            "dest": dest
        }
    )
    
# --- ABOUT / DOCUMENTATION ROUTE ---
@router.get("/about")
async def about_page(request: Request):
    # Paths to the markdown files
    about_path = os.path.join("app", "doc", "about.md")
    desc_path = os.path.join("app", "doc", "description.md")

    # Helper function to read and convert Markdown to HTML
    def render_md(filepath):
        if os.path.exists(filepath):
            with open(filepath, "r", encoding="utf-8") as f:
                # We include extensions for code blocks and tables
                return markdown.markdown(f.read(), extensions=["fenced_code", "tables"])
        return f"<p class='text-danger'>Documentation file not found at {filepath}.</p>"

    about_html = render_md(about_path)
    desc_html = render_md(desc_path)

    return templates.TemplateResponse(
        request=request, 
        name="about.html", 
        context={
            "about_content": about_html,
            "desc_content": desc_html
        }
    )