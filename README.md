# Flash Agent

A lightweight ITOps Kubernetes log metrics agent that collects pod logs, analyzes them using an LLM (via OpenAI-compatible API), and exports traces via OpenTelemetry (OTLP).

## Features

- **Log Collection**: Automatically collects logs from all pods in a configured Kubernetes namespace
- **LLM-Powered Analysis**: Uses AI to identify errors, warnings, anomalies, and performance issues
- **Issue Categorization**: Classifies issues into categories (CrashLoop, OOM, ImagePull, Connectivity, etc.)
- **Health Metrics**: Generates overall health scores and pod health statistics
- **OpenTelemetry Integration**: Exports traces via OTLP to any compatible backend (Langfuse, Jaeger, Grafana Tempo, etc.)
- **Flexible Execution**: Supports both CronJob (run-once) and continuous deployment modes

## Prerequisites

- Python 3.12+
- Access to a Kubernetes cluster (in-cluster or via kubeconfig)
- OpenAI-compatible API endpoint
- (Optional) OTLP-compatible tracing backend (Langfuse, Jaeger, Grafana Tempo, etc.)

## Setup

### Local Development

1. **Clone the repository**:
   ```bash
   git clone <repository-url>
   cd flash-agent
   ```

2. **Create and activate a virtual environment**:
   ```bash
   python -m venv venv
   source venv/bin/activate
   ```

3. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

4. **Configure environment variables**:
   ```bash
   export AGENT_NAME="flash-agent"
   export AGENT_MODE="active"
   export K8S_NAMESPACE="sock-shop"
   export OPENAI_BASE_URL="https://api.openai.com/v1"  # Or your OpenAI-compatible endpoint
   export OPENAI_API_KEY="your-api-key"
   export MODEL_ALIAS="gpt-4.1"
   
   # Optional: OpenTelemetry configuration (send traces to any OTLP backend)
   export OTEL_EXPORTER_OTLP_ENDPOINT="https://your-otlp-endpoint/v1/traces"
   export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer your-token"
   
   # Example: Send traces to Langfuse
   # export OTEL_EXPORTER_OTLP_ENDPOINT="https://us.cloud.langfuse.com/api/public/otel/v1/traces"
   # export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Basic <base64(public_key:secret_key)>"
   
   # Optional: Scan settings
   export LOG_TAIL_LINES="200"
   export SCAN_INTERVAL="300"  # Set to 0 for run-once mode
   export LOG_LEVEL="INFO"
   ```

5. **Run the agent**:
   ```bash
   python main.py
   ```

### Environment Variables Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENT_NAME` | `flash-agent` | Name identifier for the agent |
| `AGENT_MODE` | `active` | Operating mode |
| `K8S_NAMESPACE` | `default` | Kubernetes namespace to monitor |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | OpenAI-compatible API base URL |
| `OPENAI_API_KEY` | `` | API key for the LLM endpoint |
| `MODEL_ALIAS` | `gpt-41` | Model to use for analysis |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `` | OTLP endpoint URL (e.g., `https://host/v1/traces`) |
| `OTEL_EXPORTER_OTLP_HEADERS` | `` | OTLP headers (format: `key1=value1,key2=value2`) |
| `TRACE_TAGS` | `flash-agent` | Comma-separated tags for traces |
| `LOG_TAIL_LINES` | `200` | Number of log lines to collect per container |
| `SCAN_INTERVAL` | `300` | Seconds between scans (0 = run once) |
| `LOG_LEVEL` | `INFO` | Logging level (DEBUG, INFO, WARNING, ERROR) |

## Verification

### Verify Local Setup

1. **Check Python version**:
   ```bash
   python --version  # Should be 3.12+
   ```

2. **Verify dependencies are installed**:
   ```bash
   pip list | grep -E "kubernetes|openai|opentelemetry"
   ```

3. **Test Kubernetes connectivity**:
   ```bash
   kubectl get pods -n $K8S_NAMESPACE
   ```

4. **Run agent in dry mode** (with debug logging):
   ```bash
   LOG_LEVEL=DEBUG SCAN_INTERVAL=0 python main.py
   ```

### Verify Docker Image

1. **Run the container locally**:
   ```bash
   docker run --rm \
     -e K8S_NAMESPACE=default \
     -e OPENAI_BASE_URL=http://host.docker.internal:4000/v1 \
     -v ~/.kube/config:/home/agent/.kube/config:ro \
     flash-agent:latest
   ```

2. **Check container logs**:
   ```bash
   docker logs <container-id>
   ```

## Docker Build and Push

### Using Makefile (Recommended)

```bash
# Build the Docker image
make build

# Push to Docker Hub (agentcert/flash-agent:latest)
make push

# Build and push in one step
make build-push

# Build without cache
make build-no-cache

# Tag with a specific version
make tag NEW_TAG=v1.0.0

# Show current image configuration
make version
```

### Manual Docker Commands

```bash
# Build from the parent directory (context needs access to flash-agent/)
docker build -t agentcert/flash-agent:latest -f Dockerfile .

# Push to Docker Hub
docker login
docker push agentcert/flash-agent:latest
```

### Multi-Architecture Build (Optional)

```bash
# Build and push for multiple architectures
docker buildx create --name multiarch --use
docker buildx build --platform linux/amd64,linux/arm64 \
  -t <registry>/flash-agent:latest \
  -f Dockerfile \
  --push .
```

## Kubernetes Deployment

The agent is designed to run as either a CronJob (periodic scans) or a Deployment (continuous monitoring):

- **CronJob mode**: Set `SCAN_INTERVAL=0` (default)
- **Continuous mode**: Set `SCAN_INTERVAL` to desired interval in seconds (e.g., `300` for 5 minutes)

## License

See [LICENSE](LICENSE) file for details.