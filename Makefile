.PHONY: test lint race demo serve

test:
	python main.py test

lint:
	ruff check src tests main.py

race:
	python main.py race

demo:
	python main.py order

serve:
	python main.py serve
