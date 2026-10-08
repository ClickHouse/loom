"""Members fixture: a Flask blueprint and aiohttp registrations."""
from aiohttp import web
from flask import Blueprint

bp = Blueprint("admin", __name__, url_prefix="/admin")


@bp.route("/users", methods=["GET", "POST"])
def users():
    return []


@bp.get("/ping")
def ping():
    return "pong"


async def health(request):
    return web.Response(text="ok")


routes = [web.get("/health", health)]
