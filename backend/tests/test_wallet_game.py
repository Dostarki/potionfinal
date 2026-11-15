"""Backend tests for PotionRH: wallet auth, /game/*, admin claims.

Updated for the new on-chain Market purchase flow (price in USD, paid in the
admin-configured ERC-20 token, treasury 0x608bedeb69740e0330d79baf55396ce7762a9b5e).
We cannot send real transactions, so purchase tests only verify the verification
paths (fake/unrelated tx). Earnings/claims use direct Mongo seeding on a fresh
test wallet, and every seeded doc is cleaned up at the end of the module.
"""
import os
import time
import threading
import datetime as dt
import secrets as pysecrets
import uuid
import pytest
import requests
from eth_account import Account
from eth_account.messages import encode_defunct
from pymongo import MongoClient

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    for line in open("/app/frontend/.env"):
        if line.startswith("REACT_APP_BACKEND_URL="):
            BASE_URL = line.split("=", 1)[1].strip().rstrip("/")

HOST = BASE_URL.split("//", 1)[1]
CHAIN_ID = 4663
ADMIN_PASSWORD = "potion-admin-2026"
TREASURY = "0x608bedeb69740e0330d79baf55396ce7762a9b5e"
# A real, confirmed tx on Robinhood Chain that is NOT a transfer to our treasury
# of our test token in the amount we want -> should always be rejected.
UNRELATED_REAL_TX = "0x96b5c62a7fd2a6841f48b0eed6a43b519ea1b887559c09a665a6c90aa5e0a559"

MONGO_URL = "mongodb://localhost:27017"
DB_NAME = "test_database"
mongo = MongoClient(MONGO_URL)
db = mongo[DB_NAME]

# wallets we create during the suite so we can clean them up at the end
_test_wallets: set[str] = set()


# ---------- SIWE helpers ----------

def build_siwe(address: str, nonce: str, chain_id: int = CHAIN_ID, domain: str = HOST, uri: str = BASE_URL) -> str:
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return (
        f"{domain} wants you to sign in with your Ethereum account:\n{address}\n\n"
        f"Sign in to PotionRH on Robinhood Chain.\n\n"
        f"URI: {uri}\nVersion: 1\nChain ID: {chain_id}\nNonce: {nonce}\nIssued At: {now}"
    )


def sign(msg: str, key: str) -> str:
    sig = Account.sign_message(encode_defunct(text=msg), key).signature.hex()
    return sig if sig.startswith("0x") else "0x" + sig


def new_wallet():
    key = "0x" + pysecrets.token_hex(32)
    acct = Account.from_key(key)
    _test_wallets.add(acct.address.lower())
    return key, acct.address


def get_nonce(address: str):
    return requests.get(f"{BASE_URL}/api/auth/nonce", params={"address": address})


def login(key: str = None, address: str = None, chain_id: int = CHAIN_ID,
          domain: str = HOST, uri: str = BASE_URL, origin: str = None):
    if key is None:
        key, address = new_wallet()
    else:
        _test_wallets.add(address.lower())
    r = get_nonce(address)
    assert r.status_code == 200, r.text
    nonce = r.json()["nonce"]
    msg = build_siwe(address, nonce, chain_id=chain_id, domain=domain, uri=uri)
    sig = sign(msg, key)
    headers = {"Origin": origin if origin is not None else BASE_URL}
    return requests.post(f"{BASE_URL}/api/auth/verify", json={"message": msg, "signature": sig}, headers=headers), key, address, msg, sig


def login_ok():
    r, key, address, _, _ = login()
    assert r.status_code == 200, r.text
    return r.json()["token"], address.lower()


# ---------- Fixtures ----------

@pytest.fixture
def wallet_token():
    return login_ok()


@pytest.fixture
def auth_headers(wallet_token):
    token, _ = wallet_token
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def admin_token():
    r = requests.post(f"{BASE_URL}/api/admin/login", json={"password": ADMIN_PASSWORD})
    assert r.status_code == 200, r.text
    return r.json()["token"]


