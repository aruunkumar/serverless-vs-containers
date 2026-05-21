
# Experiment Implementation Guide — File 4 of 8 

> **Cross-reference**: Sections 1–4 are in Files 1–3 (earlier). Start here for Section 5.

---

## 5. Building Container Images

Each archetype needs two container image variants: **slim (~50MB)** and **standard (~250MB)**. The slim image uses minimal dependencies; the standard image adds common enterprise libraries to increase image size (this tests the cold start impact of larger images).

> **KEY DESIGN**: Lambda images use `public.ecr.aws/lambda/python:3.11` as base and set `CMD ["handler.handler"]`. Fargate images use `python:3.11-slim` as base and run Flask on port 8080 with a `/invoke` POST endpoint and `/health` GET endpoint. The application logic in `handler.py` is **identical** for both platforms.

---

### 5.1 handler.py for Archetype 1 (Thumbnailer)

Create the file `archetypes/thumbnailer/handler.py`:

```python
import json, time, io, os
import boto3
from PIL import Image, ExifTags

s3 = boto3.client('s3')
BUCKET = os.environ.get('DATA_BUCKET', 'sebs-experiment-data')
PLATFORM = os.environ.get('PLATFORM', 'lambda')

def process(payload_tier, s3_key):
    """Core processing logic — same for Lambda and Fargate."""
    start = time.time()
    resp = s3.get_object(Bucket=BUCKET, Key=s3_key)
    image_bytes = resp['Body'].read()
    img = Image.open(io.BytesIO(image_bytes))
    results = {}

    # Tier 1 (all tiers): resize to thumbnail
    thumb = img.copy(); thumb.thumbnail((128, 128))
    out = io.BytesIO(); thumb.save(out, format='JPEG', quality=85)
    results['thumbnail_bytes'] = out.tell()

    if payload_tier in ('medium', 'large'):
        # Tier 2: add PNG conversion + EXIF extraction
        png_out = io.BytesIO(); thumb.save(png_out, format='PNG')
        results['png_bytes'] = png_out.tell()
        exif = {}
        if hasattr(img, '_getexif') and img._getexif():
            for tag_id, val in img._getexif().items():
                tag = ExifTags.TAGS.get(tag_id, tag_id)
                if isinstance(val, (str, int, float)): exif[str(tag)] = str(val)
        results['exif_count'] = len(exif)

    if payload_tier == 'large':
        # Tier 3: multi-format compression variants
        for fmt in ['JPEG', 'PNG', 'WEBP']:
            for q in [50, 75, 95]:
                o = io.BytesIO()
                try: thumb.save(o, format=fmt, quality=q); results[f'{fmt}_{q}'] = o.tell()
                except: pass
        for sz in [(64,64),(256,256),(512,512)]:
            v = img.copy(); v.thumbnail(sz)
            o = io.BytesIO(); v.save(o, format='JPEG'); results[f'thumb_{sz}'] = o.tell()

    return {'payload_tier': payload_tier, 'execution_ms': round((time.time()-start)*1000,2), 'results': results}

# Lambda entrypoint
def handler(event, context=None):
    tier = event.get('payload_tier', 'small')
    key = event.get('s3_key', f'payloads/thumbnailer/{tier}/sample.jpg')
    return {'statusCode': 200, 'body': json.dumps(process(tier, key))}

# Fargate entrypoint (Flask)
if PLATFORM == 'fargate':
    from flask import Flask, request, jsonify
    app = Flask(__name__)
    @app.route('/invoke', methods=['POST'])
    def invoke():
        ev = request.get_json()
        r = handler(ev)
        return jsonify(json.loads(r['body'])), r['statusCode']
    @app.route('/health', methods=['GET'])
    def health(): return jsonify({'status': 'healthy'}), 200
    if __name__ == '__main__': app.run(host='0.0.0.0', port=8080)
```

> **NOTE**: Apply the same `handler.py` pattern to Archetypes 2 (ETL), 3 (ML Inference), and 4 (Hotel Reservation), replacing the core `process()` function with the appropriate workload logic from the SeBS/DeathStarBench repositories.

---

### 5.2 Dockerfile.slim (applies to all archetypes — same pattern)

```dockerfile
# Dockerfile.slim — minimal dependencies, ~50MB target
# Build with: --target lambda-slim or --target fargate-slim

# Lambda slim base
FROM public.ecr.aws/lambda/python:3.11 AS lambda-slim
COPY requirements-slim.txt .
RUN pip install --no-cache-dir -r requirements-slim.txt
COPY handler.py ${LAMBDA_TASK_ROOT}/
CMD ["handler.handler"]

# Fargate slim base
FROM python:3.11-slim AS fargate-slim
WORKDIR /app
COPY requirements-slim.txt .
RUN pip install --no-cache-dir -r requirements-slim.txt
COPY handler.py .
ENV PLATFORM=fargate
EXPOSE 8080
CMD ["python", "handler.py"]
```

---

### 5.3 Dockerfile.standard (same pattern, adds enterprise libs for ~250MB)

