IMQUIC_DIR := $(CURDIR)/deps/imquic
PICOQUIC_DIR := $(CURDIR)/deps/picoquic
THREEDGS_DIR := $(CURDIR)/deps/3dgs_over_moq
THREEDGS_FIXTURE := $(CURDIR)/build/imquic-3dgs-moq
THREEDGS_BACKGROUND_MBPS ?= 150
DYNAMIC_LAPIS_PYTHON ?= python3

.PHONY: init analyzer-check experiment-record-check reno-fairness-check reno-step-join-check 3dgs-deadline-check 3dgs-static-export 3dgs-static-export-check 3dgs-static-verify 3dgs-native-loopback-check 3dgs-training-init 3dgs-training-stage 3dgs-training-acceptance 3dgs-training-full build build-3dgs-fixture build-3dgs-fixture-only build-3dgs-native build-3dgs-native-only l4s-timeseries-guest-check l4s-mininet-benchmark-guest-check l4s-dualpi2-reference-guest-check l4s-dualpi2-reference-qemu-check l4s-sustained-moq-check l4s-reno-fairness-check l4s-reno-step-join-check l4s-3dgs-deadline-check l4s-3dgs-shared-check l4s-3dgs-priority-split-check l4s-3dgs-native-guest-check l4s-3dgs-native-qemu-check moq-loopback-check

init:
	git submodule update --init

# Optional CUDA path: deliberately excluded from normal init/build.
3dgs-training-init:
	git -C $(THREEDGS_DIR) submodule update --init deps/dynamic-lapis-gs
	git -C $(THREEDGS_DIR)/deps/dynamic-lapis-gs submodule update --init --recursive

3dgs-static-export:
	$(DYNAMIC_LAPIS_PYTHON) $(THREEDGS_DIR)/tools/export_static_ply.py \
		--input $(CURDIR)/data/point_cloud.ply \
		--output $(CURDIR)/results/3dgs/media/point-cloud

3dgs-static-export-check:
	PYTHONPATH=$(THREEDGS_DIR)/tools $(DYNAMIC_LAPIS_PYTHON) -m unittest discover \
		-s $(THREEDGS_DIR)/tools -p 'test_export_static_ply.py' -v

3dgs-static-verify:
	node $(THREEDGS_DIR)/tools/verify_static_bundle.mjs \
		$(CURDIR)/results/3dgs/media/point-cloud/manifest.json

3dgs-native-loopback-check: build-3dgs-native-only
	SGSS_IMQUIC_PUBLISHER=$(CURDIR)/build/sgss-imquic-publisher \
	SGSS_IMQUIC_SUBSCRIBER=$(CURDIR)/build/sgss-imquic-subscriber \
		node $(THREEDGS_DIR)/native/sgss-moq-client/native_loopback.mjs \
		$(CURDIR)/results/3dgs/media/point-cloud

3dgs-training-stage:
	$(DYNAMIC_LAPIS_PYTHON) $(THREEDGS_DIR)/tools/dynamic_lapis_8i.py stage --scene loot
	$(DYNAMIC_LAPIS_PYTHON) $(THREEDGS_DIR)/tools/dynamic_lapis_8i.py stage --scene redandblack

3dgs-training-acceptance: 3dgs-training-init
	$(DYNAMIC_LAPIS_PYTHON) $(THREEDGS_DIR)/tools/dynamic_lapis_8i.py all --scene loot
	node $(THREEDGS_DIR)/tools/verify_dynamic_bundle.mjs $(CURDIR)/results/3dgs/dynamic-lapis/media/loot/manifest.json
	$(DYNAMIC_LAPIS_PYTHON) $(THREEDGS_DIR)/tools/dynamic_lapis_8i.py all --scene redandblack
	node $(THREEDGS_DIR)/tools/verify_dynamic_bundle.mjs $(CURDIR)/results/3dgs/dynamic-lapis/media/redandblack/manifest.json

3dgs-training-full: 3dgs-training-init
	@test -n "$(SCENE)" || { echo "SCENE=loot or SCENE=redandblack is required" >&2; exit 2; }
	$(DYNAMIC_LAPIS_PYTHON) $(THREEDGS_DIR)/tools/dynamic_lapis_8i.py all --scene "$(SCENE)" --full
	node $(THREEDGS_DIR)/tools/verify_dynamic_bundle.mjs $(CURDIR)/results/3dgs/dynamic-lapis/media/$(SCENE)/manifest.json

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

build-3dgs-fixture: build build-3dgs-fixture-only

build-3dgs-fixture-only:
	mkdir -p $(CURDIR)/build
	$${CC:-cc} -std=c11 -O2 -Wall -Wextra \
		-I$(IMQUIC_DIR)/src \
		$$(pkg-config --cflags glib-2.0 libssl libcrypto jansson) \
		tests/3dgs-moq-test.c -o $(THREEDGS_FIXTURE) \
		-L$(IMQUIC_DIR)/src/.libs -limquic \
		$$(pkg-config --libs glib-2.0 libssl libcrypto jansson) -lm -pthread \
		-Wl,-rpath,'$$ORIGIN/../deps/imquic/src/.libs'