# ---------- Cleanup ----------

@pytest.fixture(scope="module", autouse=True)
def cleanup():
    yield
    if not _test_wallets:
        return
    addrs = list(_test_wallets)
    db.game_items.delete_many({"address": {"$in": addrs}})
    db.game_claims.delete_many({"address": {"$in": addrs}})
    db.game_orders.delete_many({"address": {"$in": addrs}})
    db.game_payments.delete_many({"address": {"$in": addrs}})
    db.game_players.delete_many({"address": {"$in": addrs}})
    db.game_expansions.delete_many({"address": {"$in": addrs}})


# ---------- Nonce / verify (regression) ----------

class TestNonce:
    def test_nonce_valid(self):
        _, addr = new_wallet()
        r = get_nonce(addr)
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data["nonce"], str) and len(data["nonce"]) >= 16
        assert data["chain_id"] == CHAIN_ID

    def test_nonce_invalid_address(self):
        assert get_nonce("not-an-address").status_code == 422


class TestVerify:
    def test_valid_login(self):
        r, _, address, _, _ = login()
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["address"] == address.lower()
        assert isinstance(data["token"], str) and len(data["token"]) > 20

    def test_wrong_chain_id(self):
        r, *_ = login(chain_id=1)
        assert r.status_code == 403

    def test_replayed_nonce(self):
        r, key, address, msg, sig = login()
        assert r.status_code == 200
        r2 = requests.post(f"{BASE_URL}/api/auth/verify",
                           json={"message": msg, "signature": sig},
                           headers={"Origin": BASE_URL})
        assert r2.status_code == 401

    def test_wrong_signature(self):
        key, address = new_wallet()
        nonce = get_nonce(address).json()["nonce"]
        msg = build_siwe(address, nonce)
        bad_key = "0x" + pysecrets.token_hex(32)
        rv = requests.post(f"{BASE_URL}/api/auth/verify",
                           json={"message": msg, "signature": sign(msg, bad_key)},
                           headers={"Origin": BASE_URL})
        assert rv.status_code == 401


# ---------- Auth gating / catalog ----------

class TestAuthGating:
    def test_state_without_token(self):
        assert requests.get(f"{BASE_URL}/api/game/state").status_code == 401

    def test_admin_jwt_rejected_on_game(self, admin_token):
        rg = requests.get(f"{BASE_URL}/api/game/state",
                          headers={"Authorization": f"Bearer {admin_token}"})
        assert rg.status_code == 403

    def test_catalog_public_and_per_min(self):
        r = requests.get(f"{BASE_URL}/api/game/catalog")
        assert r.status_code == 200
        data = r.json()
        cat = data["catalog"]
        assert len(cat) == 6
        assert data["chain_id"] == CHAIN_ID
        prices = {c["level"]: c["price"] for c in cat}
        assert prices == {1: 1, 2: 10, 3: 50, 4: 100, 5: 200, 6: 400}
        for c in cat:
            assert c["per_min"] == pytest.approx(c["price"] / 1440, rel=1e-9)


# ---------- State shape ----------

class TestState:
    def test_state_shape(self, auth_headers):
        s = requests.get(f"{BASE_URL}/api/game/state", headers=auth_headers).json()
        assert "earned_usd" in s and isinstance(s["earned_usd"], (int, float))
        assert "per_min" in s
        tok = s["token"]
        assert tok["address"].startswith("0x") and tok["symbol"] and tok["url"].startswith("http")
        assert isinstance(tok["price_usd"], (int, float))
        claim = s["claim"]
        assert claim["hours"] == 12
        assert "next_at" in claim and "last" in claim


# ---------- Buy quote ----------

