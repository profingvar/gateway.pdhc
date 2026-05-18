#!/usr/bin/env bash
# safe_restart.sh — Graceful restart of gateway.pdhc on the server
set -e

export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
export DOCKER_HOST="${DOCKER_HOST:-unix://$HOME/.colima/default/docker.sock}"
# macOS ObjC fork-safety: CoreFoundation in parent poisons fork()s; setting
# this env var before gunicorn prevents the SIGKILL spiral after worker recycles.
export OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$PROJECT_DIR/gateway_app"
VENV_DIR="$APP_DIR/venv"
PID_FILE="$PROJECT_DIR/gunicorn.pid"
HEALTH_URL="http://127.0.0.1:9050/api/v1/health"

PORTS=(9050 9051 9052 9053)

echo "=== gateway.pdhc safe restart — $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

# Step 1: Stop existing gunicorn
echo "Stopping application..."
if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    kill -TERM "$PID" 2>/dev/null || true
    sleep 2
    kill -9 "$PID" 2>/dev/null || true
    rm -f "$PID_FILE"
fi
lsof -ti :9050 2>/dev/null | xargs kill -TERM 2>/dev/null || true
sleep 1
lsof -ti :9050 2>/dev/null | xargs kill -9 2>/dev/null || true

# Step 2: Ensure venv exists
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment..."
    python3 -m venv "$VENV_DIR"
fi
source "$VENV_DIR/bin/activate"

# Step 3: Install dependencies
cd "$APP_DIR"
pip install -q -r requirements.txt

# Step 4: Start Docker DB if not running
if ! docker ps --format {{.Names}} | grep -q gateway_pdhc_db; then
    echo "Starting PostgreSQL container (pgvector on port 9051)..."
    docker-compose up -d db
    echo "Waiting for database..."
    for i in 1 2 3 4 5 6; do
        sleep 2
        if docker exec gateway_pdhc_db pg_isready -U gateway_pdhc >/dev/null 2>&1; then
            echo "Database ready after $i checks"
            break
        fi
        if [ "$i" -eq 6 ]; then
            echo "ERROR: Database not ready — aborting"
            exit 1
        fi
    done
fi

# Step 5: Run migrations
echo "Running migrations..."
flask db upgrade 2>/dev/null || echo "No pending migrations (or first run)"

# Step 6: Start gunicorn on 127.0.0.1:9050
echo "Starting gunicorn on 127.0.0.1:9050..."
mkdir -p "$PROJECT_DIR/results"
gunicorn \
    --bind 127.0.0.1:9050 \
    --workers 2 \
    --timeout 120 \
    --daemon \
    --pid "$PID_FILE" \
    --access-logfile "$PROJECT_DIR/results/access.log" \
    --error-logfile "$PROJECT_DIR/results/error.log" \
    "app:create_app()"

# Step 7: Health check
echo "Verifying health..."
for i in 1 2 3; do
    sleep 2
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "$HEALTH_URL" 2>/dev/null || echo "000")
    if [ "$HTTP_CODE" = "200" ]; then
        echo "Health check: OK (attempt $i)"
        echo "PID: $(cat "$PID_FILE" 2>/dev/null || echo unknown)"
        exit 0
    fi
    echo "Health check attempt $i: HTTP ${HTTP_CODE}"
done

echo "ERROR: Health check failed"
echo "Check logs: $PROJECT_DIR/results/error.log"
exit 1
