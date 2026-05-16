"""
Enterprise Microservice handler (Archetype 4: Hotel Reservation).

Simplified 2-service slice from DeathStarBench hotel-reservation, re-implemented
in Python with DocumentDB (MongoDB-compatible) as the sole backing store.

Operations mapped to duration tiers:
  search-only  (small):  GET /hotels — geo-proximity search
  search+rec   (medium): GET /recommendations — search + scoring algorithm
  full-booking (large):  POST /reservation — search + recommend + auth + write

DocumentDB data model (database: hotel_reservation):
  hotels:       { hotelId, name, lat, lon, rate, type, description, rooms_available }
  users:        { username, password_hash }
  reservations: { hotelId, customerName, inDate, outDate, roomNumber, created_at }

Dual-entrypoint: Lambda handler() + Fargate Flask server on port 8080.
"""

import json
import math
import os
import time
import hashlib
from datetime import datetime, timezone

import pymongo

# --------------- Configuration ---------------
DOCDB_ENDPOINT = os.environ.get("DOCDB_ENDPOINT", "localhost")
DOCDB_PORT = int(os.environ.get("DOCDB_PORT", "27017"))
DOCDB_USERNAME = os.environ.get("DOCDB_USERNAME", "docdbadmin")
DOCDB_PASSWORD = os.environ.get("DOCDB_PASSWORD", "docdbpassword")
PLATFORM = os.environ.get("PLATFORM", "lambda")

# TLS CA bundle for DocumentDB (AWS requires TLS)
TLS_CA_FILE = os.environ.get("TLS_CA_FILE", "/app/global-bundle.pem")

DATABASE_NAME = "hotel_reservation"

# Global connection — reused across warm invocations
_client = None
_db = None


def _get_db():
    """Return a pymongo Database handle, creating the connection on first call."""
    global _client, _db
    if _db is not None:
        return _db

    conn_kwargs = {
        "host": DOCDB_ENDPOINT,
        "port": DOCDB_PORT,
        "username": DOCDB_USERNAME,
        "password": DOCDB_PASSWORD,
        "authSource": "admin",
        "retryWrites": False,  # DocumentDB does not support retryWrites
        "directConnection": True,
        "serverSelectionTimeoutMS": 5000,
        "connectTimeoutMS": 5000,
    }

    # Use TLS if the CA bundle exists (production with DocumentDB)
    if os.path.exists(TLS_CA_FILE):
        conn_kwargs["tls"] = True
        conn_kwargs["tlsCAFile"] = TLS_CA_FILE

    _client = pymongo.MongoClient(**conn_kwargs)
    _db = _client[DATABASE_NAME]
    return _db


# --------------- Geo-proximity helpers ---------------

def _haversine_km(lat1, lon1, lat2, lon2):
    """Haversine distance between two (lat, lon) points in kilometres."""
    R = 6371.0  # Earth radius in km
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1))
         * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# --------------- Core operations ---------------

def search_hotels(lat, lon, radius_km=10.0):
    """
    GET /hotels — search-only (small tier).

    Query the hotels collection and return those within *radius_km* of the
    given coordinates, sorted by distance (ascending).
    """
    db = _get_db()
    hotels = list(db.hotels.find({}, {"_id": 0}))

    results = []
    for h in hotels:
        dist = _haversine_km(lat, lon, h.get("lat", 0), h.get("lon", 0))
        if dist <= radius_km:
            results.append({**h, "distance_km": round(dist, 3)})

    results.sort(key=lambda x: x["distance_km"])
    return results


def recommend_hotels(lat, lon, radius_km=10.0, preferred_type=None,
                     max_rate=None):
    """
    GET /recommendations — search + scoring (medium tier).

    Finds nearby hotels then scores them using a weighted algorithm that
    considers distance, rate, and type preference — mirrors the
    DeathStarBench recommendation service logic.
    """
    nearby = search_hotels(lat, lon, radius_km)

    scored = []
    for h in nearby:
        # Distance score: closer is better (inverse normalised)
        dist_score = 1.0 / (1.0 + h["distance_km"])

        # Rate score: lower rate is better
        rate = h.get("rate", 100)
        rate_score = 1.0 / (1.0 + rate / 100.0)

        # Type preference bonus
        type_score = 1.5 if (preferred_type and
                             h.get("type", "").lower() == preferred_type.lower()) else 1.0

        # Filter by max rate if specified
        if max_rate is not None and rate > max_rate:
            continue

        composite = dist_score * 0.4 + rate_score * 0.3 + type_score * 0.3
        scored.append({**h, "score": round(composite, 4)})

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored


