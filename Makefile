IMQUIC_DIR := $(CURDIR)/deps/imquic
PICOQUIC_DIR := $(CURDIR)/deps/picoquic

.PHONY: init analyzer-check reno-fairness-check build l4s-timeseries-guest-check l4s-mininet-benchmark-guest-check l4s-sustained-moq-check l4s-reno-fairness-check moq-loopback-check

init:
	git submodule update --init

analyzer-check:
	python3 tools/l4s/analyze_mininet_benchmark.py --self-test
	python3 tools/l4s/analyze_sustained_coexistence.py --self-test
	python3 tools/l4s/analyze_reno_fairness.py --self-test

reno-fairness-check:
	python3 -m unittest discover -s tests -p 'test_reno_fairness.py' -v

build: init
	cmake -S $(PICOQUIC_DIR) -B $(PICOQUIC_DIR)/build \
		-DCMAKE_POSITION_INDEPENDENT_CODE=ON -DPICOQUIC_FETCH_PTLS=Y
	cmake --build $(PICOQUIC_DIR)/build --target picoquic-core picoquic-log picohttp-core -j$${JOBS:-$$(nproc)}
	for library in libpicoquic-core.a libpicoquic-log.a libpicohttp-core.a; do \
		ln -sf build/$$library $(PICOQUIC_DIR)/$$library; \
	done
	mkdir -p $(PICOQUIC_DIR)/_deps
	ln -sfn ../build/_deps/picotls-build $(PICOQUIC_DIR)/_deps/picotls-build
	cd $(IMQUIC_DIR) && autoreconf -fi && ./configure --with-picoquic=$(PICOQUIC_DIR) --enable-moq-examples && make -j$${JOBS:-$$(nproc)}; status=$$?; \
		for library in libpicoquic-core.a libpicoquic-log.a libpicohttp-core.a; do \
			if [ -L $(PICOQUIC_DIR)/$$library ]; then unlink $(PICOQUIC_DIR)/$$library; fi; \
		done; \
		if [ -L $(PICOQUIC_DIR)/_deps/picotls-build ]; then unlink $(PICOQUIC_DIR)/_deps/picotls-build; fi; \
		if [ -d $(PICOQUIC_DIR)/_deps ] && [ -z "$$(find $(PICOQUIC_DIR)/_deps -mindepth 1 -maxdepth 1 -print -quit)" ]; then rmdir $(PICOQUIC_DIR)/_deps; fi; \
		exit $$status

l4s-timeseries-guest-check:
	tools/l4s/run_timeseries_test.sh $(L4S_RESULT_DIR)

l4s-mininet-benchmark-guest-check:
	python3 tools/l4s/run_mininet_benchmark.py --output $(L4S_RESULT_DIR)

l4s-sustained-moq-check:
	python3 tools/l4s/run_sustained_coexistence.py --output $(L4S_RESULT_DIR) $(L4S_SUSTAINED_ARGS)

l4s-reno-fairness-check:
	python3 tools/l4s/run_reno_fairness.py --output $(L4S_RESULT_DIR) $(L4S_RENO_FAIRNESS_ARGS)

moq-loopback-check:
	python3 tools/l4s/run_sustained_moq_loopback.py
