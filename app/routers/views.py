import os
import markdown
from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates
from sqlmodel import Session

from app.database import engine, RobotModule
from app.core.state import LIVE_FLEET_STATE, resolve_proxy_target, normalize_mac

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _remote_context(mac: str, dest: str):
    norm_mac = normalize_mac(mac)
    if norm_mac in LIVE_FLEET_STATE:
        node = LIVE_FLEET_STATE[norm_mac]
        raw_hostname = node["identity"]["hostname"]
        with Session(engine) as session:
            db_mod = session.get(RobotModule, norm_mac) or session.get(RobotModule, mac)
            identifier = db_mod.alias if (db_mod and db_mod.alias) else raw_hostname
        return {
            "mac": norm_mac,
            "identifier": identifier,
            "is_offline": False,
            "dest": dest,
            "proxy_warning": None,
        }

    db_ip = None
    db_last_seen = None
    with Session(engine) as session:
        db_mod = session.get(RobotModule, norm_mac) or session.get(RobotModule, mac)
        if db_mod:
            db_ip = db_mod.ip_address
            db_last_seen = db_mod.last_seen

    ip, warning = resolve_proxy_target(norm_mac, db_ip, db_last_seen)
    if ip:
        with Session(engine) as session:
            db_mod = session.get(RobotModule, norm_mac) or session.get(RobotModule, mac)
            identifier = (
                db_mod.alias
                if (db_mod and db_mod.alias)
                else (db_mod.hostname if db_mod else norm_mac)
            )
        return {
            "mac": norm_mac,
            "identifier": identifier,
            "is_offline": False,
            "dest": dest,
            "proxy_warning": warning,
        }

    return {
        "mac": norm_mac,
        "identifier": "Offline Module",
        "is_offline": True,
        "dest": dest,
        "proxy_warning": None,
    }


@router.get("/")
async def root(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")


@router.get("/experiments")
async def view_experiments(request: Request):
    return templates.TemplateResponse(request=request, name="experiments.html")


@router.get("/remote/{mac}")
async def remote_view(request: Request, mac: str, dest: str = ""):
    ctx = _remote_context(mac, dest)
    return templates.TemplateResponse(request=request, name="remote.html", context=ctx)


@router.get("/about")
async def about_page(request: Request):
    about_path = os.path.join("app", "doc", "about.md")
    desc_path = os.path.join("app", "doc", "description.md")

    def render_md(filepath):
        if os.path.exists(filepath):
            with open(filepath, "r", encoding="utf-8") as f:
                return markdown.markdown(f.read(), extensions=["fenced_code", "tables"])
        return f"<p class='text-danger'>Documentation file not found at {filepath}.</p>"

    return templates.TemplateResponse(
        request=request,
        name="about.html",
        context={
            "about_content": render_md(about_path),
            "desc_content": render_md(desc_path),
        },
    )
