import asyncio
from contextlib import asynccontextmanager, suppress
from fastapi import FastAPI, APIRouter, Query, HTTPException, Depends, Request
from siwe import SiweMessage
from urllib.parse import urlparse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ASCENDING, DESCENDING, IndexModel, ReturnDocument
from pymongo.errors import DuplicateKeyError
import os
import re
import json
import time
import httpx
import secrets
import jwt
import logging
from pathlib import Path
from pydantic import BaseModel, Field, ConfigDict
from typing import List, Optional
import uuid
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR


ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")
load_dotenv(ROOT_DIR.parent / ".env")

# Connect on first use so `vercel build` can import this module before env vars exist.
client: AsyncIOMotorClient | None = None
_db = None


def get_db():
    global client, _db
    if _db is None:
        mongo_url = os.environ.get("MONGO_URL")
        db_name = os.environ.get("DB_NAME")
        if not mongo_url or not db_name:
            raise RuntimeError("MONGO_URL and DB_NAME must be set")
        client = AsyncIOMotorClient(mongo_url)
        _db = client[db_name]
    return _db

# One entry per collection: every field a route filters, sorts or dedupes on. Add the index in the same edit as the query.
INDEXES: dict[str, list[IndexModel]] = {
    "status_checks": [
        IndexModel([("id", ASCENDING)], name="id", unique=True),
        IndexModel([("timestamp", DESCENDING)], name="timestamp_desc"),
    ],
    "players": [
        IndexModel([("nick_key", ASCENDING)], name="nick_key", unique=True),
        IndexModel([("last_seen", DESCENDING)], name="last_seen_desc"),
        IndexModel([("created_at", DESCENDING)], name="created_at_desc"),
        IndexModel([("ref_code", ASCENDING)], name="ref_code", unique=True, partialFilterExpression={"ref_code": {"$type": "string"}}),
        IndexModel([("evm_key", ASCENDING)], name="evm_key", unique=True, partialFilterExpression={"evm_key": {"$type": "string"}}),
        IndexModel([("invites", DESCENDING)], name="invites_desc"),
    ],
    "settings": [
        IndexModel([("key", ASCENDING)], name="key", unique=True),
    ],
    "nonces": [
        IndexModel([("nonce", ASCENDING)], name="nonce", unique=True),
        IndexModel([("expires_at", ASCENDING)], name="expires_at_ttl", expireAfterSeconds=0),
    ],
    "game_players": [
        IndexModel([("address", ASCENDING)], name="address", unique=True),
    ],
    "game_expansions": [
        IndexModel([("address", ASCENDING), ("index", ASCENDING)], name="address_index", unique=True),
    ],
    "game_items": [
        IndexModel([("id", ASCENDING)], name="id", unique=True),
        IndexModel([("address", ASCENDING), ("bought_at", ASCENDING)], name="address_bought_at"),
        IndexModel([("address", ASCENDING), ("level", ASCENDING), ("slot", ASCENDING)], name="address_level_slot", unique=True, partialFilterExpression={"slot": {"$type": "int"}}),
        IndexModel([("address", ASCENDING), ("boost_nft", ASCENDING)], name="address_boost_nft", unique=True, partialFilterExpression={"boost_nft": {"$type": "string"}}),
    ],
    "game_holdings": [
        IndexModel([("address", ASCENDING)], name="address", unique=True),
    ],
    "game_orders": [
        IndexModel([("id", ASCENDING)], name="id", unique=True),
    ],
    "game_payments": [
        IndexModel([("tx_hash", ASCENDING)], name="tx_hash", unique=True),
    ],
    "game_claims": [
        IndexModel([("id", ASCENDING)], name="id", unique=True),
        IndexModel([("tx_hash", ASCENDING)], name="tx_hash", unique=True, partialFilterExpression={"tx_hash": {"$type": "string"}}),
        IndexModel([("address", ASCENDING), ("created_at", DESCENDING)], name="address_created_at"),
        IndexModel([("created_at", DESCENDING)], name="created_at_desc"),
    ],
}


async def ensure_indexes() -> None:
    database = get_db()
    for collection, models in INDEXES.items():
        for model in models:  # one at a time so a bad spec skips only itself
            try:
                await database[collection].create_indexes([model])
            except Exception as exc:  # never block boot on an index; the log line names what to fix
                logging.getLogger(__name__).error("ensure_indexes(%s.%s): %s", collection, model.document["name"], exc)

@asynccontextmanager
async def lifespan(_app: FastAPI):
    index_task = asyncio.create_task(ensure_indexes())
    yield
    index_task.cancel()
    with suppress(asyncio.CancelledError):
        await index_task
    if client is not None:
        client.close()


# Create the main app without a prefix
app = FastAPI(lifespan=lifespan)

# Create a router with the /api prefix
api_router = APIRouter(prefix="/api")


# Define Models
class StatusCheck(BaseModel):
    model_config = ConfigDict(extra="ignore")  # Ignore MongoDB's _id field
    
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    client_name: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class StatusCheckCreate(BaseModel):
    client_name: str

# Add your routes to the router instead of directly to app
@api_router.get("/")
async def root():
    return {"message": "Hello World"}

@api_router.post("/status", response_model=StatusCheck)
async def create_status_check(input: StatusCheckCreate):
    status_dict = input.model_dump()
    status_obj = StatusCheck(**status_dict)
    
    # Convert to dict and serialize datetime to ISO string for MongoDB
    doc = status_obj.model_dump()
    doc['timestamp'] = doc['timestamp'].isoformat()
    
    _ = await get_db().status_checks.insert_one(doc)
    return status_obj

@api_router.get("/status", response_model=List[StatusCheck])
async def get_status_checks(limit: int = Query(20, ge=1, le=100)):
    # Exclude MongoDB's _id field from the query results
    status_checks = await get_db().status_checks.find({}, {"_id": 0}).sort("timestamp", -1).limit(limit).to_list(limit)
    
    # Convert ISO string timestamps back to datetime objects
    for check in status_checks:
        if isinstance(check['timestamp'], str):
            check['timestamp'] = datetime.fromisoformat(check['timestamp'])
    
    return status_checks

NICK_RE = re.compile(r"^[A-Za-z0-9_]{2,12}$")
EVM_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
REF_RE = re.compile(r"^PRH\d{5}$")
POINTS_PER_INVITE = 10
QUESTS = ("follow", "rt", "like", "share")
DEFAULT_SETTINGS = {
    "follow_url": "https://x.com/potionrh",
    "rt_url": "https://x.com/potionrh",
    "like_url": "https://x.com/potionrh",
    "share_url": "https://x.com/potionrh",
    "share_text": "Grabbing my potion at PotionRH 🧪",
}
JWT_ALGORITHM = "HS256"
bearer = HTTPBearer(auto_error=False)


def empty_progress() -> dict:
    return {q: False for q in QUESTS}


