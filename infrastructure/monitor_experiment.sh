#!/bin/bash
set -euo pipefail

###############################################################################
# monitor_experiment.sh
#
# Monitoring utility for the 7-day serverless vs container benchmark experiment.
# Provides subcommands to check load generator status, count completed blocks,
# query CloudWatch for Lambda metrics, verify Fargate services, and recover
# from failures.
#
# Usage:
#   ./monitor_experiment.sh <command>
#
# Commands:
#   status    — Check load generator process status
#   logs      — Tail the load generator log file
#   blocks    — Count completed blocks across all per-deployment CSVs
#   lambda    — CloudWatch CLI: Lambda invocation counts and error rates
#   fargate   — ECS CLI: Fargate running task counts
#   redeploy  — Force new deployment for a stopped Fargate service
#   help      — Show this help message
###############################################################################

REGION="us-east-2"
ENV_FILE="experiment-env.sh"
LOG_FILE="load_generator.log"
RESULTS_DIR="results"
EXPECTED_BLOCKS=1152  # 32 deployments × 36 blocks

CLUSTER="svc-experiment-cluster"

# All 16 Lambda function names (svc-*-serverless)
ARCHETYPES=("event-driven-api" "batch-transform" "ml-inference" "enterprise-microservice")
MEMORIES=("512mb" "2gb")
SIZES=("slim" "standard")

# Source experiment-env.sh if available (for ALB ARN, etc.)
if [[ -f "$ENV_FILE" ]]; then
    # shellcheck disable=SC1090
    source "$ENV_FILE"
fi

###############################################################################
# Helpers
###############################################################################

_lambda_names() {
    for arch in "${ARCHETYPES[@]}"; do
        for mem in "${MEMORIES[@]}"; do
            for sz in "${SIZES[@]}"; do
                echo "svc-${arch}-${mem}-${sz}-serverless"
            done
        done
    done
}

_fargate_names() {
    for arch in "${ARCHETYPES[@]}"; do
        for mem in "${MEMORIES[@]}"; do
            for sz in "${SIZES[@]}"; do
                echo "svc-${arch}-${mem}-${sz}-container"
            done
        done
    done
}

###############################################################################
# status — Check load generator process
###############################################################################

cmd_status() {
    echo "=== Load Generator Process Status ==="
    echo ""
    if pgrep -f "load_generator.py" > /dev/null 2>&1; then
        echo "RUNNING — load_generator.py is active."
        echo ""
        ps aux | grep "[l]oad_generator.py" || true
    else
        echo "NOT RUNNING — load_generator.py process not found."
    fi
    echo ""
    if [[ -f "$LOG_FILE" ]]; then
        echo "Last 5 log lines:"
        tail -5 "$LOG_FILE"
    else
        echo "Log file ($LOG_FILE) not found."
    fi
}

###############################################################################
# logs — Tail the log file
###############################################################################

cmd_logs() {
    if [[ ! -f "$LOG_FILE" ]]; then
        echo "Log file ($LOG_FILE) not found."
        exit 1
    fi
    echo "=== Tailing $LOG_FILE (Ctrl-C to stop) ==="
    tail -f "$LOG_FILE"
}

###############################################################################
# blocks — Count completed blocks across all per-deployment CSVs
###############################################################################

