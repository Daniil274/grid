# GRID Agent System - Makefile
# Comprehensive build and development automation

.PHONY: help build run stop clean test lint format setup dev prod logs shell

# Default target
.DEFAULT_GOAL := help

# Variables
DOCKER_COMPOSE := docker-compose
DOCKER_COMPOSE_DEV := docker-compose -f docker-compose.yml -f docker-compose.dev.yml
PYTHON := python3
NPM := npm

# Colors for output
RED := \033[0;31m
GREEN := \033[0;32m
YELLOW := \033[1;33m
BLUE := \033[0;34m
NC := \033[0m # No Color

define print_color
	@echo "$(2)$(1)$(NC)"
endef

help: ## Show this help message
	@echo "$(GREEN)GRID Agent System - Development Commands$(NC)"
	@echo ""
	@echo "$(YELLOW)Usage:$(NC) make [target]"
	@echo ""
	@echo "$(YELLOW)Targets:$(NC)"
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  $(BLUE)%-15s$(NC) %s\n", $$1, $$2}' $(MAKEFILE_LIST)

setup: ## Initial project setup
	$(call print_color,"🚀 Setting up GRID Agent System...",$(GREEN))
	@echo "Installing backend dependencies..."
	$(PYTHON) -m pip install --upgrade pip
	$(PYTHON) -m pip install -r requirements-api.txt
	$(PYTHON) -m pip install -r requirements.txt
	@echo "Installing frontend dependencies..."
	cd frontend && $(NPM) install
	@echo "Creating config file..."
	@if [ ! -f config.yaml ]; then cp config.yaml.example config.yaml; fi
	@echo "Creating .env file..."
	@if [ ! -f .env ]; then \
		echo "OPENROUTER_API_KEY=your_key_here" > .env; \
		echo "OPENAI_API_KEY=your_key_here" >> .env; \
		echo "ANTHROPIC_API_KEY=your_key_here" >> .env; \
	fi
	$(call print_color,"✅ Setup complete! Edit .env and config.yaml as needed.",$(GREEN))

build: ## Build all Docker images
	$(call print_color,"🔨 Building Docker images...",$(BLUE))
	$(DOCKER_COMPOSE) build --parallel

build-dev: ## Build development Docker images
	$(call print_color,"🔨 Building development Docker images...",$(BLUE))
	$(DOCKER_COMPOSE_DEV) build --parallel

run: ## Start production services
	$(call print_color,"🚀 Starting production services...",$(GREEN))
	$(DOCKER_COMPOSE) up -d
	@echo ""
	@echo "$(GREEN)Services started:$(NC)"
	@echo "  Frontend: http://localhost:3000"
	@echo "  Backend API: http://localhost:8000"
	@echo "  API Docs: http://localhost:8000/docs"

dev: ## Start development environment
	$(call print_color,"🛠️  Starting development environment...",$(YELLOW))
	$(DOCKER_COMPOSE_DEV) up -d
	@echo ""
	@echo "$(YELLOW)Development services started:$(NC)"
	@echo "  Frontend: http://localhost:3000"
	@echo "  Backend API: http://localhost:8000"
	@echo "  Redis: localhost:6379"
	@echo "  PostgreSQL: localhost:5432"
	@echo "  MailHog: http://localhost:8025"
	@echo "  Jaeger: http://localhost:16686"

stop: ## Stop all services
	$(call print_color,"🛑 Stopping services...",$(RED))
	$(DOCKER_COMPOSE) down
	$(DOCKER_COMPOSE_DEV) down

restart: stop run ## Restart production services

restart-dev: stop dev ## Restart development services

logs: ## Show service logs
	$(DOCKER_COMPOSE) logs -f

logs-dev: ## Show development service logs
	$(DOCKER_COMPOSE_DEV) logs -f

logs-backend: ## Show backend logs only
	$(DOCKER_COMPOSE) logs -f backend

logs-frontend: ## Show frontend logs only
	$(DOCKER_COMPOSE) logs -f frontend

shell-backend: ## Open shell in backend container
	$(DOCKER_COMPOSE) exec backend bash

shell-frontend: ## Open shell in frontend container
	$(DOCKER_COMPOSE) exec frontend sh

shell-redis: ## Open Redis CLI
	$(DOCKER_COMPOSE) exec redis redis-cli

test: ## Run all tests
	$(call print_color,"🧪 Running tests...",$(BLUE))
	@echo "Running backend tests..."
	$(PYTHON) -m pytest tests/ -v
	@echo "Running frontend tests..."
	cd frontend && $(NPM) test

test-backend: ## Run backend tests only
	$(call print_color,"🧪 Running backend tests...",$(BLUE))
	$(PYTHON) -m pytest tests/ -v --cov=. --cov-report=html

test-frontend: ## Run frontend tests only
	$(call print_color,"🧪 Running frontend tests...",$(BLUE))
	cd frontend && $(NPM) test

lint: ## Run linting on all code
	$(call print_color,"🔍 Running linters...",$(BLUE))
	@echo "Linting backend..."
	$(PYTHON) -m flake8 . --count --select=E9,F63,F7,F82 --show-source --statistics
	$(PYTHON) -m flake8 . --count --exit-zero --max-complexity=10 --max-line-length=127 --statistics
	@echo "Linting frontend..."
	cd frontend && $(NPM) run lint

format: ## Format all code
	$(call print_color,"✨ Formatting code...",$(BLUE))
	@echo "Formatting backend..."
	$(PYTHON) -m black . --line-length 100
	@echo "Formatting frontend..."
	cd frontend && $(NPM) run format

type-check: ## Run type checking
	$(call print_color,"🔍 Running type checks...",$(BLUE))
	@echo "Type checking backend..."
	$(PYTHON) -m mypy . --ignore-missing-imports
	@echo "Type checking frontend..."
	cd frontend && $(NPM) run type-check

clean: ## Clean up containers, images, and volumes
	$(call print_color,"🧹 Cleaning up...",$(RED))
	$(DOCKER_COMPOSE) down -v --remove-orphans
	$(DOCKER_COMPOSE_DEV) down -v --remove-orphans
	docker system prune -f
	@echo "Cleaned up Docker resources"

clean-all: clean ## Clean everything including images
	$(call print_color,"🧹 Deep cleaning...",$(RED))
	docker image prune -a -f
	docker volume prune -f

status: ## Show service status
	$(call print_color,"📊 Service status:",$(BLUE))
	$(DOCKER_COMPOSE) ps

status-dev: ## Show development service status
	$(call print_color,"📊 Development service status:",$(BLUE))
	$(DOCKER_COMPOSE_DEV) ps

health: ## Check service health
	$(call print_color,"🏥 Checking service health...",$(BLUE))
	@echo "Backend health:"
	@curl -s http://localhost:8000/health | jq . || echo "Backend not accessible"
	@echo ""
	@echo "Frontend health:"
	@curl -s -o /dev/null -w "%{http_code}" http://localhost:3000 || echo "Frontend not accessible"

backup: ## Backup data volumes
	$(call print_color,"💾 Creating backup...",$(BLUE))
	mkdir -p backups
	docker run --rm -v grid_agent_data:/data -v $(PWD)/backups:/backup alpine tar czf /backup/agent_data_$(shell date +%Y%m%d_%H%M%S).tar.gz -C /data .
	docker run --rm -v grid_redis_data:/data -v $(PWD)/backups:/backup alpine tar czf /backup/redis_data_$(shell date +%Y%m%d_%H%M%S).tar.gz -C /data .
	$(call print_color,"✅ Backup created in ./backups/",$(GREEN))

monitor: ## Start monitoring stack
	$(call print_color,"📊 Starting monitoring stack...",$(BLUE))
	$(DOCKER_COMPOSE) --profile monitoring up -d
	@echo ""
	@echo "$(BLUE)Monitoring services:$(NC)"
	@echo "  Prometheus: http://localhost:9090"
	@echo "  Grafana: http://localhost:3001 (admin/admin)"

install-hooks: ## Install git hooks
	$(call print_color,"🪝 Installing git hooks...",$(BLUE))
	cp scripts/pre-commit .git/hooks/
	chmod +x .git/hooks/pre-commit
	$(call print_color,"✅ Git hooks installed",$(GREEN))

docs: ## Generate documentation
	$(call print_color,"📚 Generating documentation...",$(BLUE))
	$(PYTHON) -m mkdocs build
	@echo "Documentation generated in site/"

docs-serve: ## Serve documentation locally
	$(call print_color,"📚 Serving documentation...",$(BLUE))
	$(PYTHON) -m mkdocs serve

production: ## Deploy to production
	$(call print_color,"🚀 Deploying to production...",$(GREEN))
	$(DOCKER_COMPOSE) --profile production up -d
	@echo ""
	@echo "$(GREEN)Production deployment complete!$(NC)"
	@echo "  Main site: http://localhost"
	@echo "  API: http://localhost/api"

# Database operations
db-reset: ## Reset development database
	$(call print_color,"🗄️  Resetting database...",$(YELLOW))
	$(DOCKER_COMPOSE_DEV) exec db dropdb -U grid grid_dev --if-exists
	$(DOCKER_COMPOSE_DEV) exec db createdb -U grid grid_dev

db-shell: ## Open database shell
	$(DOCKER_COMPOSE_DEV) exec db psql -U grid grid_dev

# Security scanning
security-scan: ## Run security scans
	$(call print_color,"🔒 Running security scans...",$(BLUE))
	@echo "Scanning Python dependencies..."
	$(PYTHON) -m safety check --json || true
	@echo "Scanning for secrets..."
	docker run --rm -v $(PWD):/app trufflesecurity/trufflehog:latest filesystem /app --only-verified

# Performance testing
perf-test: ## Run performance tests
	$(call print_color,"⚡ Running performance tests...",$(BLUE))
	@echo "Starting load test..."
	@command -v ab >/dev/null 2>&1 || { echo "Apache Bench (ab) required for load testing"; exit 1; }
	ab -n 100 -c 10 http://localhost:8000/health

# Quick development commands
quick-start: setup build-dev dev ## Quick start for new developers

quick-test: lint type-check test ## Quick test suite

quick-deploy: build run ## Quick production deployment