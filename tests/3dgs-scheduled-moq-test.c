#define main legacy_fixture_main
#include "3dgs-moq-test.c"
#undef main

static const char *release_schedule_path;

static int read_release_ms(FILE *schedule, uint64_t *release_ms) {
	char line[128];
	if(fgets(line, sizeof(line), schedule) == NULL)
		return -1;
	char *end = NULL;
	unsigned long long value = strtoull(line, &end, 10);
	if(end == line)
		return -1;
	while(*end == ' ' || *end == '\t' || *end == '\r' || *end == '\n')
		end++;
	if(*end != '\0')
		return -1;
	*release_ms = (uint64_t)value;
	return 0;
}

static int schedule_has_extra_values(FILE *schedule) {
	char line[128];
	while(fgets(line, sizeof(line), schedule) != NULL) {
		char *cursor = line;
		while(*cursor == ' ' || *cursor == '\t' || *cursor == '\r' || *cursor == '\n')
			cursor++;
		if(*cursor != '\0')
			return 1;
	}
	return 0;
}

static int run_scheduled_publisher(
		const char *bind_address, uint16_t port, const char *mode) {
	imquic_congestion_controller cc;
	imquic_ecn_mode ecn;
	if(parse_mode(mode, &cc, &ecn) < 0 || imquic_init(NULL) < 0)
		return 1;
	imquic_server *server = imquic_create_moq_server("3dgs-moq-scheduled-publisher",
		IMQUIC_CONFIG_INIT, IMQUIC_CONFIG_TLS_CERT, CERT_PATH,
		IMQUIC_CONFIG_TLS_KEY, KEY_PATH, IMQUIC_CONFIG_LOCAL_BIND, bind_address,
		IMQUIC_CONFIG_LOCAL_PORT, port, IMQUIC_CONFIG_CONGESTION_CONTROL, cc,
		IMQUIC_CONFIG_ECN, ecn, IMQUIC_CONFIG_RAW_QUIC, TRUE,
		IMQUIC_CONFIG_MOQ_VERSION, IMQUIC_MOQ_VERSION_19, IMQUIC_CONFIG_DONE, NULL);
	if(server == NULL)
		return 1;

	imquic_set_new_moq_connection_cb(server, new_connection);
	imquic_set_moq_ready_cb(server, moq_ready);
	imquic_set_incoming_subscribe_cb(server, incoming_subscribe);
	imquic_set_publish_accepted_cb(server, publish_accepted);
	imquic_set_publish_error_cb(server, publish_error);
	imquic_set_connection_failed_cb(server, connection_failed);
	imquic_set_moq_connection_gone_cb(server, connection_gone);
	imquic_start_endpoint(server);

	while(!g_atomic_int_get(&stop_requested) && !g_atomic_int_get(&publishing))
		g_usleep(1000);

	FILE *source = NULL, *metrics = NULL, *schedule = NULL;
	if(!g_atomic_int_get(&failed) && open_source_bundle(&source) < 0)
		fail_case("could not open source scene bundle");
	if(!g_atomic_int_get(&failed)) {
		schedule = fopen(release_schedule_path, "r");
		if(schedule == NULL)
			fail_case("could not open release schedule");
	}
	if(metrics_path != NULL) {
		metrics = fopen(metrics_path, "w");
		if(metrics != NULL)
			fputs("time_us,rtt_us,cwnd_bytes,bytes_in_flight,queued_stream_bytes,"
				"pacing_Bps,ect0_packets,ect1_packets,ce_packets,alpha_numerator,"
				"alpha_denominator\n", metrics);
	}

	gint64 started = g_get_monotonic_time();
	gint64 started_real_us = g_get_real_time();
	gint64 deadline = started + (gint64)deadline_ms * 1000;
	gint64 next_metric = started;
	gboolean source_done = FALSE;
	uint64_t next_release_ms = 0;
	uint64_t scheduled_records = 0;
	if(!g_atomic_int_get(&failed) && source_objects > 0) {
		if(read_release_ms(schedule, &next_release_ms) < 0)
			fail_case("release schedule has fewer rows than source bundle");
	}

	while(!g_atomic_int_get(&stop_requested) &&
			!g_atomic_int_get(&failed) &&
			g_get_monotonic_time() < deadline) {
		gint64 now = g_get_monotonic_time();
		if(now >= next_metric) {
			write_metric(metrics, started);
			next_metric += METRIC_INTERVAL_US;
		}
		if(source_done) {
			imquic_transport_metrics transport = {0};
			if(connection != NULL &&
					imquic_get_transport_metrics(connection, &transport) == 0 &&
					transport.bytes_in_flight == 0 &&
					transport.queued_stream_bytes == 0)
				break;
			g_usleep(1000);
			continue;
		}

		gint64 release_at = started + (gint64)next_release_ms * 1000;
		if(now < release_at) {
			gint64 sleep_us = release_at - now;
			g_usleep((gulong)(sleep_us > 1000 ? 1000 : sleep_us));
			continue;
		}

		imquic_transport_metrics transport = {0};
		if(connection == NULL ||
				imquic_get_transport_metrics(connection, &transport) < 0 ||
				transport.bytes_in_flight + transport.queued_stream_bytes >= MAX_QUEUED_BYTES) {
			g_usleep(1000);
			continue;
		}

		uint8_t *payload = NULL;
		uint32_t length = 0;
		int read_status = read_record(source, &payload, &length);
		if(read_status != 0) {
			fail_case("source bundle ended before declared object count");
			break;
		}
		uint64_t subgroup_id = embedded_subgroup(payload, length);
		uint64_t object_id = outer_object_ids[subgroup_id]++;
		imquic_moq_object object = {0};
		object.request_id = publish_request_id;
		object.track_alias = publish_track_alias;
		object.group_id = 0;
		object.subgroup_id = subgroup_id;
		object.object_id = object_id;
		object.priority = subgroup_id == 0 ? 0 : (subgroup_id == 1 ? 64 : 128);
		object.payload = payload;
		object.payload_len = length;
		object.delivery = IMQUIC_MOQ_USE_SUBGROUP;
		object.first_of_subgroup = object_id == 0;
		if(imquic_moq_send_object(connection, &object) < 0)
			fail_case("could not queue scheduled 3dgs object");
		else {
			queued_objects++;
			queued_bytes += length;
		}
		free(payload);
		scheduled_records++;

		if(scheduled_records >= source_objects) {
			source_done = TRUE;
			if(schedule_has_extra_values(schedule))
				fail_case("release schedule has more rows than source bundle");
		} else if(read_release_ms(schedule, &next_release_ms) < 0) {
			fail_case("release schedule has fewer rows than source bundle");
		}
	}

	write_metric(metrics, started);
	if(g_atomic_int_get(&publishing) && connection != NULL)
		imquic_moq_publish_done(connection, publish_request_id,
			IMQUIC_MOQ_PUBDONE_SUBSCRIPTION_ENDED,
			source_done ? "scheduled scene complete" : "scheduled scene deadline elapsed");
	if(source != NULL)
		fclose(source);
	if(metrics != NULL)
		fclose(metrics);
	if(schedule != NULL)
		fclose(schedule);

	if(result_path != NULL) {
		FILE *result = fopen(result_path, "w");
		if(result != NULL) {
			fprintf(result,
				"{\"source_objects\":%" PRIu64 ",\"source_bytes\":%" PRIu64
				",\"queued_objects\":%" PRIu64 ",\"queued_bytes\":%" PRIu64
				",\"deadline_ms\":%u,\"source_fully_queued\":%s"
				",\"publisher_started_epoch_us\":%" G_GINT64_FORMAT
				",\"scheduled\":true,\"validated\":%s}\n",
				source_objects, source_bytes, queued_objects, queued_bytes, deadline_ms,
				source_done ? "true" : "false", started_real_us,
				g_atomic_int_get(&failed) ? "false" : "true");
			fclose(result);
		}
	}

	close_active_connection("3dgs scheduled publisher complete");
	imquic_shutdown_endpoint(server);
	imquic_deinit();
	return g_atomic_int_get(&failed) ? 1 : 0;
}

int main(int argc, char **argv) {
	if(argc == 10 && !strcmp(argv[1], "publisher-scheduled")) {
		moq_namespace.buffer = (uint8_t *)NAMESPACE_NAME;
		moq_namespace.length = strlen(NAMESPACE_NAME);
		moq_track.buffer = (uint8_t *)TRACK_NAME;
		moq_track.length = strlen(TRACK_NAME);
		bundle_path = argv[5];
		deadline_ms = (uint32_t)strtoul(argv[6], NULL, 10);
		if(deadline_ms == 0)
			return 2;
		metrics_path = strcmp(argv[7], "-") ? argv[7] : NULL;
		result_path = argv[8];
		release_schedule_path = argv[9];
		return run_scheduled_publisher(
			argv[2], (uint16_t)strtoul(argv[3], NULL, 10), argv[4]);
	}
	return legacy_fixture_main(argc, argv);
}
