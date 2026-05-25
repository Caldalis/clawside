.PHONY: build dev test

build:
	docker build -t clawside-agent:latest -f container/Dockerfile .

dev:
	python -m src.main

test:
	pytest tests/ -v