class TestBuyQuote:
    def test_quote_ok(self, auth_headers):
        r = requests.post(f"{BASE_URL}/api/game/buy/quote", json={"level": 4}, headers=auth_headers)
        assert r.status_code == 200, r.text
        q = r.json()
        # fields
        assert q["level"] == 4
        assert q["price_usd"] == 100
        assert q["treasury"] == TREASURY
        assert q["decimals"] == 18
        assert q["token_address"].startswith("0x") and q["token_address"] == q["token_address"].lower()
        # amount_wei == ceil(price/token_price * 1e18)
        from decimal import Decimal, ROUND_CEILING
        expected = int((Decimal("100") / Decimal(str(q["token_price_usd"])) * Decimal(10) ** 18)
                       .to_integral_value(rounding=ROUND_CEILING))
        assert int(q["amount_wei"]) == expected

    def test_quote_unknown_level(self, auth_headers):
        r = requests.post(f"{BASE_URL}/api/game/buy/quote", json={"level": 99}, headers=auth_headers)
        assert r.status_code == 422

    def test_quote_requires_auth(self):
        r = requests.post(f"{BASE_URL}/api/game/buy/quote", json={"level": 1})
        assert r.status_code == 401


# ---------- Buy confirm ----------

class TestBuyConfirm:
    def test_malformed_hash(self, auth_headers):
        q = requests.post(f"{BASE_URL}/api/game/buy/quote", json={"level": 1}, headers=auth_headers).json()
        r = requests.post(f"{BASE_URL}/api/game/buy",
                          json={"order_id": q["id"], "tx_hash": "0xdead"},
                          headers=auth_headers)
        assert r.status_code == 422

    def test_unknown_order(self, auth_headers):
        random_tx = "0x" + pysecrets.token_hex(32)
        r = requests.post(f"{BASE_URL}/api/game/buy",
                          json={"order_id": str(uuid.uuid4()), "tx_hash": random_tx},
                          headers=auth_headers)
        assert r.status_code == 404

    def test_random_tx_not_confirmed(self, auth_headers, wallet_token):
        _, addr = wallet_token
        qr = requests.post(f"{BASE_URL}/api/game/buy/quote", json={"level": 1}, headers=auth_headers)
        assert qr.status_code == 200, qr.text
        q = qr.json()
        random_tx = "0x" + pysecrets.token_hex(32)
        r = requests.post(f"{BASE_URL}/api/game/buy",
                          json={"order_id": q["id"], "tx_hash": random_tx},
                          headers=auth_headers)
        assert r.status_code == 425, r.text
        # no items created
        s = requests.get(f"{BASE_URL}/api/game/state", headers=auth_headers).json()
        assert s["items"] == []

    def test_unrelated_real_tx(self, auth_headers, wallet_token):
        _, addr = wallet_token
        q = requests.post(f"{BASE_URL}/api/game/buy/quote", json={"level": 1}, headers=auth_headers).json()
        r = requests.post(f"{BASE_URL}/api/game/buy",
                          json={"order_id": q["id"], "tx_hash": UNRELATED_REAL_TX},
                          headers=auth_headers)
        assert r.status_code == 400, r.text
        s = requests.get(f"{BASE_URL}/api/game/state", headers=auth_headers).json()
        assert s["items"] == []

    def test_other_wallet_order_404(self, auth_headers):
        q = requests.post(f"{BASE_URL}/api/game/buy/quote", json={"level": 1}, headers=auth_headers).json()
        # Different wallet trying to confirm this order
        other_token, _ = login_ok()
        r = requests.post(f"{BASE_URL}/api/game/buy",
                          json={"order_id": q["id"], "tx_hash": "0x" + pysecrets.token_hex(32)},
                          headers={"Authorization": f"Bearer {other_token}"})
        assert r.status_code == 404

    def test_old_demo_buy_rejected(self, auth_headers):
        """POST /api/game/buy with only {level} (old demo flow) must not grant a card."""
        r = requests.post(f"{BASE_URL}/api/game/buy", json={"level": 1}, headers=auth_headers)
        assert r.status_code == 422, r.text
        s = requests.get(f"{BASE_URL}/api/game/state", headers=auth_headers).json()
        assert s["items"] == []


# ---------- Earnings via Mongo seed ----------

