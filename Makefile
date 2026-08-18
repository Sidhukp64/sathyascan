\
.PHONY: infra-up infra-down infra-logs

infra-up:
	docker compose -f infra/docker-compose.yml up -d

infra-down:
	docker compose -f infra/docker-compose.yml down

infra-logs:
	docker compose -f infra/docker-compose.yml logs -f

# Backend/dashboard/test targets are added starting Phase 0 of docs/phased-plan.md,
# once there is application code and a package manager config to invoke.
