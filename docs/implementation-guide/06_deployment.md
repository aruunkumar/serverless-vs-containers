
# Experiment Implementation Guide — File 5 of 8

> **Cross-reference**: Complete Sections 1–5 (Files 1–5) before this file. Source `experiment-env.sh` at the start of every session.

---

## 6. Deploying the 32 Endpoints

### 6.1 Naming Convention

All deployments follow a strict naming convention to make experiment data traceable:

```
Pattern: svc-{archetype}-{memory}-{imagesize}-{platform}

Examples:
  svc-event-driven-api-512mb-slim-serverless      → Lambda, 512MB, slim image
  svc-event-driven-api-512mb-slim-container       → Fargate, 512MB, slim image
  svc-batch-transform-2gb-standard-serverless     → Lambda, 2GB, standard image
  svc-ml-inference-2gb-slim-container             → Fargate, 2GB, slim image
```

---

### 6.2 Deployment Matrix (All 32 Deployments)

| Deployment Name | Platform | Archetype | Memory | Image Size | State |
|---|---|---|---|---|---|
| svc-event-driven-api-512mb-slim-serverless | Lambda | event-driven-api | 512MB | slim | stateless |
| svc-event-driven-api-512mb-standard-serverless | Lambda | event-driven-api | 512MB | standard | stateless |
| svc-event-driven-api-2gb-slim-serverless | Lambda | event-driven-api | 2GB | slim | stateless |
| svc-event-driven-api-2gb-standard-serverless | Lambda | event-driven-api | 2GB | standard | stateless |
| svc-event-driven-api-512mb-slim-container | Fargate | event-driven-api | 512MB | slim | stateless |
| svc-event-driven-api-512mb-standard-container | Fargate | event-driven-api | 512MB | standard | stateless |
| svc-event-driven-api-2gb-slim-container | Fargate | event-driven-api | 2GB | slim | stateless |
| svc-event-driven-api-2gb-standard-container | Fargate | event-driven-api | 2GB | standard | stateless |
| svc-batch-transform-512mb-slim-serverless | Lambda | batch-transform | 512MB | slim | stateless |
| svc-batch-transform-512mb-standard-serverless | Lambda | batch-transform | 512MB | standard | stateless |
| svc-batch-transform-2gb-slim-serverless | Lambda | batch-transform | 2GB | slim | stateless |
| svc-batch-transform-2gb-standard-serverless | Lambda | batch-transform | 2GB | standard | stateless |
| svc-batch-transform-512mb-slim-container | Fargate | batch-transform | 512MB | slim | stateless |
| svc-batch-transform-512mb-standard-container | Fargate | batch-transform | 512MB | standard | stateless |
| svc-batch-transform-2gb-slim-container | Fargate | batch-transform | 2GB | slim | stateless |
| svc-batch-transform-2gb-standard-container | Fargate | batch-transform | 2GB | standard | stateless |
| svc-ml-inference-512mb-slim-serverless | Lambda | ml-inference | 512MB | slim | stateless |
| svc-ml-inference-512mb-standard-serverless | Lambda | ml-inference | 512MB | standard | stateless |
| svc-ml-inference-2gb-slim-serverless | Lambda | ml-inference | 2GB | slim | stateless |
| svc-ml-inference-2gb-standard-serverless | Lambda | ml-inference | 2GB | standard | stateless |
| svc-ml-inference-512mb-slim-container | Fargate | ml-inference | 512MB | slim | stateless |
| svc-ml-inference-512mb-standard-container | Fargate | ml-inference | 512MB | standard | stateless |
| svc-ml-inference-2gb-slim-container | Fargate | ml-inference | 2GB | slim | stateless |
| svc-ml-inference-2gb-standard-container | Fargate | ml-inference | 2GB | standard | stateless |
| svc-enterprise-microservices-512mb-slim-serverless | Lambda | enterprise-microservices | 512MB | slim | stateful |
| svc-enterprise-microservices-512mb-standard-serverless | Lambda | enterprise-microservices | 512MB | standard | stateful |
| svc-enterprise-microservices-2gb-slim-serverless | Lambda | enterprise-microservices | 2GB | slim | stateful |
| svc-enterprise-microservices-2gb-standard-serverless | Lambda | enterprise-microservices | 2GB | standard | stateful |
| svc-enterprise-microservices-512mb-slim-container | Fargate | enterprise-microservices | 512MB | slim | stateful |
| svc-enterprise-microservices-512mb-standard-container | Fargate | enterprise-microservices | 512MB | standard | stateful |
| svc-enterprise-microservices-2gb-slim-container | Fargate | enterprise-microservices | 2GB | slim | stateful |
| svc-enterprise-microservices-2gb-standard-container | Fargate | enterprise-microservices | 2GB | standard | stateful |