class PlayerCreate(BaseModel):
    nick: str
    evm_address: Optional[str] = None
    referral_code: Optional[str] = None


class Progress(BaseModel):
    follow: bool = False
    rt: bool = False
    like: bool = False
    share: bool = False


class Player(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    nick: str
    avatar_url: str
    visits: int
    created_at: str
    last_seen: str
    evm_address: Optional[str] = None
    ref_code: Optional[str] = None
    referred_by: Optional[str] = None
    invites: int = 0
    points: int = 0
    progress: Progress = Field(default_factory=Progress)


class Settings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    follow_url: str
    rt_url: str
    like_url: str
    share_url: str
    share_text: str


class AdminLogin(BaseModel):
    password: str


def require_admin(creds: HTTPAuthorizationCredentials | None = Depends(bearer)) -> None:
    if creds is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = jwt.decode(creds.credentials, os.environ["JWT_SECRET"], algorithms=[JWT_ALGORITHM])
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    if payload.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Forbidden")


async def load_settings() -> dict:
    doc = await get_db().settings.find_one({"key": "main"}, {"_id": 0, "key": 0})
    return {**DEFAULT_SETTINGS, **(doc or {})}


def to_player(doc: dict) -> Player:
    invites = doc.get("invites", 0)
    return Player(**{**doc, "invites": invites, "points": invites * POINTS_PER_INVITE})


async def find_wallet_owner(evm_key: str) -> dict | None:
    return await get_db().players.find_one(
        {"$or": [
            {"evm_key": evm_key},
            {"evm_address": {"$regex": "^" + re.escape(evm_key) + "$", "$options": "i"}},
        ]},
        {"_id": 0, "nick_key": 1},
    )


async def ensure_ref_code(key: str) -> dict:
    while True:
        code = f"PRH{secrets.randbelow(100000):05d}"
        try:
            doc = await get_db().players.find_one_and_update(
                {"nick_key": key, "ref_code": {"$exists": False}},
                {"$set": {"ref_code": code}},
                return_document=ReturnDocument.AFTER,
                projection={"_id": 0},
            )
        except DuplicateKeyError:
            continue
        return doc or await get_db().players.find_one({"nick_key": key}, {"_id": 0})


@api_router.post("/players", response_model=Player)
async def register_player(input: PlayerCreate):
    nick = input.nick.strip().lstrip("@")
    if not NICK_RE.match(nick):
        raise HTTPException(status_code=422, detail="Username must be 2-12 letters, digits or _")
    key = nick.lower()
    now = datetime.now(timezone.utc).isoformat()
    evm_key = None
    if input.evm_address is not None:
        evm = input.evm_address.strip()
        if not EVM_RE.match(evm):
            raise HTTPException(status_code=422, detail="EVM address must be 0x followed by 40 hex characters")
        evm_key = evm.lower()
    ref = (input.referral_code or "").strip().upper()
    if ref:
        if not REF_RE.match(ref):
            raise HTTPException(status_code=422, detail="Referral code must look like PRH12345")
        referrer = await get_db().players.find_one({"ref_code": ref}, {"_id": 0, "nick_key": 1})
        if not referrer:
            raise HTTPException(status_code=422, detail="Referral code not found")
        if referrer["nick_key"] == key:
            raise HTTPException(status_code=422, detail="You cannot use your own code")
    existing = await get_db().players.find_one({"nick_key": key}, {"_id": 0})
    if evm_key:
        owner = await find_wallet_owner(evm_key)
        if owner and owner["nick_key"] != key:
            raise HTTPException(status_code=409, detail="Wallet already registered")
        if existing and existing.get("evm_address"):
            raise HTTPException(status_code=409, detail="Username already registered")
    if existing:
        updates = {"last_seen": now}
        if evm_key and not existing.get("evm_address"):
            updates["evm_address"] = evm_key
            updates["evm_key"] = evm_key
        try:
            doc = await get_db().players.find_one_and_update(
                {"nick_key": key},
                {"$set": updates, "$inc": {"visits": 1}},
                return_document=ReturnDocument.AFTER,
                projection={"_id": 0},
            )
        except DuplicateKeyError:
            raise HTTPException(status_code=409, detail="Wallet already registered")
    else:
        doc = {
            "id": str(uuid.uuid4()),
            "nick": nick,
            "nick_key": key,
            "avatar_url": f"https://unavatar.io/x/{key}",
            "created_at": now,
            "last_seen": now,
            "visits": 1,
            "invites": 0,
            "progress": empty_progress(),
        }
        if evm_key:
            doc["evm_address"] = evm_key
            doc["evm_key"] = evm_key
        try:
            await get_db().players.insert_one(doc)
        except DuplicateKeyError as exc:
            detail = "Wallet already registered" if "evm_key" in str(exc) else "Username already registered"
            raise HTTPException(status_code=409, detail=detail)
        doc.pop("_id", None)
    if not doc.get("ref_code"):
        doc = await ensure_ref_code(key)
    if ref and evm_key:
        claimed = await get_db().players.find_one_and_update(
            {"nick_key": key, "referred_by": {"$exists": False}},
            {"$set": {"referred_by": ref}},
            return_document=ReturnDocument.AFTER,
            projection={"_id": 0},
        )
        if claimed:
            await get_db().players.update_one({"ref_code": ref}, {"$inc": {"invites": 1}})
            doc = claimed
    return to_player(doc)


@api_router.get("/allowlist")
async def allowlist():
    latest = await get_db().players.find({}, {"_id": 0, "nick": 1, "avatar_url": 1, "created_at": 1}).sort("created_at", -1).limit(10).to_list(10)
    top = await get_db().players.find({"invites": {"$gt": 0}}, {"_id": 0, "nick": 1, "avatar_url": 1, "ref_code": 1, "invites": 1}).sort([("invites", -1), ("created_at", 1)]).limit(10).to_list(10)
    for t in top:
        t["points"] = t["invites"] * POINTS_PER_INVITE
    return {"latest": latest, "leaders": top}


@api_router.get("/allowlist/count")
async def allowlist_count():
    return {"count": await get_db().players.count_documents({})}


@api_router.get("/allowlist/{nick}")
async def allowlist_find(nick: str):
    key = nick.lower().lstrip("@").strip()
    if not re.fullmatch(r"[a-z0-9_]{2,12}", key):
        raise HTTPException(status_code=422, detail="Enter a username")
    doc = await get_db().players.find_one(
        {"nick_key": key},
        {"_id": 0, "nick": 1, "avatar_url": 1, "created_at": 1, "ref_code": 1, "invites": 1},
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Not on the allow list")
    place = await get_db().players.count_documents({"created_at": {"$lt": doc.get("created_at", "")}}) + 1
    invites = doc.get("invites", 0)
    return {
        "nick": doc["nick"],
        "avatar_url": doc.get("avatar_url") or f"https://unavatar.io/x/{key}",
        "ref_code": doc.get("ref_code"),
        "points": invites * POINTS_PER_INVITE,
        "place": place,
    }


@api_router.get("/wallets/{address}")
async def wallet_taken(address: str):
    evm = address.strip()
    if not EVM_RE.match(evm):
        raise HTTPException(status_code=422, detail="EVM address must be 0x followed by 40 hex characters")
    if not await find_wallet_owner(evm.lower()):
        raise HTTPException(status_code=404, detail="Wallet not registered")
    return {"taken": True}


@api_router.get("/players/{nick}/invited")
async def invited_players(nick: str):
    key = nick.lower().lstrip("@").strip()
    doc = await get_db().players.find_one({"nick_key": key}, {"_id": 0, "ref_code": 1})
    if not doc or not doc.get("ref_code"):
        return {"invited": []}
    people = await get_db().players.find(
        {"referred_by": doc["ref_code"]},
        {"_id": 0, "nick": 1, "avatar_url": 1},
    ).sort("created_at", 1).limit(50).to_list(50)
    return {"invited": people}


@api_router.get("/players/{nick}", response_model=Player)
async def get_player(nick: str):
    doc = await get_db().players.find_one({"nick_key": nick.lower().lstrip("@")}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="Player not found")
    return to_player(doc)


@api_router.patch("/players/{nick}/progress", response_model=Player)
async def update_progress(nick: str, input: Progress):
    done = {f"progress.{q}": True for q in QUESTS if getattr(input, q)}
    if not done:
        return await get_player(nick)
    doc = await get_db().players.find_one_and_update(
        {"nick_key": nick.lower().lstrip("@")},
        {"$set": {**done, "last_seen": datetime.now(timezone.utc).isoformat()}},
        return_document=ReturnDocument.AFTER,
        projection={"_id": 0},
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Player not found")
    return to_player(doc)


@api_router.get("/settings", response_model=Settings)
async def get_settings():
    return Settings(**(await load_settings()))


@api_router.post("/admin/login")
async def admin_login(input: AdminLogin):
    if not secrets.compare_digest(input.password, os.environ["ADMIN_PASSWORD"]):
        raise HTTPException(status_code=401, detail="Wrong password")
    exp = datetime.now(timezone.utc) + timedelta(hours=12)
    token = jwt.encode({"role": "admin", "exp": exp}, os.environ["JWT_SECRET"], algorithm=JWT_ALGORITHM)
    return {"token": token}


@api_router.get("/admin/me", dependencies=[Depends(require_admin)])
async def admin_me():
    return {"role": "admin"}


@api_router.put("/admin/settings", response_model=Settings, dependencies=[Depends(require_admin)])
async def put_settings(input: Settings):
    await get_db().settings.update_one({"key": "main"}, {"$set": input.model_dump()}, upsert=True)
    return Settings(**(await load_settings()))


@api_router.get("/admin/players", response_model=List[Player], dependencies=[Depends(require_admin)])
async def admin_players(limit: int = Query(200, ge=1, le=1000)):
    docs = await get_db().players.find({}, {"_id": 0}).sort("last_seen", -1).limit(limit).to_list(limit)
    return [to_player(d) for d in docs]


PONS_URL_RE = re.compile(r"^https?://(?:www\.)?ponsfamily\.com/launchpad/(0x[a-fA-F0-9]{40})(?:[/?#].*)?$")
PONS_CHUNK_RE = re.compile(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)')
TOKEN_CACHE_SECONDS = 25
_token_cache: dict = {"at": 0.0, "address": None, "data": None}


class TokenLink(BaseModel):
    url: str


def pons_address(url: str) -> str:
    match = PONS_URL_RE.match(url.strip())
    if not match:
        raise HTTPException(status_code=400, detail="Paste a ponsfamily.com token page link (https://ponsfamily.com/launchpad/0x...)")
    return match.group(1).lower()


def pons_image(artwork: str | None) -> str | None:
    if not artwork:
        return None
    if artwork.startswith("ipfs://"):
        return f"https://dbk-vercel.vercel.app/api/ipfs/content/{artwork[7:]}?variant=card"
    return artwork if artwork.startswith("https://") else None


async def fetch_pons_token(address: str) -> dict:
    url = f"https://ponsfamily.com/launchpad/{address}"
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}) as http:
            res = await http.get(url)
        res.raise_for_status()
        stream = "".join(json.loads(chunk) for chunk in PONS_CHUNK_RE.findall(res.text))
        start = stream.index('"launch":{') + len('"launch":')
        launch, _ = json.JSONDecoder().raw_decode(stream, start)
    except (httpx.HTTPError, ValueError) as exc:
        logging.getLogger(__name__).warning("fetch_pons_token(%s): %s", address, exc)
        raise HTTPException(status_code=422, detail="Could not read token details from that ponsfamily.com page")
    return {
        "address": address,
        "url": url,
        "name": launch.get("name"),
        "symbol": launch.get("symbol"),
        "image": pons_image(launch.get("artwork")),
        "stage": launch.get("stage"),
        "price_usd": launch.get("priceUsd"),
        "market_cap_usd": launch.get("marketCapUsd"),
        "volume_usd": launch.get("volumeUsd"),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


async def saved_token() -> dict | None:
    return await get_db().settings.find_one({"key": "token"}, {"_id": 0, "key": 0})


async def live_token() -> dict | None:
    saved = await saved_token()
    if not saved:
        return None
    if _token_cache["address"] == saved["address"] and time.monotonic() - _token_cache["at"] < TOKEN_CACHE_SECONDS:
        return _token_cache["data"]
    try:
        data = await fetch_pons_token(saved["address"])
        await get_db().settings.update_one({"key": "token"}, {"$set": data})
    except HTTPException:
        data = saved  # last known values, updated_at shows their age
    _token_cache.update(at=time.monotonic(), address=saved["address"], data=data)
    return data


@api_router.get("/token")
async def get_token():
    return {"token": await live_token()}


@api_router.put("/admin/token", dependencies=[Depends(require_admin)])
async def put_token(input: TokenLink):
    data = await fetch_pons_token(pons_address(input.url))
    await get_db().settings.replace_one({"key": "token"}, {"key": "token", **data}, upsert=True)
    _token_cache.update(at=time.monotonic(), address=data["address"], data=data)
    return {"token": data}


@api_router.delete("/admin/token", dependencies=[Depends(require_admin)])
async def delete_token():
    await get_db().settings.delete_one({"key": "token"})
    _token_cache.update(at=0.0, address=None, data=None)
    return {"token": None}


CHAIN_ID = 4663
SESSION_DAYS = 7
PAYBACK_MINUTES = 24 * 60
ORDER_MINUTES = 15
CLAIM_HOURS = 12
PRICE_MAX_AGE_SECONDS = 300
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
TX_RE = re.compile(r"^0x[a-fA-F0-9]{64}$")
LEVELS = {
    1: {"name": "Mine Worker", "price": 1, "max_placed": 16},
    2: {"name": "Pump Worker", "price": 10, "max_placed": 12},
    3: {"name": "Small Drill Rig", "price": 50, "max_placed": 6},
    4: {"name": "Big Drill Rig", "price": 100, "max_placed": 4},
    5: {"name": "Hose Extractor", "price": 200, "max_placed": 8},
    6: {"name": "Potion Factory", "price": 400, "max_placed": 4},
}


EXTRA_PER_EXPANSION = {1: 4, 2: 4, 3: 2, 4: 1, 5: 2, 6: 1}
EXPANSION_PRICE = 2
MAX_EXPANSIONS = 4
_decimals: dict[str, int] = {}


def treasury() -> str:
    return os.environ["TREASURY_ADDRESS"].lower()


def usd_per_min(level: int) -> float:
    return LEVELS[level]["price"] / PAYBACK_MINUTES


def capacity(level: int, expansions: int) -> int:
    return LEVELS[level]["max_placed"] + expansions * EXTRA_PER_EXPANSION[level]


async def rpc(method: str, params: list):
    try:
        async with httpx.AsyncClient(timeout=15) as http:
            res = await http.post(os.environ["RPC_URL"], json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        res.raise_for_status()
        body = res.json()
    except (httpx.HTTPError, ValueError) as exc:
        logging.getLogger(__name__).warning("rpc(%s): %s", method, exc)
        raise HTTPException(status_code=503, detail="Robinhood Chain is not answering, try again")
    if body.get("error"):
        logging.getLogger(__name__).warning("rpc(%s): %s", method, body["error"])
        raise HTTPException(status_code=503, detail="Robinhood Chain is not answering, try again")
    return body.get("result")


async def token_decimals(address: str) -> int:
    if address not in _decimals:
        raw = await rpc("eth_call", [{"to": address, "data": "0x313ce567"}, "latest"])
        if not raw or raw == "0x":
            raise HTTPException(status_code=409, detail="The game token was not found on Robinhood Chain")
        _decimals[address] = int(raw, 16)
    return _decimals[address]


async def priced_token() -> dict:
    token = await live_token()
    if not token:
        raise HTTPException(status_code=409, detail="No game token is set yet")
    price = token.get("price_usd")
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(token["updated_at"])).total_seconds()
    if not isinstance(price, (int, float)) or price <= 0 or age > PRICE_MAX_AGE_SECONDS:
        raise HTTPException(status_code=503, detail="The token price is not available right now, try again")
    return {**token, "decimals": await token_decimals(token["address"])}


def token_units(usd: float, token: dict, rounding: str) -> int:
    units = Decimal(str(usd)) / Decimal(str(token["price_usd"])) * Decimal(10) ** token["decimals"]
    return int(units.to_integral_value(rounding=rounding))


async def find_transfer(tx_hash: str, token: str, recipient: str, min_units: int, sender: str | None = None) -> int:
    """Check a confirmed ERC-20 transfer and return its block time."""
    receipt = await rpc("eth_getTransactionReceipt", [tx_hash])
    if not receipt:
        raise HTTPException(status_code=425, detail="The transaction is not confirmed yet")
    if receipt.get("status") != "0x1":
        raise HTTPException(status_code=400, detail="The transaction failed on chain")
    for log in receipt.get("logs", []):
        topics = log.get("topics", [])
        if log.get("address", "").lower() != token or len(topics) != 3 or topics[0].lower() != TRANSFER_TOPIC:
            continue
        if topics[2][-40:].lower() != recipient[2:] or (sender and topics[1][-40:].lower() != sender[2:]):
            continue
        if int(log.get("data") or "0x0", 16) >= min_units:
            block = await rpc("eth_getBlockByNumber", [receipt["blockNumber"], False])
            return int(block["timestamp"], 16)
    raise HTTPException(status_code=400, detail="The transaction does not contain the expected token transfer")


class WalletVerify(BaseModel):
    message: str
    signature: str


class LevelInput(BaseModel):
    level: int


class BuyConfirm(BaseModel):
    order_id: str
    tx_hash: str


class TxInput(BaseModel):
    tx_hash: str


def require_player(creds: HTTPAuthorizationCredentials | None = Depends(bearer)) -> str:
    if creds is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = jwt.decode(creds.credentials, os.environ["JWT_SECRET"], algorithms=[JWT_ALGORITHM])
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    if payload.get("role") != "player" or payload.get("chain_id") != CHAIN_ID or not payload.get("sub"):
        raise HTTPException(status_code=403, detail="Forbidden")
    return payload["sub"]


@api_router.get("/auth/nonce")
async def wallet_nonce(address: str):
    if not EVM_RE.match(address):
        raise HTTPException(status_code=422, detail="Invalid wallet address")
    nonce = secrets.token_hex(16)
    await get_db().nonces.insert_one({
        "nonce": nonce,
        "address": address.lower(),
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=10),
    })
    return {"nonce": nonce, "chain_id": CHAIN_ID}


@api_router.post("/auth/verify")
async def wallet_verify(input: WalletVerify, request: Request):
    try:
        msg = SiweMessage.from_message(input.message)
    except Exception:
        raise HTTPException(status_code=400, detail="Malformed sign-in message")
    if msg.chain_id != CHAIN_ID:
        raise HTTPException(status_code=403, detail="Only Robinhood Chain mainnet is accepted")
    origin = request.headers.get("origin")
    site = urlparse(str(msg.uri))
    if site.netloc != msg.domain or (origin and urlparse(origin).netloc != msg.domain):
        raise HTTPException(status_code=401, detail="Sign-in message was issued for another site")
    address = str(msg.address).lower()
    row = await get_db().nonces.find_one_and_update(
        {"nonce": msg.nonce, "address": address, "used": {"$ne": True}, "expires_at": {"$gt": datetime.now(timezone.utc)}},
        {"$set": {"used": True}},
    )
    if not row:
        raise HTTPException(status_code=401, detail="Sign-in request expired, try again")
    try:
        msg.verify(signature=input.signature, domain=msg.domain, nonce=row["nonce"], chain_id=CHAIN_ID, timestamp=datetime.now(timezone.utc))
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid wallet signature")
    now = datetime.now(timezone.utc)
    await get_db().game_players.update_one(
        {"address": address},
        {"$set": {"last_login": now.isoformat()}, "$setOnInsert": {"created_at": now.isoformat()}},
        upsert=True,
    )
    token = jwt.encode(
        {"sub": address, "role": "player", "chain_id": CHAIN_ID, "exp": now + timedelta(days=SESSION_DAYS)},
        os.environ["JWT_SECRET"],
        algorithm=JWT_ALGORITHM,
    )
    return {"token": token, "address": address}


def catalog(expansions: int = 0) -> list[dict]:
    return [{"level": lv, **spec, "per_min": usd_per_min(lv), "max_placed": capacity(lv, expansions)} for lv, spec in LEVELS.items()]


async def count_expansions(address: str) -> int:
    return await get_db().game_expansions.count_documents({"address": address})


async def load_items(address: str) -> list[dict]:
    return await get_db().game_items.find({"address": address}, {"_id": 0, "address": 0}).sort("bought_at", 1).to_list(5000)


def earns(item: dict) -> bool:
    return bool(item.get("placed")) and not item.get("demo")


OPENSEA_API = "https://api.opensea.io/api/v2"
OPENSEA_LINK_RE = re.compile(r"^https?://(?:www\.)?opensea\.io/(?:[a-z]{2}(?:-[A-Za-z]{2})?/)?(?:collection/([A-Za-z0-9][A-Za-z0-9_-]{0,127})|(?:assets|item)/([a-z0-9_-]+)/(0x[a-fA-F0-9]{40})/(\d+))(?:[/?#].*)?$")
NFT_CHECK_SECONDS = 300
NFT_PAGES = 5
MAX_BOOST = 100


async def opensea(path: str, **params):
    try:
        async with httpx.AsyncClient(timeout=15) as http:
            res = await http.get(OPENSEA_API + path, params=params, headers={"X-API-KEY": os.environ["OPENSEA_API_KEY"], "Accept": "application/json"})
    except httpx.HTTPError as exc:
        logging.getLogger(__name__).warning("opensea(%s): %s", path, exc)
        raise HTTPException(status_code=503, detail="OpenSea is not answering, try again")
    if res.status_code in (400, 404):
        raise HTTPException(status_code=404, detail="OpenSea could not find that NFT collection")
    if res.is_error:
        logging.getLogger(__name__).warning("opensea(%s): HTTP %s", path, res.status_code)
        raise HTTPException(status_code=503, detail="OpenSea is not answering, try again")
    return res.json()


async def fetch_collection(url: str) -> dict:
    match = OPENSEA_LINK_RE.match(url.strip())
    if not match:
        raise HTTPException(status_code=400, detail="Paste an opensea.io collection or NFT link (https://opensea.io/collection/...)")
    slug = match.group(1)
    if not slug:
        slug = (await opensea(f"/chain/{match.group(2)}/contract/{match.group(3).lower()}/nfts/{match.group(4)}"))["nft"]["collection"]
    info = await opensea(f"/collections/{slug}")
    raw = await opensea(f"/traits/{slug}")
    contracts = [{"chain": c["chain"], "address": c["address"].lower()} for c in info.get("contracts") or []]
    if not contracts:
        raise HTTPException(status_code=422, detail="This collection has no on-chain contract, so ownership cannot be checked")
    categories = raw.get("categories") or {}
    counts = {kind: values for kind, values in (raw.get("counts") or {}).items() if categories.get(kind) == "string" and isinstance(values, dict)}
    if not counts:  # OpenSea has not summarised the traits yet: read them off the NFTs themselves
        cursor = None
        for _ in range(NFT_PAGES):
            page = await opensea(f"/collection/{slug}/nfts", limit=200, **({"next": cursor} if cursor else {}))
            for n in page.get("nfts") or []:
                for t in n.get("traits") or []:
                    if t.get("trait_type") and not isinstance(t.get("value"), (int, float)):
                        bucket = counts.setdefault(t["trait_type"], {})
                        bucket[str(t.get("value"))] = bucket.get(str(t.get("value")), 0) + 1
            cursor = page.get("next")
            if not cursor:
                break
    traits = [
        {"type": kind, "value": str(value), "count": count}
        for kind, values in counts.items()
        for value, count in sorted(values.items(), key=lambda kv: kv[1])
    ]
    return {
        "slug": slug, "url": f"https://opensea.io/collection/{slug}", "name": info.get("name") or slug, "image": info.get("image_url") or None,
        "contracts": contracts, "total_supply": info.get("total_supply"), "traits": traits, "boosts": [],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


async def nft_config() -> dict | None:
    return await get_db().settings.find_one({"key": "nft"}, {"_id": 0, "key": 0})


async def fetch_wallet_nfts(address: str, cfg: dict) -> list[dict]:
    wanted = {c["address"] for c in cfg["contracts"]}
    found: dict[str, dict] = {}
    for chain in sorted({c["chain"] for c in cfg["contracts"]}):
        cursor = None
        for _ in range(NFT_PAGES):
            params = {"collection": cfg["slug"], "limit": 200, **({"next": cursor} if cursor else {})}
            page = await opensea(f"/chain/{chain}/account/{address}/nfts", **params)
            for n in page.get("nfts") or []:
                contract = (n.get("contract") or "").lower()
                if contract not in wanted or n.get("collection") != cfg["slug"]:
                    continue
                key = f"{chain}:{contract}:{n['identifier']}"
                found[key] = {
                    "key": key, "identifier": n["identifier"], "name": n.get("name") or f"#{n['identifier']}",
                    "image": n.get("display_image_url") or n.get("image_url"), "url": n.get("opensea_url"),
                    "traits": [{"type": t.get("trait_type"), "value": str(t.get("value"))} for t in n.get("traits") or []],
                }
            cursor = page.get("next")
            if not cursor:
                break
    return list(found.values())


async def load_holdings(address: str, cfg: dict, force: bool = False) -> dict | None:
    """The wallet's NFTs of the boost collection, re-read from OpenSea when older than NFT_CHECK_SECONDS."""
    doc = await get_db().game_holdings.find_one({"address": address, "slug": cfg["slug"]}, {"_id": 0})
    now = datetime.now(timezone.utc)
    if doc and not force and (now - datetime.fromisoformat(doc["checked_at"])).total_seconds() < NFT_CHECK_SECONDS:
        return doc
    try:
        nfts = await fetch_wallet_nfts(address, cfg)
    except HTTPException:
        if force:
            raise HTTPException(status_code=503, detail="Could not check your NFTs on OpenSea right now, try again")
        return doc
    doc = {"address": address, "slug": cfg["slug"], "nfts": nfts, "checked_at": now.isoformat()}
    await get_db().game_holdings.replace_one({"address": address}, doc, upsert=True)
    await get_db().game_items.update_many(
        {"address": address, "boost_nft": {"$type": "string", "$nin": [n["key"] for n in nfts]}}, {"$unset": {"boost_nft": ""}},
    )
    return doc


def holding_boosts(hold: dict | None, cfg: dict | None) -> dict[str, dict]:
    """Per owned NFT: its best multiplier and the trait that gives it."""
    if not hold or not cfg:
        return {}
    table = {(b["type"], b["value"]): b["multiplier"] for b in cfg.get("boosts", [])}
    out = {}
    for n in hold["nfts"]:
        mult, trait = max(((table.get((t["type"], t["value"]), 1), t) for t in n["traits"]), key=lambda p: p[0], default=(1, None))
        out[n["key"]] = {"multiplier": mult, "trait": f"{trait['type']}: {trait['value']}" if mult > 1 else None}
    return out


def boost_extra_per_min(items: list[dict], boosts: dict[str, dict]) -> float:
    return sum(usd_per_min(i["level"]) * (boosts.get(i.get("boost_nft"), {}).get("multiplier", 1) - 1) for i in items)


async def settle(address: str, add: float = 0.0, verify: bool = False) -> tuple[float, str]:
    """Bank the dollars earned since the last change; returns the balance and its timestamp.

    NFT boost extra is only banked up to the last time ownership was confirmed (boost_at)."""
    players = get_db().game_players
    items = get_db().game_items
    cfg = await nft_config()
    for _ in range(5):
        boosted = bool(cfg) and await items.count_documents({"address": address, "boost_nft": {"$type": "string"}}, limit=1) > 0
        hold = await load_holdings(address, cfg, force=verify) if boosted else None
        now = datetime.now(timezone.utc)
        player = await players.find_one({"address": address}, {"_id": 0}) or {}
        placed = await items.find({"address": address, "placed": True, "demo": {"$ne": True}}, {"_id": 0, "level": 1, "boost_nft": 1}).to_list(5000)
        per_sec = sum(usd_per_min(i["level"]) for i in placed) / 60
        at = player.get("earned_at")
        bat = player.get("boost_at")
        earned = float(player.get("earned_usd", 0)) + add
        if at:
            earned += per_sec * max(0.0, (now - datetime.fromisoformat(at)).total_seconds())
        upto = datetime.fromisoformat(hold["checked_at"]) if hold else None if boosted else now
        new_bat = bat
        if upto and (not bat or upto > datetime.fromisoformat(bat)):
            if bat:
                earned += boost_extra_per_min(placed, holding_boosts(hold, cfg)) / 60 * (upto - datetime.fromisoformat(bat)).total_seconds()
            new_bat = upto.isoformat()
        try:
            done = await players.update_one(
                {"address": address, "earned_at": at, "boost_at": bat},
                {"$set": {"earned_usd": earned, "earned_at": now.isoformat(), "boost_at": new_bat}}, upsert=at is None,
            )
        except DuplicateKeyError:
            continue
        if done.matched_count or done.upserted_id:
            return earned, now.isoformat()
    raise HTTPException(status_code=409, detail="Busy, try again")


def public_claim(claim: dict | None) -> dict | None:
    return claim and {k: claim.get(k) for k in ("id", "amount_usd", "amount_wei", "decimals", "symbol", "status", "created_at", "tx_hash")}


async def game_state(address: str) -> dict:
    earned, _ = await settle(address)
    cfg = await nft_config()
    hold = await load_holdings(address, cfg) if cfg else None
    boosts = holding_boosts(hold, cfg)
    items = await load_items(address)
    for item in items:
        item["boost"] = boosts.get(item.get("boost_nft"), {}).get("multiplier", 1)
    expansions = await count_expansions(address)
    token = await live_token()
    player = await get_db().game_players.find_one({"address": address}, {"_id": 0, "claim_at": 1, "boost_at": 1}) or {}
    working = [i for i in items if earns(i)]
    extra = boost_extra_per_min(working, boosts)
    if extra and player.get("boost_at"):
        earned += extra / 60 * max(0.0, (datetime.now(timezone.utc) - datetime.fromisoformat(player["boost_at"])).total_seconds())
    next_claim = datetime.fromisoformat(player["claim_at"]) + timedelta(hours=CLAIM_HOURS) if player.get("claim_at") else None
    last_claim = await get_db().game_claims.find_one({"address": address}, {"_id": 0}, sort=[("created_at", -1)])
    return {
        "address": address, "earned_usd": earned, "per_min": sum(usd_per_min(i["level"]) for i in working) + extra,
        "nft": cfg and {
            "name": cfg["name"], "image": cfg["image"], "url": cfg["url"], "checked": hold is not None,
            "nfts": [{k: n[k] for k in ("key", "identifier", "name", "image", "url")} | boosts[n["key"]] for n in (hold or {}).get("nfts", [])],
        },
        "items": items, "catalog": catalog(expansions),
        "expansion": {"count": expansions, "max": MAX_EXPANSIONS, "price": EXPANSION_PRICE},
        "token": token and {k: token.get(k) for k in ("address", "name", "symbol", "image", "url", "price_usd")},
        "claim": {
            "hours": CLAIM_HOURS,
            "next_at": next_claim.isoformat() if next_claim and next_claim > datetime.now(timezone.utc) else None,
            "last": public_claim(last_claim),
        },
    }


@api_router.get("/game/catalog")
async def game_catalog():
    return {"catalog": catalog(), "chain_id": CHAIN_ID}


@api_router.get("/game/state")
async def get_game_state(address: str = Depends(require_player)):
    return await game_state(address)


async def new_order(address: str, kind: str, price_usd: float, level: int | None = None) -> dict:
    token = await priced_token()
    order = {
        "id": str(uuid.uuid4()),
        "address": address,
        "kind": kind,
        "level": level,
        "price_usd": price_usd,
        "token_address": token["address"],
        "symbol": token["symbol"],
        "decimals": token["decimals"],
        "token_price_usd": token["price_usd"],
        "amount_wei": str(token_units(price_usd, token, ROUND_CEILING)),
        "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await get_db().game_orders.insert_one({**order})
    return {**order, "treasury": treasury(), "token_url": token["url"], "valid_minutes": ORDER_MINUTES}


async def paid_order(input: BuyConfirm, address: str, kind: str) -> tuple[dict, str, bool]:
    """Find the order and check its payment on chain; the flag says it was already recorded as paid."""
    tx_hash = input.tx_hash.strip().lower()
    if not TX_RE.match(tx_hash):
        raise HTTPException(status_code=422, detail="Invalid transaction hash")
    order = await get_db().game_orders.find_one({"id": input.order_id, "address": address}, {"_id": 0})
    if not order or order.get("kind", "card") != kind:
        raise HTTPException(status_code=404, detail="Purchase not found")
    if order["status"] == "paid":
        return order, tx_hash, True
    paid_at = await find_transfer(tx_hash, order["token_address"], treasury(), int(order["amount_wei"]), sender=address)
    created = datetime.fromisoformat(order["created_at"]).timestamp()
    if not created - 60 <= paid_at <= created + ORDER_MINUTES * 60:
        raise HTTPException(status_code=400, detail="This payment was not made within the price window of this purchase")
    try:
        await get_db().game_payments.insert_one({"tx_hash": tx_hash, "order_id": order["id"], "address": address, "created_at": datetime.now(timezone.utc).isoformat()})
    except DuplicateKeyError:
        used = await get_db().game_payments.find_one({"tx_hash": tx_hash}, {"_id": 0, "order_id": 1})
        if not used or used["order_id"] != order["id"]:
            raise HTTPException(status_code=409, detail="This transaction was already used for another purchase")
    return order, tx_hash, False


async def mark_paid(order: dict, tx_hash: str) -> None:
    await get_db().game_orders.update_one({"id": order["id"]}, {"$set": {"status": "paid", "tx_hash": tx_hash}})


@api_router.post("/game/buy/quote")
async def quote_character(input: LevelInput, address: str = Depends(require_player)):
    spec = LEVELS.get(input.level)
    if not spec:
        raise HTTPException(status_code=422, detail="Unknown character level")
    return await new_order(address, "card", spec["price"], input.level)


@api_router.post("/game/buy")
async def buy_character(input: BuyConfirm, address: str = Depends(require_player)):
    order, tx_hash, done = await paid_order(input, address, "card")
    if not done:
        with suppress(DuplicateKeyError):
            await get_db().game_items.insert_one({
                "id": order["id"],
                "address": address,
                "level": order["level"],
                "name": LEVELS[order["level"]]["name"],
                "price_usd": order["price_usd"],
                "token_address": order["token_address"],
                "amount_wei": order["amount_wei"],
                "tx_hash": tx_hash,
                "placed": False,
                "bought_at": datetime.now(timezone.utc).isoformat(),
            })
        await mark_paid(order, tx_hash)
    return await game_state(address)


@api_router.post("/game/claim")
async def claim_earnings(address: str = Depends(require_player)):
    token = await priced_token()
    earned, at = await settle(address, verify=True)
    now = datetime.now(timezone.utc)
    amount = token_units(earned, token, ROUND_FLOOR)
    if amount <= 0:
        raise HTTPException(status_code=409, detail="Nothing to claim yet")
    cutoff = (now - timedelta(hours=CLAIM_HOURS)).isoformat()
    taken = await get_db().game_players.update_one(
        {"address": address, "earned_at": at, "$or": [{"claim_at": None}, {"claim_at": {"$lte": cutoff}}]},
        {"$set": {"earned_usd": 0.0, "claim_at": now.isoformat()}},
    )
    if not taken.matched_count:
        raise HTTPException(status_code=429, detail=f"You can claim once every {CLAIM_HOURS} hours")
    await get_db().game_claims.insert_one({
        "id": str(uuid.uuid4()),
        "address": address,
        "amount_usd": earned,
        "token_address": token["address"],
        "symbol": token["symbol"],
        "decimals": token["decimals"],
        "token_price_usd": token["price_usd"],
        "amount_wei": str(amount),
        "status": "pending",
        "created_at": now.isoformat(),
    })
    return await game_state(address)


@api_router.get("/admin/claims", dependencies=[Depends(require_admin)])
async def admin_claims(limit: int = Query(200, ge=1, le=1000)):
    return {"claims": await get_db().game_claims.find({}, {"_id": 0}).sort("created_at", -1).limit(limit).to_list(limit)}


@api_router.post("/admin/claims/{claim_id}/paid", dependencies=[Depends(require_admin)])
async def admin_claim_paid(claim_id: str, input: TxInput):
    tx_hash = input.tx_hash.strip().lower()
    if not TX_RE.match(tx_hash):
        raise HTTPException(status_code=422, detail="Invalid transaction hash")
    claim = await get_db().game_claims.find_one({"id": claim_id}, {"_id": 0})
    if not claim:
        raise HTTPException(status_code=404, detail="Claim not found")
    if claim["status"] != "pending":
        raise HTTPException(status_code=409, detail=f"Claim is already {claim['status']}")
    paid_at = await find_transfer(tx_hash, claim["token_address"], claim["address"], int(claim["amount_wei"]))
    if paid_at < datetime.fromisoformat(claim["created_at"]).timestamp() - 60:
        raise HTTPException(status_code=400, detail="This transfer was sent before the claim was made")
    try:
        done = await get_db().game_claims.find_one_and_update(
            {"id": claim_id, "status": "pending"},
            {"$set": {"status": "paid", "tx_hash": tx_hash, "paid_at": datetime.now(timezone.utc).isoformat()}},
            return_document=ReturnDocument.AFTER, projection={"_id": 0},
        )
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="This transaction was already used for another claim")
    if not done:
        raise HTTPException(status_code=409, detail="Claim is no longer pending")
    return {"claim": done}


@api_router.post("/admin/claims/{claim_id}/reject", dependencies=[Depends(require_admin)])
async def admin_claim_reject(claim_id: str):
    done = await get_db().game_claims.find_one_and_update(
        {"id": claim_id, "status": "pending"},
        {"$set": {"status": "rejected", "rejected_at": datetime.now(timezone.utc).isoformat()}},
        return_document=ReturnDocument.AFTER, projection={"_id": 0},
    )
    if not done:
        raise HTTPException(status_code=409, detail="Claim is not pending")
    await settle(done["address"], add=done["amount_usd"])
    return {"claim": done}


@api_router.post("/game/place")
async def place_character(input: LevelInput, address: str = Depends(require_player)):
    spec = LEVELS.get(input.level)
    if not spec:
        raise HTTPException(status_code=422, detail="Unknown character level")
    items = get_db().game_items
    for _ in range(5):
        item = await items.find_one({"address": address, "level": input.level, "placed": False}, {"_id": 0, "id": 1}, sort=[("bought_at", 1)])
        if not item:
            raise HTTPException(status_code=404, detail="Nothing in the inventory to place")
        used = {i["slot"] for i in await items.find({"address": address, "level": input.level, "placed": True}, {"_id": 0, "slot": 1}).to_list(100)}
        free = next((s for s in range(capacity(input.level, await count_expansions(address))) if s not in used), None)
        if free is None:
            raise HTTPException(status_code=409, detail=f"No free spot on the island for {spec['name']}")
        await settle(address, verify=True)
        try:
            done = await items.update_one({"id": item["id"], "address": address, "placed": False}, {"$set": {"placed": True, "slot": free}})
        except DuplicateKeyError:
            continue
        if done.modified_count:
            return {**(await game_state(address)), "focus": item["id"]}
    raise HTTPException(status_code=409, detail="Could not place, try again")


@api_router.post("/game/expand/quote")
async def quote_expansion(address: str = Depends(require_player)):
    if await count_expansions(address) >= MAX_EXPANSIONS:
        raise HTTPException(status_code=409, detail="The island is already at its maximum size")
    return await new_order(address, "expansion", EXPANSION_PRICE)


@api_router.post("/game/expand")
async def expand_island(input: BuyConfirm, address: str = Depends(require_player)):
    order, tx_hash, done = await paid_order(input, address, "expansion")
    if done:
        return await game_state(address)
    expansions = get_db().game_expansions
    for _ in range(MAX_EXPANSIONS + 1):
        row = await expansions.find_one({"id": order["id"]}, {"_id": 0, "index": 1})
        index = row["index"] if row else await count_expansions(address)
        if not row:
            try:
                await expansions.insert_one({
                    "id": order["id"],
                    "address": address,
                    "index": index,
                    "price_usd": order["price_usd"],
                    "token_address": order["token_address"],
                    "amount_wei": order["amount_wei"],
                    "tx_hash": tx_hash,
                    "bought_at": datetime.now(timezone.utc).isoformat(),
                })
            except DuplicateKeyError:
                continue
        await mark_paid(order, tx_hash)
        return {**(await game_state(address)), "focus_plot": index}
    raise HTTPException(status_code=409, detail="Could not expand, try again")


LEADERBOARD_SIZE = 20


def short_address(address: str) -> str:
    return f"{address[:6]}…{address[-4:]}"


@api_router.get("/game/leaderboard")
async def game_leaderboard(address: str = Depends(require_player)):
    count_by_address = [{"$group": {"_id": "$address", "n": {"$sum": 1}}}]
    characters = {r["_id"]: r["n"] async for r in get_db().game_items.aggregate(count_by_address)}
    expansions = {r["_id"]: r["n"] async for r in get_db().game_expansions.aggregate(count_by_address)}
    ranked = sorted(
        ({"address": a, "characters": characters.get(a, 0), "expansions": expansions.get(a, 0)} for a in characters.keys() | expansions.keys()),
        key=lambda r: (-r["characters"], -r["expansions"], r["address"]),
    )
    rows = [{"rank": i + 1, "wallet": short_address(r["address"]), "characters": r["characters"], "expansions": r["expansions"], "me": r["address"] == address} for i, r in enumerate(ranked)]
    me = next((r for r in rows if r["me"]), None)
    return {"leaders": rows[:LEADERBOARD_SIZE], "me": me, "total": len(rows), "max_expansions": MAX_EXPANSIONS}


@api_router.post("/game/recall")
async def recall_character(input: LevelInput, address: str = Depends(require_player)):
    item = await get_db().game_items.find_one({"address": address, "level": input.level, "placed": True}, {"_id": 0, "id": 1}, sort=[("slot", -1)])
    if not item:
        raise HTTPException(status_code=404, detail="Nothing placed for this level")
    await settle(address, verify=True)
    await get_db().game_items.update_one({"id": item["id"], "address": address}, {"$set": {"placed": False}, "$unset": {"slot": ""}})
    return await game_state(address)


class BoostInput(BaseModel):
    item_id: str
    nft: Optional[str] = None


@api_router.post("/game/boost")
async def boost_character(input: BoostInput, address: str = Depends(require_player)):
    items = get_db().game_items
    item = await items.find_one({"id": input.item_id, "address": address}, {"_id": 0, "id": 1})
    if not item:
        raise HTTPException(status_code=404, detail="Character not found")
    cfg = await nft_config()
    if input.nft is None:
        await settle(address, verify=bool(cfg))
        await items.update_one({"id": item["id"], "address": address}, {"$unset": {"boost_nft": ""}})
        return await game_state(address)
    if not cfg:
        raise HTTPException(status_code=409, detail="No NFT collection is set yet")
    hold = await load_holdings(address, cfg, force=True)
    boost = holding_boosts(hold, cfg).get(input.nft)
    if not boost:
        raise HTTPException(status_code=403, detail="This NFT is not in your wallet")
    if boost["multiplier"] <= 1:
        raise HTTPException(status_code=409, detail="This NFT has no boost trait")
    await settle(address)
    await items.update_many({"address": address, "boost_nft": input.nft}, {"$unset": {"boost_nft": ""}})
    try:
        await items.update_one({"id": item["id"], "address": address}, {"$set": {"boost_nft": input.nft}})
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="This NFT is already boosting another character")
    return await game_state(address)


class NftLink(BaseModel):
    url: str


class TraitBoost(BaseModel):
    type: str
    value: str
    multiplier: float = Field(gt=1, le=MAX_BOOST)


class BoostTable(BaseModel):
    boosts: List[TraitBoost]


async def settle_boosted() -> None:
    for owner in await get_db().game_items.distinct("address", {"boost_nft": {"$type": "string"}}):
        with suppress(HTTPException):
            await settle(owner)


async def clear_boosts() -> None:
    await settle_boosted()
    await get_db().game_items.update_many({"boost_nft": {"$type": "string"}}, {"$unset": {"boost_nft": ""}})
    await get_db().game_holdings.delete_many({})


@api_router.get("/admin/nft", dependencies=[Depends(require_admin)])
async def admin_get_nft():
    cfg = await nft_config()
    if cfg and (datetime.now(timezone.utc) - datetime.fromisoformat(cfg["updated_at"])).total_seconds() > 60:
        with suppress(HTTPException):  # keep the trait list in step with OpenSea without pressing FETCH
            fresh = await fetch_collection(cfg["url"])
            changes = {k: fresh[k] for k in ("name", "image", "contracts", "total_supply", "traits", "updated_at")}
            await get_db().settings.update_one({"key": "nft"}, {"$set": changes})
            cfg = {**cfg, **changes}
    return {"nft": cfg}


@api_router.put("/admin/nft", dependencies=[Depends(require_admin)])
async def admin_put_nft(input: NftLink):
    data = await fetch_collection(input.url)
    old = await nft_config()
    if old and old["slug"] == data["slug"]:
        known = {(t["type"], t["value"]) for t in data["traits"]}
        data["boosts"] = [b for b in old.get("boosts", []) if (b["type"], b["value"]) in known]
        await settle_boosted()
    elif old:
        await clear_boosts()
    await get_db().settings.replace_one({"key": "nft"}, {"key": "nft", **data}, upsert=True)
    return {"nft": data}


@api_router.put("/admin/nft/boosts", dependencies=[Depends(require_admin)])
async def admin_put_boosts(input: BoostTable):
    cfg = await nft_config()
    if not cfg:
        raise HTTPException(status_code=409, detail="Add an OpenSea collection first")
    known = {(t["type"], t["value"]) for t in cfg["traits"]}
    table = {(b.type, b.value): b.multiplier for b in input.boosts}
    if not table.keys() <= known:
        raise HTTPException(status_code=422, detail="Unknown trait for this collection")
    await settle_boosted()
    boosts = [{"type": t, "value": v, "multiplier": m} for (t, v), m in table.items()]
    await get_db().settings.update_one({"key": "nft"}, {"$set": {"boosts": boosts}})
    return {"nft": {**cfg, "boosts": boosts}}


@api_router.delete("/admin/nft", dependencies=[Depends(require_admin)])
async def admin_delete_nft():
    await clear_boosts()
    await get_db().settings.delete_one({"key": "nft"})
    return {"nft": None}


# Include the router in the main app
app.include_router(api_router)

_origins = [o.strip() for o in os.environ.get("CORS_ORIGINS", "*").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_credentials=False,
    allow_origins=_origins or ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)