def _seed_placed_item(address: str, level: int = 4, demo: bool = False):
    now = dt.datetime.now(dt.timezone.utc)
    bought = (now - dt.timedelta(hours=24)).isoformat()
    doc = {
        "id": str(uuid.uuid4()),
        "address": address.lower(),
        "level": level,
        "name": f"TEST_item_lv{level}",
        "price_usd": {1: 1, 2: 10, 3: 50, 4: 100, 5: 200, 6: 400}[level],
        "placed": True,
        "slot": 0 if not demo else 1,
        "bought_at": bought,
    }
    if demo:
        doc["demo"] = True
    db.game_items.insert_one(doc)
    # Seed player with earned_at 24h ago, earned_usd 0
    db.game_players.update_one(
        {"address": address.lower()},
        {"$set": {"earned_at": (now - dt.timedelta(hours=24)).isoformat(), "earned_usd": 0.0}},
        upsert=True,
    )
    return doc


class TestEarnings:
    def test_earn_24h_full_price(self):
        token, addr = login_ok()
        _seed_placed_item(addr, level=4, demo=False)
        h = {"Authorization": f"Bearer {token}"}
        s = requests.get(f"{BASE_URL}/api/game/state", headers=h).json()
        assert s["earned_usd"] == pytest.approx(100, rel=0.01), s["earned_usd"]
        assert s["per_min"] * 1440 == pytest.approx(100, rel=1e-6)

    def test_demo_item_earns_nothing(self):
        token, addr = login_ok()
        _seed_placed_item(addr, level=4, demo=True)
        h = {"Authorization": f"Bearer {token}"}
        s = requests.get(f"{BASE_URL}/api/game/state", headers=h).json()
        assert s["earned_usd"] == pytest.approx(0, abs=0.01)
        assert s["per_min"] == 0

    def test_recall_stops_earning(self):
        token, addr = login_ok()
        _seed_placed_item(addr, level=4, demo=False)
        h = {"Authorization": f"Bearer {token}"}
        r = requests.post(f"{BASE_URL}/api/game/recall", json={"level": 4}, headers=h)
        assert r.status_code == 200, r.text
        s1 = requests.get(f"{BASE_URL}/api/game/state", headers=h).json()
        assert s1["per_min"] == 0
        before = s1["earned_usd"]
        time.sleep(2)
        s2 = requests.get(f"{BASE_URL}/api/game/state", headers=h).json()
        assert s2["earned_usd"] == pytest.approx(before, abs=0.001)


# ---------- Claim ----------

class TestClaim:
    def test_zero_balance_409(self, auth_headers):
        r = requests.post(f"{BASE_URL}/api/game/claim", headers=auth_headers)
        assert r.status_code == 409

    def test_claim_ok_and_second_429(self):
        token, addr = login_ok()
        _seed_placed_item(addr, level=4, demo=False)
        h = {"Authorization": f"Bearer {token}"}
        before = requests.get(f"{BASE_URL}/api/game/state", headers=h).json()
        amount_usd = before["earned_usd"]
        assert amount_usd > 1  # earned

        r = requests.post(f"{BASE_URL}/api/game/claim", headers=h)
        assert r.status_code == 200, r.text
        s = r.json()
        assert s["earned_usd"] == pytest.approx(0, abs=0.01)
        assert s["claim"]["last"]["status"] == "pending"
        # next_at ~ 12h ahead
        next_at = dt.datetime.fromisoformat(s["claim"]["next_at"])
        delta_h = (next_at - dt.datetime.now(dt.timezone.utc)).total_seconds() / 3600
        assert 11.9 <= delta_h <= 12.1
        # amount_wei == floor(amount_usd / price * 1e18) using the amount reported by the claim itself
        from decimal import Decimal, ROUND_FLOOR
        tok_price = Decimal(str(before["token"]["price_usd"]))
        amount_usd_actual = s["claim"]["last"]["amount_usd"]
        expected = int((Decimal(str(amount_usd_actual)) / tok_price * Decimal(10) ** 18)
                       .to_integral_value(rounding=ROUND_FLOOR))
        assert int(s["claim"]["last"]["amount_wei"]) == expected
        # And sanity check vs the earned_usd we measured just before (allow ~1% drift from time between calls)
        assert amount_usd_actual >= amount_usd * 0.99

        # second claim immediately -> 429
        r2 = requests.post(f"{BASE_URL}/api/game/claim", headers=h)
        assert r2.status_code == 429

    def test_concurrent_claims_create_one(self):
        token, addr = login_ok()
        _seed_placed_item(addr, level=4, demo=False)
        h = {"Authorization": f"Bearer {token}"}
        # make sure earned_usd got settled > 0
        requests.get(f"{BASE_URL}/api/game/state", headers=h)
        results: list[int] = []

        def fire():
            results.append(requests.post(f"{BASE_URL}/api/game/claim", headers=h).status_code)

        threads = [threading.Thread(target=fire) for _ in range(5)]
        for t in threads: t.start()
        for t in threads: t.join()
        oks = [s for s in results if s == 200]
        others = [s for s in results if s != 200]
        # Exactly one success is the primary expectation, but we also assert only
        # one claim doc exists in Mongo for this address regardless of statuses.
        n_claims = db.game_claims.count_documents({"address": addr})
        assert n_claims == 1, f"Expected 1 claim doc, got {n_claims}, statuses={results}"
        assert len(oks) == 1, f"Expected 1 OK, got {results}"


