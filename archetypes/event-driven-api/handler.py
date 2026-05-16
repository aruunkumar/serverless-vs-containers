"""
Event-Driven API handler (Archetype 1: Thumbnailer).

Adapted from SeBS 210.thumbnailer benchmark. Downloads an image from S3,
performs tiered image processing, and returns results.

Dual-entrypoint: Lambda handler() + Fargate Flask server on port 8080.
"""

import json
import time
import io
import os

import boto3
from PIL import Image, ExifTags, ImageDraw, ImageFont

s3 = boto3.client("s3")
BUCKET = os.environ.get("DATA_BUCKET", "svc-experiment-data")
PLATFORM = os.environ.get("PLATFORM", "lambda")


def process(payload_tier, s3_key):
    """Core processing logic — identical for Lambda and Fargate."""
    start = time.time()

    # Download image from S3
    resp = s3.get_object(Bucket=BUCKET, Key=s3_key)
    image_bytes = resp["Body"].read()
    img = Image.open(io.BytesIO(image_bytes))
    results = {}

    # --- Small tier (all tiers): resize to thumbnail ---
    thumb = img.copy()
    thumb.thumbnail((128, 128))
    out = io.BytesIO()
    thumb.save(out, format="JPEG", quality=85)
    results["thumbnail_bytes"] = out.tell()

    if payload_tier in ("medium", "large"):
        # --- Medium tier: add PNG conversion + EXIF extraction ---
        png_out = io.BytesIO()
        thumb.save(png_out, format="PNG")
        results["png_bytes"] = png_out.tell()

        exif = {}
        if hasattr(img, "_getexif") and img._getexif():
            for tag_id, val in img._getexif().items():
                tag = ExifTags.TAGS.get(tag_id, tag_id)
                if isinstance(val, (str, int, float)):
                    exif[str(tag)] = str(val)
        results["exif_count"] = len(exif)

    if payload_tier == "large":
        # --- Large tier: multi-format compression (9 variants) + watermark ---

        # 9 variants: 3 formats × 3 quality levels
        for fmt in ["JPEG", "PNG", "WEBP"]:
            for q in [50, 75, 95]:
                o = io.BytesIO()
                try:
                    thumb.save(o, format=fmt, quality=q)
                    results[f"{fmt}_{q}"] = o.tell()
                except Exception:
                    pass

        # Additional resize variants
        for sz in [(64, 64), (256, 256), (512, 512)]:
            v = img.copy()
            v.thumbnail(sz)
            o = io.BytesIO()
            v.save(o, format="JPEG")
            results[f"thumb_{sz[0]}x{sz[1]}"] = o.tell()

        # Watermarking
        watermarked = img.copy()
        if watermarked.mode != "RGBA":
            watermarked = watermarked.convert("RGBA")
        overlay = Image.new("RGBA", watermarked.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 36)
        except (IOError, OSError):
            font = ImageFont.load_default()
        text = "BENCHMARK"
        bbox = draw.textbbox((0, 0), text, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]
        x = (watermarked.width - text_w) // 2
        y = (watermarked.height - text_h) // 2
        draw.text((x, y), text, fill=(255, 255, 255, 128), font=font)
        watermarked = Image.alpha_composite(watermarked, overlay)
        wm_out = io.BytesIO()
        watermarked.convert("RGB").save(wm_out, format="JPEG", quality=85)
        results["watermarked_bytes"] = wm_out.tell()

    execution_ms = round((time.time() - start) * 1000, 2)
    return {
        "payload_tier": payload_tier,
        "execution_ms": execution_ms,
        "results": results,
    }


# --------------- Lambda entrypoint ---------------
def handler(event, context=None):
    """AWS Lambda handler function."""
    try:
        tier = event.get("payload_tier", "small")
        key = event.get("s3_key", f"payloads/event-driven-api/{tier}/sample.jpg")
        result = process(tier, key)
        return {"statusCode": 200, "body": json.dumps(result)}
    except Exception as e:
        return {"statusCode": 500, "body": json.dumps({"error": str(e)})}


# --------------- Fargate entrypoint (Flask) ---------------
if PLATFORM == "fargate":
    from flask import Flask, request, jsonify

    app = Flask(__name__)

    @app.route("/invoke", methods=["POST"])
    @app.route("/<path:_prefix>/invoke", methods=["POST"])
    def invoke(_prefix=None):
        ev = request.get_json()
        r = handler(ev)
        return jsonify(json.loads(r["body"])), r["statusCode"]

    @app.route("/health", methods=["GET"])
    @app.route("/<path:_prefix>/health", methods=["GET"])
    def health(_prefix=None):
        return jsonify({"status": "healthy"}), 200

    if __name__ == "__main__":
        app.run(host="0.0.0.0", port=8080)
