# GRID Agent System - Deployment Guide

This guide covers deployment strategies for GRID Agent System in various environments.

## 🐳 Docker Deployment (Recommended)

### Quick Start
```bash
# Clone and setup
git clone <your-repo>
cd grid
make setup

# Start production services
make build
make run
```

### Services Overview
- **Frontend**: React app served by Nginx (port 3000)
- **Backend**: FastAPI application (port 8000)
- **Redis**: Caching and session storage (port 6379)
- **Monitoring**: Prometheus & Grafana (optional)

## 🚀 Production Deployment

### 1. Environment Preparation
```bash
# Create production environment file
cat > .env.prod << EOF
OPENROUTER_API_KEY=your_production_key
OPENAI_API_KEY=your_production_key
ANTHROPIC_API_KEY=your_production_key
DATABASE_URL=postgresql://user:pass@prod-db:5432/grid
REDIS_URL=redis://prod-redis:6379
ENVIRONMENT=production
DEBUG_LOGGING=false
LOG_LEVEL=INFO
EOF
```

### 2. SSL Configuration
```bash
# Create SSL directory
mkdir -p ssl

# Add your SSL certificates
cp your-domain.crt ssl/
cp your-domain.key ssl/
```

### 3. Production Deployment
```bash
# Deploy with production profile
docker-compose --profile production up -d

# Verify deployment
make health
```

## ☁️ Cloud Deployment

### AWS ECS Deployment
```bash
# Build and push to ECR
aws ecr get-login-password --region us-west-2 | docker login --username AWS --password-stdin <account>.dkr.ecr.us-west-2.amazonaws.com

docker build -f Dockerfile.backend -t grid-backend .
docker tag grid-backend:latest <account>.dkr.ecr.us-west-2.amazonaws.com/grid-backend:latest
docker push <account>.dkr.ecr.us-west-2.amazonaws.com/grid-backend:latest

docker build -f Dockerfile.frontend -t grid-frontend .
docker tag grid-frontend:latest <account>.dkr.ecr.us-west-2.amazonaws.com/grid-frontend:latest
docker push <account>.dkr.ecr.us-west-2.amazonaws.com/grid-frontend:latest
```

### Google Cloud Run
```bash
# Deploy backend
gcloud run deploy grid-backend \
  --image gcr.io/your-project/grid-backend \
  --platform managed \
  --region us-central1 \
  --allow-unauthenticated

# Deploy frontend
gcloud run deploy grid-frontend \
  --image gcr.io/your-project/grid-frontend \
  --platform managed \
  --region us-central1 \
  --allow-unauthenticated
```

### Azure Container Instances
```bash
# Create resource group
az group create --name grid-rg --location eastus

# Deploy containers
az container create \
  --resource-group grid-rg \
  --name grid-backend \
  --image your-registry.azurecr.io/grid-backend:latest \
  --ports 8000 \
  --environment-variables ENVIRONMENT=production
```

## 🔧 Kubernetes Deployment

### 1. Create Namespace
```yaml
# k8s/namespace.yaml
apiVersion: v1
kind: Namespace
metadata:
  name: grid-system
```

### 2. ConfigMap for Configuration
```yaml
# k8s/configmap.yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: grid-config
  namespace: grid-system
data:
  config.yaml: |
    # Your config.yaml content here
```

### 3. Secrets for API Keys
```yaml
# k8s/secrets.yaml
apiVersion: v1
kind: Secret
metadata:
  name: grid-secrets
  namespace: grid-system
type: Opaque
data:
  openrouter-api-key: <base64-encoded-key>
  openai-api-key: <base64-encoded-key>
```

### 4. Backend Deployment
```yaml
# k8s/backend-deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: grid-backend
  namespace: grid-system
spec:
  replicas: 3
  selector:
    matchLabels:
      app: grid-backend
  template:
    metadata:
      labels:
        app: grid-backend
    spec:
      containers:
      - name: backend
        image: your-registry/grid-backend:latest
        ports:
        - containerPort: 8000
        env:
        - name: OPENROUTER_API_KEY
          valueFrom:
            secretKeyRef:
              name: grid-secrets
              key: openrouter-api-key
        volumeMounts:
        - name: config
          mountPath: /app/config.yaml
          subPath: config.yaml
      volumes:
      - name: config
        configMap:
          name: grid-config
```

### 5. Deploy to Kubernetes
```bash
# Apply all manifests
kubectl apply -f k8s/

# Check deployment status
kubectl get pods -n grid-system

# View logs
kubectl logs -f deployment/grid-backend -n grid-system
```

## 🌐 Reverse Proxy Configuration

### Nginx Configuration
```nginx
# nginx.conf
upstream grid_backend {
    server backend:8000;
}

upstream grid_frontend {
    server frontend:80;
}

server {
    listen 80;
    server_name your-domain.com;
    return 301 https://$server_name$request_uri;
}

server {
    listen 443 ssl http2;
    server_name your-domain.com;

    ssl_certificate /etc/ssl/certs/your-domain.crt;
    ssl_certificate_key /etc/ssl/private/your-domain.key;

    # Frontend
    location / {
        proxy_pass http://grid_frontend;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    # API
    location /api/ {
        proxy_pass http://grid_backend/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }

    # WebSocket
    location /ws/ {
        proxy_pass http://grid_backend/ws/;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
    }
}
```

