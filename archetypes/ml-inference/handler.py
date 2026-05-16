"""
ML Inference handler (Archetype 3: Image Recognition).

Based on the SeBS 411.image-recognition benchmark. Loads a pre-trained model
once globally on cold start, runs image classification with tiered batch sizes.

Small:  batch=1, MobileNetV2 (~14MB)
Medium: batch=4, ResNet-50 (~98MB)
Large:  batch=8, ResNet-50 (~98MB)

Models baked into container image at /app/models/ (not downloaded at runtime).
Uses torchvision transforms: Resize(256) -> CenterCrop(224) -> ToTensor -> Normalize.

Dual-entrypoint: Lambda handler() + Fargate Flask server on port 8080.
"""

import json
import time
import os

import boto3
import torch
import torchvision.models as models
import torchvision.transforms as transforms
from PIL import Image

s3 = boto3.client("s3")
BUCKET = os.environ.get("DATA_BUCKET", "svc-experiment-data")
PLATFORM = os.environ.get("PLATFORM", "lambda")
MODEL_DIR = os.environ.get("MODEL_DIR", "/app/models")

# ImageNet preprocessing pipeline (same as SeBS 411.image-recognition)
preprocess = transforms.Compose([
    transforms.Resize(256),
    transforms.CenterCrop(224),
    transforms.ToTensor(),
    transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
])

# Global model cache — loaded once on cold start, reused for warm invocations
_models = {}


def _load_model(model_name):
    """Load a model from /app/models/ or fall back to torchvision defaults."""
    if model_name in _models:
        return _models[model_name]

    model_path = os.path.join(MODEL_DIR, f"{model_name}.pth")

    if model_name == "mobilenet_v2":
        model = models.mobilenet_v2()
    elif model_name == "resnet50":
        model = models.resnet50()
    else:
        raise ValueError(f"Unknown model: {model_name}")

    if os.path.exists(model_path):
        state_dict = torch.load(model_path, map_location="cpu", weights_only=True)
        model.load_state_dict(state_dict)
    else:
        # Fallback: use torchvision pretrained weights (for local dev/testing)
        if model_name == "mobilenet_v2":
            model = models.mobilenet_v2(weights=models.MobileNet_V2_Weights.DEFAULT)
        else:
            model = models.resnet50(weights=models.ResNet50_Weights.DEFAULT)

    model.eval()
    _models[model_name] = model
    return model


# Tier configuration: model name, default batch size
TIER_CONFIG = {
    "small":  {"model": "mobilenet_v2", "batch_size": 1},
    "medium": {"model": "resnet50",     "batch_size": 4},
    "large":  {"model": "resnet50",     "batch_size": 8},
}


def _download_images(payload_tier, batch_size):
    """Download test images from S3 for the given tier."""
    images = []
    if batch_size == 1:
        key = f"payloads/ml-inference/{payload_tier}/test_image.jpg"
        resp = s3.get_object(Bucket=BUCKET, Key=key)
        img = Image.open(resp["Body"]).convert("RGB")
        images.append(img)
    else:
        for i in range(batch_size):
            key = f"payloads/ml-inference/{payload_tier}/test_images/image_{i}.jpg"
            try:
                resp = s3.get_object(Bucket=BUCKET, Key=key)
                img = Image.open(resp["Body"]).convert("RGB")
                images.append(img)
            except Exception:
                # If fewer images available, duplicate the first one
                if images:
                    images.append(images[0])
                else:
                    raise
    return images


def process(payload_tier, batch_size=None):
    """Core ML inference logic — identical for Lambda and Fargate."""
    start = time.time()

    tier_cfg = TIER_CONFIG.get(payload_tier, TIER_CONFIG["small"])
    model_name = tier_cfg["model"]
    if batch_size is None:
        batch_size = tier_cfg["batch_size"]

    # Load model (cached after first cold start)
    model = _load_model(model_name)

    # Download and preprocess images
    images = _download_images(payload_tier, batch_size)
    input_tensors = [preprocess(img) for img in images]
    input_batch = torch.stack(input_tensors)

    # Run inference
    with torch.no_grad():
        output = model(input_batch)

    # Get top-5 predictions per image
    probabilities = torch.nn.functional.softmax(output, dim=1)
    predictions = []
    for i in range(batch_size):
        top5_prob, top5_idx = torch.topk(probabilities[i], 5)
        predictions.append({
            "image_index": i,
            "top5": [
                {"class_id": int(idx), "confidence": round(float(prob), 4)}
                for idx, prob in zip(top5_idx, top5_prob)
            ],
        })

    execution_ms = round((time.time() - start) * 1000, 2)
    return {
        "payload_tier": payload_tier,
        "execution_ms": execution_ms,
        "model": model_name,
        "batch_size": batch_size,
        "predictions": predictions,
    }


# --------------- Lambda entrypoint ---------------
def handler(event, context=None):
    """AWS Lambda handler function."""
    try:
        tier = event.get("payload_tier", "small")
        batch_size = event.get("batch_size", None)
        result = process(tier, batch_size)
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
