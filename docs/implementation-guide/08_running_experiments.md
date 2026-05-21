
# Experiment Implementation Guide — File 7 of 8
# Running the Experiments

> **Cross-reference**: Complete Sections 1–7 before this file. All 32 endpoints must be validated healthy (Section 6.6) before proceeding.

---

## 8. Running the Experiments

### 8.1 Pre-Flight Checklist

Complete every item before starting the load generator. Do not skip any step.

- [ ] All 32 endpoints return HTTP 200 on validation test (`validate_endpoints.py` passes 32/32)
- [ ] `deployments.json` contains all 32 entries with correct URLs
- [ ] All S3 payload files are uploaded (`payloads/thumbnailer/`, `payloads/etl/`, etc.)
- [ ] EC2 load generator instance is running (c5.2xlarge, us-east-1)
- [ ] `load_generator.py`, `deployments.json`, and `lua/` scripts are on the EC2 instance
- [ ] `numpy` and `requests` are installed on EC2 (`pip3.11 install numpy requests`)
- [ ] `wrk2` is installed on EC2 (`wrk2 --version` returns without error)
- [ ] CloudWatch Log Groups exist for all 16 Fargate services (`/ecs/sebs-*`)
- [ ] AWS X-Ray tracing is enabled on all Lambda functions
- [ ] You have noted the experiment start time (UTC) — needed for cost queries
- [ ] EC2 instance has an IAM role or credentials with CloudWatch and Cost Explorer read access

---

### 8.2 Experiment Timeline

| Day | Activity |
|---|---|
| **Day 1** | Deploy all 32 resources. Run `validate_endpoints.py`. Discard any data from warm-up invocations. Do NOT start the load generator yet. |
| **Day 2 (start)** | Start the load generator. Note exact UTC start time. |
| **Days 2–8** | Load generator runs continuously. Monitor for errors every 12 hours. |
| **Day 8 (end)** | Load generator completes all 36 blocks per deployment (~63 hours). |
| **Day 9** | Run data collection scripts. Export results CSV. Tear down infrastructure. |

---

### 8.3 Starting the Load Generator

Run the following on the EC2 instance. Use `nohup` or `tmux` so the process continues if your SSH session disconnects.

```bash
# Option A: Using nohup (recommended for unattended runs)
nohup python3.11 load_generator.py \
  --config deployments.json \
  --output results/ \
  > load_generator_stdout.log 2>&1 &

echo "Load generator PID: $!"
echo $! > load_generator.pid

# Option B: Using tmux (allows you to reattach and monitor)
tmux new-session -d -s experiment 'python3.11 load_generator.py --config deployments.json --output results/'
tmux attach -t experiment   # reattach at any time
```

---

### 8.4 Monitoring Experiment Progress

#### Check load generator is still running

```bash
# Check process is alive
ps aux | grep load_generator.py

# Tail the log for live progress
tail -f load_generator.log

# Count completed blocks across all deployments
grep "Block.*END" load_generator.log | wc -l
# Expected at completion: 32 deployments × 36 blocks = 1,152
```

- CloudWatch > Lambda > Monitor dashboard: invocation count, error rate, duration percentiles
-	ECS Console > Cluster > Services: check Running Task Count stays at 1 (min) or higher
-	ELB Console > Target Groups: check healthy host count
-	Check load generator logs: tail -f results/*.csv — records should accumulate
-	AWS Billing > Cost Explorer: verify spend is tracking expected estimates

#### CloudWatch monitoring commands

```bash
# Check Lambda invocation counts (last 1 hour)
aws cloudwatch get-metric-statistics \
  --namespace AWS/Lambda \
  --metric-name Invocations \
  --dimensions Name=FunctionName,Value=sebs-thumbnailer-512mb-slim-lambda \
  --start-time $(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%SZ) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%SZ) \
  --period 3600 \
  --statistics Sum

# Check Lambda error rate
aws cloudwatch get-metric-statistics \
  --namespace AWS/Lambda \
  --metric-name Errors \
  --dimensions Name=FunctionName,Value=sebs-thumbnailer-512mb-slim-lambda \
  --start-time $(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%SZ) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%SZ) \
  --period 3600 \
  --statistics Sum

# Check ECS Fargate running task count
aws ecs describe-services \
  --cluster sebs-experiment-cluster \
  --services sebs-thumbnailer-512mb-slim-fargate \
  --query 'services[0].{running:runningCount,desired:desiredCount}'
```

---

### 8.5 What to Do If a Deployment Fails Mid-Experiment

**Lambda function errors (high error rate)**:
1. Check CloudWatch Logs for the function: `aws logs tail /aws/lambda/<function-name> --follow`
2. Common causes: S3 payload not found (check S3 key), timeout (increase Lambda timeout), memory OOM (check memory setting)
3. If recoverable: fix the issue and the load generator will continue sending requests
4. If the function is unresponsive: redeploy it (Section 6.3) and update `deployments.json`

**Fargate service stopped**:
1. Check ECS service events: `aws ecs describe-services --cluster sebs-experiment-cluster --services <service-name>`
2. Check container logs: `aws logs tail /ecs/<service-name> --follow`
3. Restart the service: `aws ecs update-service --cluster sebs-experiment-cluster --service <service-name> --force-new-deployment`

**Load generator thread died**:
1. Check `load_generator.log` for the thread name and error
2. The remaining threads continue unaffected
3. Note which deployment failed and which blocks were missed — these records will be absent from the results

NOTE:	Data from failed blocks should be excluded from the training dataset (mark as FAILED in the results CSV)