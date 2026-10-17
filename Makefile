IMQUIC_DIR := $(CURDIR)/deps/imquic
PICOQUIC_DIR := $(CURDIR)/deps/picoquic

.DEFAULT_GOAL := cpu-check
.PHONY: init cpu-check experiment-record-check build build-3dgs-priority-mvp build-3dgs-priority-mvp-only 3dgs-priority-mvp-check camera-trace-player-check priority-audit-loopback-check l4s-camera-player-guest-check

init:
	git submodule update --init deps/imquic deps/picoquic deps/gaussian-player

cpu-check:
	node tools/l4s/check_cpu_prerequisites.mjs
	cd $(CURDIR)/deps/gaussian-player && npm test && npm run build
	python3 -m unittest tests.test_camera_priority_trace tests.test_camera_trace_player tests.test_experiment_metadata tests.test_player_workflow tests.test_priority_publisher_supervisor tests.test_tcp_forward
	node --test tests/test_priority_ws_bridge.mjs tests/test_player_playback.mjs

experiment-record-check:
	python3 -m unittest discover -s tests -p 'test_experiment_metadata.py' -v

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

build-3dgs-priority-mvp: build build-3dgs-priority-mvp-only

build-3dgs-priority-mvp-only:
	mkdir -p $(CURDIR)/build
	$${CC:-cc} -std=gnu11 -O2 -Wall -Wextra -Werror -I$(IMQUIC_DIR)/src \
		$$(pkg-config --cflags glib-2.0 jansson) tools/l4s/priority_moq_sidecar.c \
		-o $(CURDIR)/build/priority-moq-sidecar -L$(IMQUIC_DIR)/src/.libs -limquic \
		$$(pkg-config --libs glib-2.0 jansson) -lm -pthread \
		-Wl,-rpath,'$$ORIGIN/../deps/imquic/src/.libs'
	$${CC:-cc} -std=gnu11 -O2 -Wall -Wextra -Werror -I$(IMQUIC_DIR)/src \
		$$(pkg-config --cflags glib-2.0 jansson) tools/l4s/priority_moq_publisher.c \
		-o $(CURDIR)/build/priority-moq-publisher -L$(IMQUIC_DIR)/src/.libs -limquic \
		$$(pkg-config --libs glib-2.0 jansson) -lm -pthread \
		-Wl,-rpath,'$$ORIGIN/../deps/imquic/src/.libs'

3dgs-priority-mvp-check:
	mkdir -p results/native-check
	cd $(CURDIR)/deps/gaussian-player && npm test && npm run build
	for source in tools/l4s/priority_moq_sidecar.c tools/l4s/priority_moq_publisher.c; do \
		$${CC:-cc} -std=gnu11 -fsyntax-only -Wall -Wextra -Werror -I$(IMQUIC_DIR)/src \
			$$(pkg-config --cflags glib-2.0 jansson) $$source || exit; \
	done
	$${CC:-cc} -std=c11 -Wall -Wextra -Werror -ffunction-sections -fdata-sections \
		-I$(IMQUIC_DIR)/src -I$(IMQUIC_DIR) $$(pkg-config --cflags glib-2.0 jansson) \
		tests/priority_moq_sidecar_unit.c -Wl,--gc-sections -o $(CURDIR)/results/native-check/priority-moq-sidecar-unit \
		$$(pkg-config --libs glib-2.0 jansson)
	$(CURDIR)/results/native-check/priority-moq-sidecar-unit
	node --check tools/l4s/priority_ws_bridge.mjs

camera-trace-player-check: 3dgs-priority-mvp-check
	python3 -m unittest tests.test_camera_trace_player
	node --check tools/l4s/camera_trace_browser_runner.mjs
	python3 -m py_compile tools/l4s/camera_trace_player_server.py tools/l4s/run_camera_trace_player.py

priority-audit-loopback-check: build-3dgs-priority-mvp-only
	cd $(IMQUIC_DIR)/examples && $(MAKE) imquic-moq-relay
	cd $(CURDIR)/deps/gaussian-player && npm run build
	python3 tools/l4s/run_priority_audit_loopback.py

CAMERA_PLAYER_ADMISSION_DELAY_US ?= 0
CAMERA_PLAYER_BOTTLENECK ?=
CAMERA_PLAYER_TOPOLOGY_SCRIPT ?= tools/l4s/player_topology.py
CAMERA_PLAYER_PERSISTENT ?= 0
l4s-camera-player-guest-check: build-3dgs-priority-mvp-only
	@test -n "$(L4S_RESULT_DIR)" -a -n "$(CAMERA_PRIORITY_SCENE_ARCHIVE)"
	@test -x "$(CAMERA_PRIORITY_NODE)"
	mkdir -p $(CURDIR)/results/3dgs/camera-player-input
	tar -xf "$(CAMERA_PRIORITY_SCENE_ARCHIVE)" -C $(CURDIR)/results/3dgs/camera-player-input
	CAMERA_PRIORITY_NODE="$(abspath $(CAMERA_PRIORITY_NODE))" python3 tools/l4s/run_camera_player_guest.py \
		--manifest $(CURDIR)/results/3dgs/camera-player-input/manifest-v2.json \
		--output "$(L4S_RESULT_DIR)" --transport-mode "$(CAMERA_PLAYER_MODE)" \
		--duration "$(CAMERA_PLAYER_DURATION)" --origin "$(CAMERA_PLAYER_ORIGIN)" \
		--admission-delay-us "$(CAMERA_PLAYER_ADMISSION_DELAY_US)" --bottleneck "$(CAMERA_PLAYER_BOTTLENECK)" \
		--topology-script "$(CAMERA_PLAYER_TOPOLOGY_SCRIPT)" \
		$(if $(filter 1,$(CAMERA_PLAYER_PERSISTENT)),--persistent,)