---

### 6.3 Lambda Deployments (16 Functions)

Repeat this pattern for all 16 Lambda entries in the deployment matrix. Adjust `FUNC_NAME`, `ARCHETYPE`, `MEMORY`, and `IMG_TAG` for each.

```bash
source experiment-env.sh

# Example: event-driven-api, 512MB, slim image
FUNC_NAME="svc-event-driven-api-512mb-slim-serverless"
ARCHETYPE="event-driven-api"
MEMORY=512
IMG_TAG="lambda-slim"

# Step 1: Create the Lambda function
aws lambda create-function \
  --function-name ${FUNC_NAME} \
  --package-type Image \
  --code ImageUri=${ECR_REGISTRY}/svc-experiment/${ARCHETYPE}:${IMG_TAG} \
  --role ${LAMBDA_ROLE_ARN} \
  --memory-size ${MEMORY} \
  --timeout 300 \
  --vpc-config SubnetIds=${SUBNET_PRIV_A},${SUBNET_PRIV_B},SecurityGroupIds=${SG_ID} \
  --environment Variables="{DATA_BUCKET=${BUCKET_NAME},PLATFORM=lambda,ARCHETYPE=${ARCHETYPE}}" \
  --tracing-config Mode=Active \
  --region us-east-1

# Step 2: Create API Gateway HTTP API for this function
API_ID=$(aws apigatewayv2 create-api \
  --name ${FUNC_NAME}-api \
  --protocol-type HTTP \
  --query 'ApiId' --output text)

LAMBDA_ARN=$(aws lambda get-function \
  --function-name ${FUNC_NAME} \
  --query 'Configuration.FunctionArn' --output text)

INT_ID=$(aws apigatewayv2 create-integration \
  --api-id ${API_ID} \
  --integration-type AWS_PROXY \
  --integration-uri ${LAMBDA_ARN} \
  --payload-format-version 2.0 \
  --query 'IntegrationId' --output text)

aws apigatewayv2 create-route \
  --api-id ${API_ID} \
  --route-key "POST /invoke" \
  --target "integrations/${INT_ID}"

aws apigatewayv2 create-route \
  --api-id ${API_ID} \
  --route-key "GET /health" \
  --target "integrations/${INT_ID}"

aws apigatewayv2 create-stage \
  --api-id ${API_ID} \
  --stage-name prod \
  --auto-deploy

# Step 3: Grant API Gateway permission to invoke Lambda
aws lambda add-permission \
  --function-name ${FUNC_NAME} \
  --statement-id apigateway-${API_ID} \
  --action lambda:InvokeFunction \
  --principal apigateway.amazonaws.com \
  --source-arn "arn:aws:execute-api:us-east-1:${ACCOUNT_ID}:${API_ID}/*/*"

ENDPOINT="https://${API_ID}.execute-api.us-east-1.amazonaws.com/prod/invoke"
echo "${FUNC_NAME}=${ENDPOINT}" >> endpoints.txt
echo "Lambda deployed: ${FUNC_NAME} → ${ENDPOINT}"
```

> **Memory mapping for 2GB Lambda**: Set `--memory-size 2048` (Lambda uses MB).

---

### 6.4 Fargate Deployments (16 Services)

> **CPU/MEMORY MAPPING**: 512MB memory → 0.5 vCPU + 1GB task memory. 2GB memory → 2 vCPU + 4GB task memory. This matches the Lambda memory-to-CPU ratio for a fair comparison.

