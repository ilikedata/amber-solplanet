PYTHON := pipenv run python
SCRIPT := solplanet_price_controller.py

.PHONY: install check run dry-run

install:
	pipenv install

check:
	$(PYTHON) -m py_compile $(SCRIPT)

run:
	$(PYTHON) $(SCRIPT) --price-source amber --loop --loop-seconds 300 --apply

dry-run:
	$(PYTHON) $(SCRIPT) --price-source amber --loop --loop-seconds 300
