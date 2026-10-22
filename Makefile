.PHONY: install run worker seed test check migrate
install:
	python -m pip install -r requirements-dev.txt
run:
	uvicorn app.main:app --reload
worker:
	python -m app.worker
seed:
	python -m app.seed
test:
	pytest -q
check:
	python -m compileall -q app tests
migrate:
	alembic upgrade head