### Traefik Configuration
```yaml
# docker-compose.traefik.yml
version: '3.8'

services:
  traefik:
    image: traefik:v2.9
    command:
      - --api.dashboard=true
      - --providers.docker=true
      - --entrypoints.web.address=:80
      - --entrypoints.websecure.address=:443
      - --certificatesresolvers.letsencrypt.acme.email=admin@yourdomain.com
      - --certificatesresolvers.letsencrypt.acme.storage=/acme.json
      - --certificatesresolvers.letsencrypt.acme.httpchallenge.entrypoint=web
    ports:
      - "80:80"
      - "443:443"
      - "8080:8080"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - ./acme.json:/acme.json
    labels:
      - traefik.http.routers.api.rule=Host(`traefik.yourdomain.com`)
      - traefik.http.routers.api.tls.certresolver=letsencrypt

  backend:
    labels:
      - traefik.http.routers.backend.rule=Host(`yourdomain.com`) && PathPrefix(`/api`)
      - traefik.http.routers.backend.tls.certresolver=letsencrypt

  frontend:
    labels:
      - traefik.http.routers.frontend.rule=Host(`yourdomain.com`)
      - traefik.http.routers.frontend.tls.certresolver=letsencrypt
```

## 📊 Monitoring Setup

### Prometheus Configuration
```yaml
# monitoring/prometheus.yml
global:
  scrape_interval: 15s

scrape_configs:
  - job_name: 'grid-backend'
    static_configs:
      - targets: ['backend:8000']
    metrics_path: '/metrics'

  - job_name: 'grid-system'
    static_configs:
      - targets: ['backend:8000']
    metrics_path: '/v1/system/metrics'
```

### Grafana Dashboard
```bash
# Start monitoring stack
make monitor

# Import dashboards
curl -X POST \
  http://admin:admin@localhost:3001/api/dashboards/db \
  -H 'Content-Type: application/json' \
  -d @monitoring/dashboards/grid-system.json
```

## 🔒 Security Hardening

### 1. Environment Security
```bash
# Set proper file permissions
chmod 600 .env*
chmod 600 ssl/*

# Use secrets management
kubectl create secret generic grid-secrets \
  --from-literal=openrouter-key=$OPENROUTER_API_KEY
```

### 2. Network Security
```yaml
# docker-compose security overlay
version: '3.8'

networks:
  internal:
    driver: bridge
    internal: true
  web:
    driver: bridge

services:
  backend:
    networks:
      - internal
      - web
  
  redis:
    networks:
      - internal
```

### 3. Container Security
```dockerfile
# Security-hardened Dockerfile
FROM python:3.11-alpine

# Create non-root user
RUN adduser -D -s /bin/sh griduser

# Install security updates
RUN apk update && apk upgrade

# Use non-root user
USER griduser

# Set read-only filesystem
COPY --chown=griduser:griduser . /app
WORKDIR /app
```

## 🔄 CI/CD Pipeline

### GitHub Actions
```yaml
# .github/workflows/deploy.yml
name: Deploy to Production

on:
  push:
    branches: [main]

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      
      - name: Build and Push
        run: |
          docker build -f Dockerfile.backend -t ${{ secrets.REGISTRY }}/grid-backend:${{ github.sha }} .
          docker push ${{ secrets.REGISTRY }}/grid-backend:${{ github.sha }}
      
      - name: Deploy to Production
        run: |
          docker-compose -f docker-compose.prod.yml up -d
```

### GitLab CI/CD
```yaml
# .gitlab-ci.yml
stages:
  - build
  - test
  - deploy

build:
  stage: build
  script:
    - docker build -f Dockerfile.backend -t $CI_REGISTRY_IMAGE/backend:$CI_COMMIT_SHA .
    - docker push $CI_REGISTRY_IMAGE/backend:$CI_COMMIT_SHA

deploy:
  stage: deploy
  script:
    - kubectl set image deployment/grid-backend backend=$CI_REGISTRY_IMAGE/backend:$CI_COMMIT_SHA
  only:
    - main
```

## 🔧 Maintenance

### Backup Strategy
```bash
# Backup data volumes
make backup

# Backup database
kubectl exec -it postgres-pod -- pg_dump grid > backup.sql

# Backup configuration
tar czf config-backup.tar.gz config.yaml .env ssl/
```

### Log Rotation
```yaml
# Configure log rotation in docker-compose.yml
services:
  backend:
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "3"
```

### Health Checks
```bash
# Automated health monitoring
curl -f http://localhost:8000/health || exit 1
curl -f http://localhost:3000/ || exit 1

# Set up monitoring alerts
# Configure Prometheus alerts for service downtime
```

## 🚨 Troubleshooting

### Common Issues

**Service Won't Start:**
```bash
# Check logs
docker-compose logs backend

# Check resource usage
docker stats

# Verify network connectivity
docker-compose exec backend ping redis
```

**Performance Issues:**
```bash
# Monitor resource usage
docker stats

# Check database connections
docker-compose exec redis redis-cli info clients

# Analyze logs for bottlenecks
grep -i "slow" logs/grid.log
```

**SSL Certificate Issues:**
```bash
# Verify certificate
openssl x509 -in ssl/your-domain.crt -text -noout

# Test SSL connection
openssl s_client -connect yourdomain.com:443

# Renew Let's Encrypt certificate
certbot renew --dry-run
```

---

For additional support, please refer to the [README.md](README.md) or create an issue on GitHub.