"""Members fixture: FastAPI routes on a prefixed router."""
from fastapi import APIRouter, FastAPI

from ..models import Point, Store

app = FastAPI()
router = APIRouter(prefix="/points")
store = Store("mem")


@router.get("/{point_id}")
async def read_point(point_id: int) -> Point:
    return store.query_all("SELECT 1")[0]


@router.post("/", status_code=201)
async def create_point(p: Point) -> None:
    store.add(p)


@app.api_route("/health", methods=["GET", "HEAD"])
def health() -> dict[str, str]:
    return {"ok": "yes"}


@app.websocket("/ws")
async def ws() -> None: ...


app.include_router(router, prefix="/api")