# ---------- Admin claims ----------

class TestAdminClaims:
    def test_admin_claims_requires_auth(self):
        assert requests.get(f"{BASE_URL}/api/admin/claims").status_code == 401

    def test_admin_claim_paid_random(self, admin_token):
        # seed a pending claim via API
        token, addr = login_ok()
        _seed_placed_item(addr, level=4, demo=False)
        h = {"Authorization": f"Bearer {token}"}
        requests.post(f"{BASE_URL}/api/game/claim", headers=h)
        claim = db.game_claims.find_one({"address": addr})
        assert claim and claim["status"] == "pending"
        cid = claim["id"]
        ah = {"Authorization": f"Bearer {admin_token}"}
        # random tx -> 425
        r1 = requests.post(f"{BASE_URL}/api/admin/claims/{cid}/paid",
                           json={"tx_hash": "0x" + pysecrets.token_hex(32)}, headers=ah)
        assert r1.status_code == 425
        # unrelated real tx -> 400
        r2 = requests.post(f"{BASE_URL}/api/admin/claims/{cid}/paid",
                           json={"tx_hash": UNRELATED_REAL_TX}, headers=ah)
        assert r2.status_code == 400
        # still pending
        still = db.game_claims.find_one({"id": cid})
        assert still["status"] == "pending"

    def test_admin_claim_reject_returns_balance(self, admin_token):
        token, addr = login_ok()
        _seed_placed_item(addr, level=4, demo=False)
        h = {"Authorization": f"Bearer {token}"}
        pre = requests.get(f"{BASE_URL}/api/game/state", headers=h).json()["earned_usd"]
        assert pre > 1
        claim_resp = requests.post(f"{BASE_URL}/api/game/claim", headers=h)
        assert claim_resp.status_code == 200
        claim = db.game_claims.find_one({"address": addr})
        cid = claim["id"]
        amount = claim["amount_usd"]
        ah = {"Authorization": f"Bearer {admin_token}"}
        r = requests.post(f"{BASE_URL}/api/admin/claims/{cid}/reject", headers=ah)
        assert r.status_code == 200
        assert r.json()["claim"]["status"] == "rejected"
        # second reject -> 409
        r2 = requests.post(f"{BASE_URL}/api/admin/claims/{cid}/reject", headers=ah)
        assert r2.status_code == 409
        # earned_usd restored
        after = requests.get(f"{BASE_URL}/api/game/state", headers=h).json()["earned_usd"]
        assert after >= amount - 0.01


# ---------- Place / recall / expand regression (uses seeded items) ----------