```bash
source experiment-env.sh

# Example: event-driven-api, 512MB, slim image, Fargate
SVC_NAME="svc-event-driven-api-512mb-slim-container"
ARCHETYPE="event-driven-api"
IMG_TAG="fargate-slim"
TASK_CPU="512"      # 0.5 vCPU
TASK_MEM="1024"     # 1GB

# Step 1: Register task definition
cat > task-def-${SVC_NAME}.json << EOF
{
  "family": "${SVC_NAME}",
  "networkMode": "awsvpc",
  "requiresCompatibilities": ["FARGATE"],
  "cpu": "${TASK_CPU}",
  "memory": "${TASK_MEM}",
  "executionRoleArn": "${FARGATE_ROLE_ARN}",
  "taskRoleArn": "${FARGATE_ROLE_ARN}",
  "containerDefinitions": [{
    "name": "app",
    "image": "${ECR_REGISTRY}/svc-experiment/${ARCHETYPE}:${IMG_TAG}",
    "portMappings": [{"containerPort": 8080, "protocol": "tcp"}],
    "environment": [
      {"name": "DATA_BUCKET", "value": "${BUCKET_NAME}"},
      {"name": "PLATFORM", "value": "fargate"},
      {"name": "ARCHETYPE", "value": "${ARCHETYPE}"}
    ],
    "logConfiguration": {"logDriver": "awslogs", "options": {
      "awslogs-group": "/ecs/${SVC_NAME}",
      "awslogs-region": "us-east-1",
      "awslogs-stream-prefix": "ecs"}}
  }]
}
EOF

aws ecs register-task-definition --cli-input-json file://task-def-${SVC_NAME}.json

# Step 2: Create CloudWatch log group
aws logs create-log-group --log-group-name /ecs/${SVC_NAME}

# Step 3: Create ALB target group (path-based routing)
TG_ARN=$(aws elbv2 create-target-group \
  --name ${SVC_NAME:0:32} \
  --protocol HTTP --port 8080 \
  --vpc-id ${VPC_ID} \
  --target-type ip \
  --health-check-path /health \
  --query 'TargetGroups[0].TargetGroupArn' --output text)

# Step 4: Add path-based listener rule
PRIORITY=$((RANDOM % 49000 + 1000))
aws elbv2 create-rule \
  --listener-arn ${LISTENER_ARN} \
  --priority ${PRIORITY} \
  --conditions '[{"Field":"path-pattern","Values":["/'${SVC_NAME}'/*"]}]' \
  --actions '[{"Type":"forward","TargetGroupArn":"'${TG_ARN}'"}]'

# Step 5: Create ECS service
aws ecs create-service \
  --cluster svc-experiment-cluster \
  --service-name ${SVC_NAME} \
  --task-definition ${SVC_NAME} \
  --desired-count 1 \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[${SUBNET_PRIV_A},${SUBNET_PRIV_B}],securityGroups=[${SG_ID}]}" \
  --load-balancers "targetGroupArn=${TG_ARN},containerName=app,containerPort=8080"

ENDPOINT="http://${ALB_DNS}/${SVC_NAME}/invoke"
echo "${SVC_NAME}=${ENDPOINT}" >> endpoints.txt
echo "Fargate deployed: ${SVC_NAME} → ${ENDPOINT}"
```

---

### 6.5 Required Environment Variables for Each Deployment

| Variable | Value | Purpose |
|---|---|---|
| `DATA_BUCKET` | `svc-experiment-data-{ACCOUNT_ID}` | S3 bucket containing test payload files |
| `PLATFORM` | `"lambda"` or `"fargate"` | Determines whether Flask HTTP server starts (Fargate only) |
| `ARCHETYPE` | `event-driven-api` / `batch-transform` / etc. | Identifies which archetype is running (for logging) |

---

### 6.6 Validate All 32 Endpoints Are Healthy

> **WARNING**: Do NOT start the load generator until this validation passes with 32/32 healthy.

```python
#!/usr/bin/env python3
"""validate_endpoints.py — Test all 32 endpoints before starting experiments."""
import requests, json

# Load endpoints from file built during deployment
endpoints = {}
with open('endpoints.txt') as f:
    for line in f:
        if '=' in line:
            name, url = line.strip().split('=', 1)
            endpoints[name] = url

test_payloads = {
    'event-driven-api': {'payload_tier': 'small', 's3_key': 'payloads/event-driven-api/small/sample.jpg'},
    'batch-transform':  {'payload_tier': 'small', 's3_key': 'payloads/batch-transform/small/data.csv'},
    'ml-inference':     {'payload_tier': 'small', 'batch_size': 1},
    'enterprise-microservices':{'payload_tier': 'small', 'operation': 'search-only'}
}

passed, failed = 0, []
for name, url in endpoints.items():
    archetype = next((a for a in test_payloads if a in name), None)
    if not archetype:
        failed.append(f'{name}: unknown archetype')
        continue
    try:
        r = requests.post(url, json=test_payloads[archetype], timeout=30)
        if r.status_code == 200:
            passed += 1
            print(f'  PASS: {name}')
        else:
            failed.append(f'{name}: HTTP {r.status_code}')
            print(f'  FAIL: {name} → HTTP {r.status_code}')
    except Exception as e:
        failed.append(f'{name}: {e}')
        print(f'  ERROR: {name} → {e}')

print(f'
Result: {passed}/32 endpoints healthy')
if failed:
    print('Failed endpoints:')
    for f in failed: print(f'  - {f}')
```

---