build-3dgs-native: build build-3dgs-native-only

build-3dgs-native-only:
	mkdir -p $(CURDIR)/build
	$${CC:-cc} -std=c11 -O2 -Wall -Wextra -I$(IMQUIC_DIR)/src \
		$$(pkg-config --cflags glib-2.0 libssl libcrypto jansson) \
		$(THREEDGS_DIR)/native/sgss-moq-client/imquic_subscriber.c \
		-o $(CURDIR)/build/sgss-imquic-subscriber -L$(IMQUIC_DIR)/src/.libs -limquic \
		$$(pkg-config --libs glib-2.0 libssl libcrypto jansson) -lm -pthread \
		-Wl,-rpath,'$$ORIGIN/../deps/imquic/src/.libs'
	$${CC:-cc} -std=c11 -O2 -Wall -Wextra -I$(IMQUIC_DIR)/src \
		$$(pkg-config --cflags glib-2.0 libssl libcrypto jansson) \
		$(THREEDGS_DIR)/native/sgss-moq-client/imquic_publisher.c \
		-o $(CURDIR)/build/sgss-imquic-publisher -L$(IMQUIC_DIR)/src/.libs -limquic \
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

l4s-3dgs-shared-check: build-3dgs-fixture-only
	@test -n "$(THREEDGS_BUNDLE)" || { echo "THREEDGS_BUNDLE is required" >&2; exit 2; }
	@test -n "$(L4S_RESULT_DIR)" || { echo "L4S_RESULT_DIR is required" >&2; exit 2; }
	python3 tools/l4s/run_3dgs_deadline.py \
		--output "$(L4S_RESULT_DIR)" \
		--source-bundle "$(THREEDGS_BUNDLE)" \
		--deadlines-ms 30000 \
		--modes reno,prague \
		--repetitions 1 \
		--bottleneck 300mbit \
		--htb-burst 512k \
		--dualpi2-target 15ms \
		--dualpi2-tupdate 16ms \
		--dualpi2-step-thresh 1ms \
		--background-mbps $(THREEDGS_BACKGROUND_MBPS) \
		--background-congestion bbr2 \
		--background-warmup-seconds 2 \
		--no-render \
		$(THREEDGS_ARGS)

l4s-3dgs-priority-split-check: build-3dgs-fixture-only
	@test -n "$(THREEDGS_BUNDLE)" || { echo "THREEDGS_BUNDLE is required" >&2; exit 2; }
	@test -n "$(L4S_RESULT_DIR)" || { echo "L4S_RESULT_DIR is required" >&2; exit 2; }
	modprobe sch_dualpi2
	python3 tools/l4s/run_3dgs_priority_split.py \
		--output "$(L4S_RESULT_DIR)" \
		--source-bundle "$(THREEDGS_BUNDLE)" \
		--importance native-tier \
		--deadline-ms 30000 \
		--repetitions 3 \
		$(THREEDGS_PRIORITY_ARGS)

l4s-3dgs-native-guest-check: build-3dgs-native-only
	@test -n "$(L4S_RESULT_DIR)" || { echo "L4S_RESULT_DIR is required" >&2; exit 2; }
	@test -n "$(THREEDGS_MEDIA_ARCHIVE)" || { echo "THREEDGS_MEDIA_ARCHIVE is required" >&2; exit 2; }
	@test -x "$(THREEDGS_NODE)" || { echo "THREEDGS_NODE is required and must be executable" >&2; exit 2; }
	@test -n "$(THREEDGS_NODE_MODULES_ARCHIVE)" || { echo "THREEDGS_NODE_MODULES_ARCHIVE is required" >&2; exit 2; }
	rm -rf $(CURDIR)/results/3dgs/qemu-input
	mkdir -p $(CURDIR)/results/3dgs/qemu-input
	tar -xzf "$(THREEDGS_MEDIA_ARCHIVE)" -C $(CURDIR)/results/3dgs/qemu-input
	rm -rf $(THREEDGS_DIR)/native/sgss-moq-client/node_modules
	tar -xzf "$(THREEDGS_NODE_MODULES_ARCHIVE)" -C $(THREEDGS_DIR)/native/sgss-moq-client
	PATH="$(abspath $(dir $(THREEDGS_NODE))):$(PATH)" python3 tools/l4s/run_3dgs_native_guest.py \
		--bundle $(CURDIR)/results/3dgs/qemu-input/point-cloud \
		--output "$(L4S_RESULT_DIR)" $(THREEDGS_NATIVE_ARGS)

l4s-3dgs-native-qemu-check:
	python3 tools/l4s/run_qemu_3dgs_native.py --bundle $(CURDIR)/results/3dgs/media/point-cloud \
		$(THREEDGS_QEMU_ARGS)

moq-loopback-check:
	python3 tools/l4s/run_sustained_moq_loopback.py