```dockerfile
# Dockerfile.standard — additional enterprise dependencies, ~250MB target

# Lambda standard base
FROM public.ecr.aws/lambda/python:3.11 AS lambda-standard
COPY requirements-standard.txt .
RUN pip install --no-cache-dir -r requirements-standard.txt
COPY handler.py ${LAMBDA_TASK_ROOT}/
CMD ["handler.handler"]

# Fargate standard base
FROM python:3.11 AS fargate-standard
WORKDIR /app
COPY requirements-standard.txt .
RUN pip install --no-cache-dir -r requirements-standard.txt
COPY handler.py .
ENV PLATFORM=fargate
EXPOSE 8080
CMD ["python", "handler.py"]
```

---

### 5.4 requirements-slim.txt and requirements-standard.txt

**requirements-slim.txt** (core + application library only):
```
Pillow==10.4.0
boto3==1.35.0
flask==3.0.0
```

**requirements-standard.txt** (adds enterprise libraries to reach ~250MB):
```
Pillow==10.4.0
boto3==1.35.0
flask==3.0.0
gunicorn==22.0.0
aws-xray-sdk==2.14.0
aws-lambda-powertools==2.43.0
numpy==1.26.4
requests==2.32.0
pydantic==2.9.0
structlog==24.4.0
```

---

### 5.5 Build and Push All Images (16 total: 4 archetypes × 2 variants × 2 platforms)

```bash
source experiment-env.sh

# Repeat this block for each archetype:
# ARCHETYPE = thumbnailer | etl-pipeline | ml-inference | hotel-reservation

ARCHETYPE=thumbnailer
cd archetypes/${ARCHETYPE}

# Lambda slim
docker build --target lambda-slim \
  -t ${ECR_REGISTRY}/sebs-experiment/${ARCHETYPE}:lambda-slim \
  -f Dockerfile.slim .
docker push ${ECR_REGISTRY}/sebs-experiment/${ARCHETYPE}:lambda-slim

# Lambda standard
docker build --target lambda-standard \
  -t ${ECR_REGISTRY}/sebs-experiment/${ARCHETYPE}:lambda-standard \
  -f Dockerfile.standard .
docker push ${ECR_REGISTRY}/sebs-experiment/${ARCHETYPE}:lambda-standard

# Fargate slim
docker build --target fargate-slim \
  -t ${ECR_REGISTRY}/sebs-experiment/${ARCHETYPE}:fargate-slim \
  -f Dockerfile.slim .
docker push ${ECR_REGISTRY}/sebs-experiment/${ARCHETYPE}:fargate-slim

# Fargate standard
docker build --target fargate-standard \
  -t ${ECR_REGISTRY}/sebs-experiment/${ARCHETYPE}:fargate-standard \
  -f Dockerfile.standard .
docker push ${ECR_REGISTRY}/sebs-experiment/${ARCHETYPE}:fargate-standard

cd ../..

# Verify image sizes after build
docker images | grep sebs-experiment/${ARCHETYPE}
# Expected: *-slim ~50MB, *-standard ~250MB
```

---

### 5.6 Generate and Upload Test Payloads to S3

```python
#!/usr/bin/env python3
"""generate_payloads.py — Generate synthetic test payloads and upload to S3."""
import pandas as pd, numpy as np, os, boto3

BUCKET = os.environ.get('BUCKET_NAME', 'sebs-experiment-data')
s3 = boto3.client('s3', region_name='us-east-1')

# Generate ETL CSV datasets
os.makedirs('payloads/etl/small', exist_ok=True)
os.makedirs('payloads/etl/medium', exist_ok=True)
os.makedirs('payloads/etl/large', exist_ok=True)

for name, rows in [('small', 10000), ('medium', 100000), ('large', 1000000)]:
    df = pd.DataFrame({
        'id': range(rows),
        'value_a': np.random.normal(100, 25, rows),
        'value_b': np.random.normal(50, 10, rows),
        'category': np.random.choice(['A','B','C','D','E'], rows),
        'region': np.random.choice(['us-east','us-west','eu-west'], rows),
        'status': np.random.choice(['active','inactive','pending'], rows, p=[0.7,0.2,0.1])
    })
    local_path = f'payloads/etl/{name}/data.csv'
    df.to_csv(local_path, index=False)
    s3.upload_file(local_path, BUCKET, f'payloads/etl/{name}/data.csv')
    print(f'ETL {name}: {rows} rows uploaded')

# For thumbnailer: manually place sample images:
#   payloads/thumbnailer/small/sample.jpg   (~50KB JPEG)
#   payloads/thumbnailer/medium/sample.png  (~500KB PNG)
#   payloads/thumbnailer/large/sample.tiff  (~2MB TIFF)
# Then upload:
for tier in ['small', 'medium', 'large']:
    for fname in os.listdir(f'payloads/thumbnailer/{tier}'):
        s3.upload_file(
            f'payloads/thumbnailer/{tier}/{fname}', BUCKET,
            f'payloads/thumbnailer/{tier}/{fname}')
        print(f'Thumbnailer {tier}/{fname} uploaded')

print('All payloads uploaded to S3.')
```
