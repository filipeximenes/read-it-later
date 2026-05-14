.PHONY: install uninstall

install:
	uv tool install --reinstall .

uninstall:
	uv tool uninstall read-it-later