def make_reservation(customer_name, hotel_id, in_date, out_date, password=None):
    """
    POST /reservation — full booking (large tier).

    Performs search → recommend → auth → transactional write, mirroring the
    DeathStarBench reservation flow.  Uses a pymongo client session for the
    transactional write (DocumentDB supports single-shard transactions).
    """
    db = _get_db()

    # 1. Look up the hotel (search phase)
    hotel = db.hotels.find_one({"hotelId": hotel_id}, {"_id": 0})
    if not hotel:
        return {"error": f"Hotel {hotel_id} not found"}

    # 2. Score / recommend (recommendation phase)
    score = 1.0 / (1.0 + hotel.get("rate", 100) / 100.0)

    # 3. Authenticate user (auth phase)
    user = db.users.find_one({"username": customer_name})
    if user and password:
        pw_hash = hashlib.sha256(password.encode()).hexdigest()
        if user.get("password_hash") != pw_hash:
            return {"error": "Authentication failed"}
    # If no user record exists we allow guest booking (matches DeathStarBench behaviour)

    # 4. Transactional reservation write
    with _client.start_session() as session:
        with session.start_transaction():
            # Check room availability
            rooms = hotel.get("rooms_available", 0)
            if rooms <= 0:
                return {"error": "No rooms available"}

            # Decrement available rooms
            db.hotels.update_one(
                {"hotelId": hotel_id, "rooms_available": {"$gt": 0}},
                {"$inc": {"rooms_available": -1}},
                session=session,
            )

            # Insert reservation record
            reservation = {
                "hotelId": hotel_id,
                "customerName": customer_name,
                "inDate": in_date,
                "outDate": out_date,
                "roomNumber": rooms,  # assign the last available room number
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            db.reservations.insert_one(reservation, session=session)

    # Remove the MongoDB _id before returning (not JSON-serialisable)
    reservation.pop("_id", None)
    return {
        "reservation": reservation,
        "hotel": hotel,
        "recommendation_score": round(score, 4),
    }


# --------------- Tier dispatch ---------------

# Default query parameters for each tier
TIER_DEFAULTS = {
    "small": {
        "operation": "search",
        "lat": 37.7749,
        "lon": -122.4194,
        "radius_km": 10.0,
    },
    "medium": {
        "operation": "recommend",
        "lat": 37.7749,
        "lon": -122.4194,
        "radius_km": 10.0,
        "preferred_type": "luxury",
        "max_rate": 300,
    },
    "large": {
        "operation": "reserve",
        "customer_name": "test_user",
        "hotel_id": "1",
        "in_date": "2025-08-01",
        "out_date": "2025-08-03",
    },
}

# Map payload_tier aliases used by the experiment to canonical tier names
TIER_ALIASES = {
    "search-only": "small",
    "search+recommendation": "medium",
    "full-booking": "large",
}


def process(payload_tier, **kwargs):
    """
    Core dispatch — routes to the correct operation based on tier.

    Accepts explicit kwargs that override the tier defaults so callers
    (wrk2 Lua scripts, direct HTTP) can customise parameters.
    """
    start = time.time()

    # Resolve aliases (search-only → small, etc.)
    canonical_tier = TIER_ALIASES.get(payload_tier, payload_tier)
    defaults = TIER_DEFAULTS.get(canonical_tier, TIER_DEFAULTS["small"])
    params = {**defaults, **kwargs}
    operation = params.get("operation", "search")

    if operation == "search":
        data = search_hotels(
            lat=float(params.get("lat", 37.7749)),
            lon=float(params.get("lon", -122.4194)),
            radius_km=float(params.get("radius_km", 10.0)),
        )
        result_body = {"hotels": data, "count": len(data)}

    elif operation == "recommend":
        data = recommend_hotels(
            lat=float(params.get("lat", 37.7749)),
            lon=float(params.get("lon", -122.4194)),
            radius_km=float(params.get("radius_km", 10.0)),
            preferred_type=params.get("preferred_type"),
            max_rate=float(params["max_rate"]) if params.get("max_rate") else None,
        )
        result_body = {"recommendations": data, "count": len(data)}

    elif operation == "reserve":
        data = make_reservation(
            customer_name=params.get("customer_name", "guest"),
            hotel_id=str(params.get("hotel_id", "1")),
            in_date=params.get("in_date", "2025-08-01"),
            out_date=params.get("out_date", "2025-08-03"),
            password=params.get("password"),
        )
        result_body = data

    else:
        result_body = {"error": f"Unknown operation: {operation}"}

    execution_ms = round((time.time() - start) * 1000, 2)
    return {
        "payload_tier": payload_tier,
        "execution_ms": execution_ms,
        **result_body,
    }


# --------------- Lambda entrypoint ---------------
def handler(event, context=None):
    """AWS Lambda handler function."""
    try:
        tier = event.get("payload_tier", "small")
        # Allow callers to pass any extra params through the event
        extra = {k: v for k, v in event.items() if k != "payload_tier"}
        result = process(tier, **extra)
        return {"statusCode": 200, "body": json.dumps(result, default=str)}
    except Exception as e:
        return {
            "statusCode": 500,
            "body": json.dumps({"error": str(e), "endpoint": DOCDB_ENDPOINT}),
        }


# --------------- Fargate entrypoint (Flask) ---------------
if PLATFORM == "fargate":
    from flask import Flask, request, jsonify

    app = Flask(__name__)

    # --- Standard interface routes ---
    @app.route("/invoke", methods=["POST"])
    @app.route("/<path:_prefix>/invoke", methods=["POST"])
    def invoke(_prefix=None):
        ev = request.get_json(force=True)
        r = handler(ev)
        return jsonify(json.loads(r["body"])), r["statusCode"]

    @app.route("/health", methods=["GET"])
    @app.route("/<path:_prefix>/health", methods=["GET"])
    def health(_prefix=None):
        return jsonify({"status": "healthy"}), 200

    # --- wrk2-compatible REST routes ---
    @app.route("/hotels", methods=["GET"])
    @app.route("/<path:_prefix>/hotels", methods=["GET"])
    def hotels_route(_prefix=None):
        """GET /hotels — search-only (small tier)."""
        try:
            lat = float(request.args.get("lat", 37.7749))
            lon = float(request.args.get("lon", -122.4194))
            radius = float(request.args.get("radius_km", 10.0))
            result = process("small", operation="search", lat=lat, lon=lon,
                             radius_km=radius)
            return jsonify(result), 200
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/recommendations", methods=["GET"])
    @app.route("/<path:_prefix>/recommendations", methods=["GET"])
    def recommendations_route(_prefix=None):
        """GET /recommendations — search + scoring (medium tier)."""
        try:
            lat = float(request.args.get("lat", 37.7749))
            lon = float(request.args.get("lon", -122.4194))
            radius = float(request.args.get("radius_km", 10.0))
            ptype = request.args.get("preferred_type", "luxury")
            max_rate = request.args.get("max_rate")
            result = process("medium", operation="recommend", lat=lat, lon=lon,
                             radius_km=radius, preferred_type=ptype,
                             max_rate=float(max_rate) if max_rate else None)
            return jsonify(result), 200
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/reservation", methods=["POST"])
    @app.route("/<path:_prefix>/reservation", methods=["POST"])
    def reservation_route(_prefix=None):
        """POST /reservation — full booking (large tier)."""
        try:
            body = request.get_json(force=True)
            result = process("large", operation="reserve",
                             customer_name=body.get("customer_name", "guest"),
                             hotel_id=str(body.get("hotel_id", "1")),
                             in_date=body.get("in_date", "2025-08-01"),
                             out_date=body.get("out_date", "2025-08-03"),
                             password=body.get("password"))
            return jsonify(result), 200
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    if __name__ == "__main__":
        app.run(host="0.0.0.0", port=8080)
