# Developer shortcuts for the Rotor package.
#
# These targets intentionally wrap the Poetry commands used in the README so
# local checks stay consistent across contributors and automation.
.PHONY: install test testnet testnet-e2e testnet-scenarios live-order-ack live-prices once lint lint-fix clean

install:
	poetry install

test:
	poetry run python -m pytest -q


testnet:
	ROTOR_RUN_TESTNET=1 poetry run python -m pytest -q tests/testnet

# These place real testnet orders, so they also need the operator's explicit
# acknowledgement; without it pytest would skip them and still exit 0.
testnet-e2e: live-order-ack
	ROTOR_RUN_TESTNET=1 ROTOR_RUN_TESTNET_E2E=1 poetry run python -m pytest -q tests/testnet

testnet-scenarios: live-order-ack
	ROTOR_RUN_TESTNET=1 ROTOR_RUN_TESTNET_SCENARIOS=1 poetry run python -m pytest -q tests/testnet/test_vl_runner_scenarios.py

live-order-ack:
	@test "$$ROTOR_TESTNET_ACK_LIVE_ORDER_RISK" = 1 || { \
		echo "This target places real testnet orders. Re-run with ROTOR_TESTNET_ACK_LIVE_ORDER_RISK=1."; \
		exit 1; }

live-prices:
	ROTOR_RUN_LIVE_PRICES=1 poetry run python -m pytest -q tests/live

# Single fire-once tick (what a Manus scheduled task runs). See docs/manus.md.
once:
	./scripts/manus_run_once.sh

lint:
	poetry run ruff check rotor tests

lint-fix:
	poetry run ruff check --fix rotor tests

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -type f -name '*.pyc' -delete