cmd_blocks() {
    echo "=== Completed Blocks ==="
    echo ""

    if [[ ! -d "$RESULTS_DIR" ]]; then
        echo "Results directory ($RESULTS_DIR) not found."
        exit 1
    fi

    total=0
    file_count=0

    for csv_file in "$RESULTS_DIR"/*.csv; do
        [[ -f "$csv_file" ]] || continue
        file_count=$((file_count + 1))
        # Count data rows (subtract 1 for header)
        rows=$(( $(wc -l < "$csv_file") - 1 ))
        if [[ $rows -lt 0 ]]; then rows=0; fi
        printf "  %-60s %3d / 36 blocks\n" "$(basename "$csv_file")" "$rows"
        total=$((total + rows))
    done

    echo ""
    echo "Deployment CSVs found: $file_count / 32"
    echo "Total completed blocks: $total / $EXPECTED_BLOCKS"

    if [[ $total -eq $EXPECTED_BLOCKS ]]; then
        echo "STATUS: COMPLETE — all $EXPECTED_BLOCKS blocks recorded."
    elif [[ $total -eq 0 ]]; then
        echo "STATUS: NO DATA — experiment may not have started."
    else
        pct=$(( total * 100 / EXPECTED_BLOCKS ))
        echo "STATUS: IN PROGRESS — ${pct}% complete."
    fi
}

###############################################################################
# lambda — CloudWatch Lambda invocation counts and error rates
###############################################################################

cmd_lambda() {
    echo "=== Lambda Invocation Counts & Error Rates (last 1 hour) ==="
    echo ""

    end_time=$(date -u +%Y-%m-%dT%H:%M:%S)
    start_time=$(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%S 2>/dev/null \
                 || date -u -v-1H +%Y-%m-%dT%H:%M:%S 2>/dev/null \
                 || echo "")

    if [[ -z "$start_time" ]]; then
        echo "Could not compute start time. Using 3600s ago."
        start_time=$(date -u +%Y-%m-%dT%H:%M:%S --date='1 hour ago' 2>/dev/null || echo "2025-01-01T00:00:00")
    fi

    printf "%-60s %12s %12s %12s\n" "Function" "Invocations" "Errors" "ErrorRate%"
    printf "%-60s %12s %12s %12s\n" "--------" "-----------" "------" "----------"

    for fn in $(_lambda_names); do
        invocations=$(aws cloudwatch get-metric-statistics \
            --region "$REGION" \
            --namespace "AWS/Lambda" \
            --metric-name "Invocations" \
            --dimensions "Name=FunctionName,Value=$fn" \
            --start-time "$start_time" \
            --end-time "$end_time" \
            --period 3600 \
            --statistics Sum \
            --query 'Datapoints[0].Sum' \
            --output text 2>/dev/null || echo "0")

        errors=$(aws cloudwatch get-metric-statistics \
            --region "$REGION" \
            --namespace "AWS/Lambda" \
            --metric-name "Errors" \
            --dimensions "Name=FunctionName,Value=$fn" \
            --start-time "$start_time" \
            --end-time "$end_time" \
            --period 3600 \
            --statistics Sum \
            --query 'Datapoints[0].Sum' \
            --output text 2>/dev/null || echo "0")

        # Handle "None" from empty datapoints
        [[ "$invocations" == "None" || -z "$invocations" ]] && invocations="0"
        [[ "$errors" == "None" || -z "$errors" ]] && errors="0"

        if [[ "$invocations" != "0" && "$invocations" != "0.0" ]]; then
            error_rate=$(awk "BEGIN {printf \"%.2f\", ($errors / $invocations) * 100}")
        else
            error_rate="N/A"
        fi

        printf "%-60s %12s %12s %12s\n" "$fn" "$invocations" "$errors" "$error_rate"
    done
}

###############################################################################
# fargate — ECS Fargate running task counts
###############################################################################

cmd_fargate() {
    echo "=== Fargate Service Running Task Counts ==="
    echo ""

    printf "%-60s %8s %8s %10s\n" "Service" "Desired" "Running" "Status"
    printf "%-60s %8s %8s %10s\n" "-------" "-------" "-------" "------"

    for svc in $(_fargate_names); do
        result=$(aws ecs describe-services \
            --region "$REGION" \
            --cluster "$CLUSTER" \
            --services "$svc" \
            --query 'services[0].{desired:desiredCount,running:runningCount,status:status}' \
            --output json 2>/dev/null || echo '{}')

        desired=$(echo "$result" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('desired','?'))" 2>/dev/null || echo "?")
        running=$(echo "$result" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('running','?'))" 2>/dev/null || echo "?")
        status=$(echo "$result" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('status','UNKNOWN'))" 2>/dev/null || echo "UNKNOWN")

        flag=""
        if [[ "$running" == "0" && "$desired" != "0" ]]; then
            flag=" *** DOWN"
        fi

        printf "%-60s %8s %8s %10s%s\n" "$svc" "$desired" "$running" "$status" "$flag"
    done
}

###############################################################################
# redeploy — Force new deployment for a stopped/failed Fargate service
###############################################################################

cmd_redeploy() {
    local service_name="${1:-}"

    if [[ -z "$service_name" ]]; then
        echo "Usage: $0 redeploy <service-name>"
        echo ""
        echo "Forces a new deployment for the specified ECS Fargate service."
        echo "This replaces the current task with a fresh one."
        echo ""
        echo "Example:"
        echo "  $0 redeploy svc-event-driven-api-512mb-slim-container"
        exit 1
    fi

    echo "Forcing new deployment for: $service_name"
    aws ecs update-service \
        --region "$REGION" \
        --cluster "$CLUSTER" \
        --service "$service_name" \
        --force-new-deployment \
        --query 'service.{status:status,desired:desiredCount,running:runningCount}' \
        --output table

    echo ""
    echo "New deployment triggered. Monitor with:"
    echo "  $0 fargate"
}

###############################################################################
# help — Show usage
###############################################################################

cmd_help() {
    cat <<'EOF'
Usage: ./monitor_experiment.sh <command> [args]

Commands:
  status              Check load generator process status
  logs                Tail the load generator log file (Ctrl-C to stop)
  blocks              Count completed blocks across all per-deployment CSVs
                      (expected total: 1,152 = 32 deployments × 36 blocks)
  lambda              Query CloudWatch for Lambda invocation counts and error
                      rates across all 16 serverless functions (last 1 hour)
  fargate             List all 16 Fargate services with desired/running task
                      counts and flag any that are down
  redeploy <service>  Force a new deployment for a stopped Fargate service
                      e.g., redeploy svc-event-driven-api-512mb-slim-container
  help                Show this help message

Environment:
  Region:   us-east-2
  Cluster:  svc-experiment-cluster
  Log:      load_generator.log
  Results:  results/

Tip: Source experiment-env.sh before running for full ALB/resource context.
EOF
}

###############################################################################
# Main dispatcher
###############################################################################

command="${1:-help}"
shift || true

case "$command" in
    status)    cmd_status ;;
    logs)      cmd_logs ;;
    blocks)    cmd_blocks ;;
    lambda)    cmd_lambda ;;
    fargate)   cmd_fargate ;;
    redeploy)  cmd_redeploy "$@" ;;
    help|--help|-h)  cmd_help ;;
    *)
        echo "Unknown command: $command"
        echo ""
        cmd_help
        exit 1
        ;;
esac
