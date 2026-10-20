.PHONY: probe
probe:
	mkdir -p build
	$(CC) -O2 -Wall -Wextra -Ideps/imquic/src $$(pkg-config --cflags glib-2.0 jansson) inputs/sustained-moq-options.c -o build/sustained-probe -Ldeps/imquic/src/.libs -limquic $$(pkg-config --libs glib-2.0 jansson) -Wl,-rpath,'$$ORIGIN/../deps/imquic/src/.libs'
	PROBE_BINARY="$(CURDIR)/build/sustained-probe" PROBE_SENDER_OPTIONS="$(PROBE_SENDER_OPTIONS)" python3 inputs/sustained_probe.py --output "$(L4S_RESULT_DIR)" --rate "$(PROBE_RATE)"
