IMQUIC_DIR := $(CURDIR)/deps/imquic
PICOQUIC_DIR := $(CURDIR)/deps/picoquic
THREEDGS_DIR := $(CURDIR)/deps/3dgs_over_moq
THREEDGS_FIXTURE := $(CURDIR)/build/imquic-3dgs-moq

.PHONY: init analyzer-check experiment-record-check reno-fairness-check reno-step-join-check 3dgs-deadline-check build build-3dgs-fixture l4s-timeseries-guest-check l4s-mininet-benchmark-guest-check l4s-dualpi2-reference-guest-check l4s-dualpi2-reference-qemu-check l4s-sustained-moq-check l4s-reno-fairness-check l4s-reno-step-join-check l4s-3dgs-deadline-check moq-loopback-check

init:
	git submodule update --init

analyzer-check:
	python3 tools/l4s/analyze_mininet_benchmark.py --self-test
	python3 tools/l4s/analyze_sustained_coexistence.py --self-test
	python3 tools/l4s/analyze_reno_fairness.py --self-test
	python3 tools/l4s/analyze_reno_step_join.py --self-test

experiment-record-check:
	python3 -m unittest discover -s tests -p 'test_experiment_metadata.py' -v

reno-fairness-check:
	python3 -m unittest discover -s tests -p 'test_reno_fairness.py' -v

reno-step-join-check:
	python3 -m unittest discover -s tests -p 'test_reno_step_join.py' -v

3dgs-deadline-check:
	python3 -m unittest discover -s tests -p 'test_3dgs_deadline.py' -v

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

build-3dgs-fixture: build
	mkdir -p $(CURDIR)/build
	$${CC:-cc} -std=c11 -O2 -Wall -Wextra \
		-I$(IMQUIC_DIR)/src \
		$$(pkg-config --cflags glib-2.0 libssl libcrypto jansson) \
		tests/3dgs-moq-test.c -o $(THREEDGS_FIXTURE) \
		-L$(IMQUIC_DIR)/src/.libs -limquic \
		$$(pkg-config --libs glib-2.0 libssl libcrypto jansson) -lm -pthread \
		-Wl,-rpath,'$$ORIGIN/../deps/imquic/src/.libs'

l4s-timeseries-guest-check:
	tools/l4s/run_timeseries_test.sh $(L4S_RESULT_DIR)

l4s-mininet-benchmark-guest-check:
	python3 tools/l4s/run_mininet_benchmark.py --output $(L4S_RESULT_DIR) $(L4S_MININET_ARGS)

l4s-dualpi2-reference-guest-check:
	@test -n "$(L4S_RESULT_DIR)" || { echo "L4S_RESULT_DIR is required" >&2; exit 2; }
	$(MAKE) analyzer-check
	$(MAKE) experiment-record-check
	$(MAKE) l4s-timeseries-guest-check L4S_RESULT_DIR="$(L4S_RESULT_DIR)/timeseries"
	@grep -E 'qdisc dualpi2|target|tupdate|alpha|beta|step_thresh|coupling_factor|classic_protection' \
		"$(L4S_RESULT_DIR)/timeseries/dualpi2-stats.txt" | tee "$(L4S_RESULT_DIR)/dualpi2-reference-parameters.txt"
	@grep -Eq 'target 15ms' "$(L4S_RESULT_DIR)/dualpi2-reference-parameters.txt" || { echo "unexpected DualPI2 Classic target" >&2; exit 1; }
	@grep -Eq 'tupdate 16ms' "$(L4S_RESULT_DIR)/dualpi2-reference-parameters.txt" || { echo "unexpected DualPI2 update interval" >&2; exit 1; }
	@grep -Eq 'step_thresh 1ms' "$(L4S_RESULT_DIR)/dualpi2-reference-parameters.txt" || { echo "unexpected DualPI2 L4S step threshold" >&2; exit 1; }
	@grep -Eq 'coupling_factor 2' "$(L4S_RESULT_DIR)/dualpi2-reference-parameters.txt" || { echo "unexpected DualPI2 coupling factor" >&2; exit 1; }
	$(MAKE) l4s-mininet-benchmark-guest-check L4S_RESULT_DIR="$(L4S_RESULT_DIR)/mininet"

l4s-dualpi2-reference-qemu-check:
	python3 tools/l4s/run_qemu_timeseries_test.py \
		--make-target l4s-dualpi2-reference-guest-check \
		--guest-result-name dualpi2-reference \
		--destination-prefix qemu-dualpi2-reference \
		$(L4S_QEMU_ARGS)

l4s-sustained-moq-check:
	python3 tools/l4s/run_sustained_coexistence.py --output $(L4S_RESULT_DIR) $(L4S_SUSTAINED_ARGS)

l4s-reno-fairness-check:
	python3 tools/l4s/run_reno_fairness.py --output $(L4S_RESULT_DIR) $(L4S_RENO_FAIRNESS_ARGS)

l4s-reno-step-join-check:
	python3 tools/l4s/run_reno_step_join.py --output $(L4S_RESULT_DIR) $(L4S_RENO_STEP_JOIN_ARGS)

l4s-3dgs-deadline-check: build-3dgs-fixture
	@test -n "$(THREEDGS_CACHE)" || { echo "THREEDGS_CACHE is required" >&2; exit 2; }
	@test -n "$(THREEDGS_TRACE)" || { echo "THREEDGS_TRACE is required" >&2; exit 2; }
	python3 tools/l4s/run_3dgs_deadline.py \
		--output $(L4S_RESULT_DIR) \
		--cache "$(THREEDGS_CACHE)" \
		--trace "$(THREEDGS_TRACE)" \
		--3dgs-dir "$(THREEDGS_DIR)" \
		$(THREEDGS_ARGS)

moq-loopback-check:
	python3 tools/l4s/run_sustained_moq_loopback.py
