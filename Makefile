PYTHON := pipenv run python
SCRIPT := solplanet_price_controller.py

.PHONY: install check test run dry-run

install:
	pipenv install

check:
	$(PYTHON) -m py_compile $(SCRIPT)
	$(PYTHON) -m unittest discover -s tests -p 'test_*.py'

test:
	$(PYTHON) -m unittest discover -s tests -p 'test_*.py'

run:
	$(PYTHON) $(SCRIPT) --price-source amber --loop --loop-seconds 60 --apply --discharge-min-soc 55

dry-run:
	$(PYTHON) $(SCRIPT) --price-source amber --loop --loop-seconds 60 --discharge-min-soc 55