class TestPlaceRecallExpand:
    def test_place_nothing(self, auth_headers):
        assert requests.post(f"{BASE_URL}/api/game/place", json={"level": 6}, headers=auth_headers).status_code == 404

    def test_recall_then_place(self):
        token, addr = login_ok()
        # Seed an unplaced item (bought but not placed)
        db.game_items.insert_one({
            "id": str(uuid.uuid4()),
            "address": addr,
            "level": 2,
            "name": "TEST_item",
            "price_usd": 10,
            "placed": False,
            "bought_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        })
        h = {"Authorization": f"Bearer {token}"}
        rp = requests.post(f"{BASE_URL}/api/game/place", json={"level": 2}, headers=h)
        assert rp.status_code == 200, rp.text
        placed = [i for i in rp.json()["items"] if i["level"] == 2 and i.get("placed")]
        assert len(placed) == 1
        rr = requests.post(f"{BASE_URL}/api/game/recall", json={"level": 2}, headers=h)
        assert rr.status_code == 200
        assert all(not i.get("placed") for i in rr.json()["items"] if i["level"] == 2)

    def test_expand(self):
        token, _ = login_ok()
        h = {"Authorization": f"Bearer {token}"}
        assert requests.post(f"{BASE_URL}/api/game/expand", headers=h).status_code == 422
        q = requests.post(f"{BASE_URL}/api/game/expand/quote", headers=h)
        assert q.status_code == 200, q.text
        order = q.json()
        assert order["kind"] == "expansion" and order["price_usd"] == 2 and order["treasury"] == TREASURY and int(order["amount_wei"]) > 0
        body = {"order_id": order["id"], "tx_hash": "0x" + pysecrets.token_hex(32)}
        assert requests.post(f"{BASE_URL}/api/game/expand", json=body, headers=h).status_code == 425
        assert requests.post(f"{BASE_URL}/api/game/buy", json=body, headers=h).status_code == 404
        assert requests.post(f"{BASE_URL}/api/game/expand", json={**body, "tx_hash": UNRELATED_REAL_TX}, headers=h).status_code == 400
        assert requests.get(f"{BASE_URL}/api/game/state", headers=h).json()["expansion"]["count"] == 0


# ---------- Regression: leaderboard, token, admin settings ----------

class TestRegression:
    def test_root(self):
        r = requests.get(f"{BASE_URL}/api/")
        assert r.status_code == 200 and r.json().get("message") == "Hello World"

    def test_settings(self):
        r = requests.get(f"{BASE_URL}/api/settings")
        assert r.status_code == 200
        for k in ("follow_url", "rt_url", "like_url", "share_url", "share_text"):
            assert k in r.json()

    def test_admin_login_bad(self):
        assert requests.post(f"{BASE_URL}/api/admin/login", json={"password": "wrong"}).status_code == 401

    def test_token_endpoint(self):
        r = requests.get(f"{BASE_URL}/api/token")
        assert r.status_code == 200
        tok = r.json()["token"]
        assert tok and tok["address"].startswith("0x")

    def test_leaderboard(self, auth_headers):
        r = requests.get(f"{BASE_URL}/api/game/leaderboard", headers=auth_headers)
        assert r.status_code == 200
        data = r.json()
        assert "leaders" in data and "total" in data and data["max_expansions"] == 4

    def test_admin_token_put_delete_restore(self, admin_token):
        """PUT a token link, then DELETE, then restore the original TEST token link."""
        ah = {"Authorization": f"Bearer {admin_token}"}
        original_url = "https://ponsfamily.com/launchpad/0xcd6ceec0cecabbafe91dee0db84bb97e946e895d"
        # DELETE
        rd = requests.delete(f"{BASE_URL}/api/admin/token", headers=ah)
        assert rd.status_code == 200
        # PUT restore
        rp = requests.put(f"{BASE_URL}/api/admin/token", json={"url": original_url}, headers=ah)
        assert rp.status_code == 200, rp.text
        assert rp.json()["token"]["address"] == "0xcd6ceec0cecabbafe91dee0db84bb97e946e895d"
