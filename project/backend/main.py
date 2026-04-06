import base64
import os
import re
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from dotenv import load_dotenv
from databases import Database
from fastapi import Depends, FastAPI, Header, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "")
# GET /photos requires header X-Gallery-Password. Override with GALLERY_PASSWORD env (e.g. Railway).
GALLERY_PASSWORD = (os.getenv("GALLERY_PASSWORD") or "Dyar1129@dyarm").strip()
database = Database(DATABASE_URL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set in environment")
    await database.connect()
    await database.execute(
        """
        CREATE TABLE IF NOT EXISTS photos (
            id UUID PRIMARY KEY,
            image TEXT NOT NULL,
            "timestamp" TIMESTAMPTZ NOT NULL
        );
        """
    )
    await database.execute(
        """
        ALTER TABLE photos ADD COLUMN IF NOT EXISTS mime_type TEXT DEFAULT 'image/jpeg';
        """
    )
    await database.execute(
        """
        ALTER TABLE photos ADD COLUMN IF NOT EXISTS latitude DOUBLE PRECISION;
        """
    )
    await database.execute(
        """
        ALTER TABLE photos ADD COLUMN IF NOT EXISTS longitude DOUBLE PRECISION;
        """
    )
    await database.execute(
        """
        ALTER TABLE photos ADD COLUMN IF NOT EXISTS accuracy_m DOUBLE PRECISION;
        """
    )
    yield
    await database.disconnect()


app = FastAPI(title="Photo Capture API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


ALLOWED_MIME = frozenset({"image/jpeg", "image/webp", "image/png"})


class UploadPhotoBody(BaseModel):
    image: str = Field(..., description="Base64-encoded image (optionally data URL)")
    mime_type: str | None = Field(
        None, description="MIME type of the image (e.g. image/webp)"
    )
    latitude: float | None = Field(None, description="WGS84 latitude")
    longitude: float | None = Field(None, description="WGS84 longitude")
    accuracy_m: float | None = Field(None, description="Horizontal accuracy in meters")


def validate_location(lat: float | None, lon: float | None, acc: float | None) -> None:
    if lat is None and lon is None and acc is None:
        return
    if lat is None or lon is None:
        raise HTTPException(
            status_code=400, detail="latitude and longitude must both be set or both omitted"
        )
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        raise HTTPException(status_code=400, detail="Invalid coordinates")
    if acc is not None and (acc < 0 or acc > 1_000_000):
        raise HTTPException(status_code=400, detail="Invalid accuracy value")


def require_gallery_password(
    x_gallery_password: str | None = Header(None, alias="X-Gallery-Password"),
) -> None:
    if not x_gallery_password or x_gallery_password != GALLERY_PASSWORD:
        raise HTTPException(status_code=401, detail="Invalid gallery password")


def strip_data_url(b64: str) -> str:
    s = b64.strip()
    if s.startswith("data:"):
        m = re.match(r"data:image/[^;]+;base64,(.+)", s, re.DOTALL)
        if m:
            return m.group(1)
    return s


def guess_mime_from_bytes(data: bytes) -> str | None:
    if len(data) >= 3 and data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if len(data) >= 8 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    return None


def validate_base64(raw: str) -> str:
    pad = len(raw) % 4
    if pad:
        raw += "=" * (4 - pad)
    try:
        try:
            data = base64.b64decode(raw, validate=True)
        except TypeError:
            data = base64.b64decode(raw)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid base64 image data")
    if len(data) > 15 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Image too large (max 15MB)")
    return raw


def resolve_mime_type(raw_b64: str, declared: str | None) -> str:
    try:
        pad = len(raw_b64) % 4
        b64 = raw_b64 + ("=" * (4 - pad)) if pad else raw_b64
        data = base64.b64decode(b64, validate=True)
    except Exception:
        data = base64.b64decode(raw_b64)
    guessed = guess_mime_from_bytes(data)
    if declared and declared in ALLOWED_MIME:
        if guessed and guessed != declared:
            return guessed
        return declared
    if guessed and guessed in ALLOWED_MIME:
        return guessed
    return "image/jpeg"


def utc_folder_label(ts: datetime) -> tuple[str, str]:
    """Return (display label DD/M/YYYY UTC, sort key YYYY-MM-DD)."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    u = ts.astimezone(timezone.utc)
    label = f"{u.day}/{u.month}/{u.year}"
    sort_key = f"{u.year:04d}-{u.month:02d}-{u.day:02d}"
    return label, sort_key


@app.get("/")
async def root():
    return {"status": "ok", "service": "photo-capture-api"}


@app.post("/upload-photo")
async def upload_photo(body: UploadPhotoBody):
    raw = strip_data_url(body.image)
    raw = validate_base64(raw)
    mime = resolve_mime_type(raw, body.mime_type)
    validate_location(body.latitude, body.longitude, body.accuracy_m)

    photo_id = uuid.uuid4()
    ts = datetime.now(timezone.utc)

    await database.execute(
        """
        INSERT INTO photos (id, image, "timestamp", mime_type, latitude, longitude, accuracy_m)
        VALUES (:id, :image, :ts, :mime, :lat, :lon, :acc)
        """,
        {
            "id": photo_id,
            "image": raw,
            "ts": ts,
            "mime": mime,
            "lat": body.latitude,
            "lon": body.longitude,
            "acc": body.accuracy_m,
        },
    )

    return {
        "id": str(photo_id),
        "image": raw,
        "mime_type": mime,
        "timestamp": ts.isoformat(),
        "latitude": body.latitude,
        "longitude": body.longitude,
        "accuracy_m": body.accuracy_m,
    }


@app.get("/photos")
async def list_photos(
    response: Response, _: None = Depends(require_gallery_password)
):
    rows = await database.fetch_all(
        """
        SELECT id, image, "timestamp" AS ts, mime_type,
               latitude, longitude, accuracy_m
        FROM photos
        ORDER BY "timestamp" DESC
        """
    )
    by_folder: dict[str, list[dict]] = defaultdict(list)
    folder_meta: dict[str, str] = {}

    for row in rows:
        ts = row["ts"]
        if not isinstance(ts, datetime):
            ts = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        ts_out = ts.isoformat() if isinstance(ts, datetime) else str(ts)
        mime = row["mime_type"] if row["mime_type"] else "image/jpeg"
        label, sort_key = utc_folder_label(ts)
        folder_meta[label] = sort_key
        lat = row["latitude"]
        lon = row["longitude"]
        acc = row["accuracy_m"]
        photo = {
            "id": str(row["id"]),
            "image": row["image"],
            "mime_type": mime,
            "timestamp": ts_out,
            "latitude": float(lat) if lat is not None else None,
            "longitude": float(lon) if lon is not None else None,
            "accuracy_m": float(acc) if acc is not None else None,
        }
        by_folder[label].append(photo)

    folder_list = []
    for label, sort_key in sorted(
        folder_meta.items(), key=lambda x: x[1], reverse=True
    ):
        pics = by_folder[label]
        pics.sort(
            key=lambda p: p["timestamp"],
            reverse=True,
        )
        folder_list.append(
            {
                "label": label,
                "sort_key": sort_key,
                "count": len(pics),
                "photos": pics,
            }
        )

    response.headers["Cache-Control"] = "private, max-age=60"
    return {"folders": folder_list}
