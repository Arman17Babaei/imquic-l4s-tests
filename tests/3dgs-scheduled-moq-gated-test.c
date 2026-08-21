#define main scheduled_v1_main
#include "3dgs-scheduled-moq-test.c"
#undef main

static const char *ready_path_v2, *go_path_v2;
static uint64_t queue_budget_bytes_v2 = MAX_QUEUED_BYTES;

static int write_ready_file_v2(void) {
	FILE *ready = fopen(ready_path_v2, "w");
	if(ready == NULL)
		return -1;
	fputs("ready\n", ready);
	if(fclose(ready) != 0)
		return -1;
	return 0;
}

static int wait_for_go_epoch_v2(gint64 *requested_epoch_us) {
	gint64 wait_deadline = g_get_monotonic_time() + 30 * G_USEC_PER_SEC;
	while(!g_atomic_int_get(&stop_requested) && g_get_monotonic_time() < wait_deadline) {
		FILE *go = fopen(go_path_v2, "r");
		if(go == NULL) {
			g_usleep(1000);
			continue;
		}
		char line[128];
		if(fgets(line, sizeof(line), go) == NULL) {
			fclose(go);
			return -1;
		}
		fclose(go);
		char *end = NULL;
		long long value = strtoll(line, &end, 10);
		if(end == line || value <= 0)
			return -1;
		while(*end == ' ' || *end == '\t' || *end == '\r' || *end == '\n')
			end++;
		if(*end != '\0')
			return -1;
		*requested_epoch_us = (gint64)value;
		while(!g_atomic_int_get(&stop_requested)) {
			gint64 now_real = g_get_real_time();
			if(now_real >= *requested_epoch_us)
				return 0;
			gint64 sleep_us = *requested_epoch_us - now_real;
			g_usleep((gulong)(sleep_us > 1000 ? 1000 : sleep_us));
		}
		return -1;
	}
	return -1;
}

static int run_gated_publisher_v2(
		const char *bind_address, uint16_t port, const char *mode) {
	imquic_congestion_controller cc;
	imquic_ecn_mode ecn;
	if(parse_mode(mode, &cc, &ecn) < 0 || imquic_init(NULL) < 0)
		return 1;
	imquic_server *server = imquic_create_moq_server("3dgs-moq-gated-publisher",
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

	FILE *source = NULL, *metrics = NULL, *schedule = NULL, *admission = NULL;
	scheduled_record *records = NULL;
	if(!g_atomic_int_get(&failed) && open_source_bundle(&source) < 0)
		fail_case("could not open source scene bundle");
	if(!g_atomic_int_get(&failed)) {
		schedule = fopen(release_schedule_path, "r");
		if(schedule == NULL)
			fail_case("could not open release schedule");
	}
	if(!g_atomic_int_get(&failed) &&
			index_scheduled_records(source, schedule, &records) < 0)
		fail_case("invalid source bundle or eligibility/rank schedule");
	if(!g_atomic_int_get(&failed) && admission_path != NULL) {
		admission = fopen(admission_path, "w");
		if(admission == NULL)
			fail_case("could not open admission-order log");
		else
			fputs("admission_index,time_us,bundle_record_index,release_ms,"
				"importance_rank,subgroup_id,payload_bytes\n", admission);
	}
	if(metrics_path != NULL) {
		metrics = fopen(metrics_path, "w");
		if(metrics != NULL)
			fputs("time_us,rtt_us,cwnd_bytes,bytes_in_flight,queued_stream_bytes,"
				"pacing_Bps,ect0_packets,ect1_packets,ce_packets,alpha_numerator,"
				"alpha_denominator\n", metrics);
	}

	gint64 requested_epoch_us = 0;
	if(!g_atomic_int_get(&failed) && write_ready_file_v2() < 0)
		fail_case("could not signal publisher readiness");
	if(!g_atomic_int_get(&failed) && wait_for_go_epoch_v2(&requested_epoch_us) < 0)
		fail_case("could not synchronize publisher workload start");

	gint64 started = g_get_monotonic_time();
	gint64 started_real_us = g_get_real_time();
	gint64 deadline = started + (gint64)deadline_ms * 1000;
	gint64 next_metric = started;
	gboolean source_done = source_objects == 0;
	uint64_t scheduled_records = 0;

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

		uint64_t elapsed_ms = (uint64_t)((now - started) / 1000);
		uint64_t next_release_ms = UINT64_MAX;
		int selected = select_scheduled_record(
			records, source_objects, elapsed_ms, &next_release_ms);
		if(selected == -2) {
			source_done = TRUE;
			continue;
		}
		if(selected < 0) {
			gint64 release_at = started + (gint64)next_release_ms * 1000;
			gint64 sleep_us = release_at - now;
			g_usleep((gulong)(sleep_us > 1000 ? 1000 : sleep_us));
			continue;
		}

		imquic_transport_metrics transport = {0};
		if(connection == NULL ||
				imquic_get_transport_metrics(connection, &transport) < 0 ||
				transport.bytes_in_flight + transport.queued_stream_bytes >= queue_budget_bytes_v2) {
			g_usleep(1000);
			continue;
		}

		uint8_t *payload = NULL;
		scheduled_record *record = &records[selected];
		uint32_t length = record->payload_length;
		if(read_scheduled_payload(source, record, &payload) < 0) {
			fail_case("could not read indexed source bundle record");
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
			record->admitted = TRUE;
			if(admission != NULL) {
				fprintf(admission, "%" PRIu64 ",%" G_GINT64_FORMAT ",%d,%" PRIu64
					",%" PRIu64 ",%" PRIu64 ",%u\n",
					scheduled_records, g_get_monotonic_time() - started, selected,
					record->release_ms, record->importance_rank, subgroup_id, length);
			}
			queued_objects++;
			queued_bytes += length;
		}
		free(payload);
		scheduled_records++;
		if(scheduled_records >= source_objects)
			source_done = TRUE;
	}

	write_metric(metrics, started);
	if(g_atomic_int_get(&publishing) && connection != NULL)
		imquic_moq_publish_done(connection, publish_request_id,
			IMQUIC_MOQ_PUBDONE_SUBSCRIPTION_ENDED,
			source_done ? "scheduled scene complete" : "scheduled scene deadline elapsed");
	if(source != NULL) fclose(source);
	if(metrics != NULL) fclose(metrics);
	if(schedule != NULL) fclose(schedule);
	if(admission != NULL) fclose(admission);
	free(records);

	if(result_path != NULL) {
		FILE *result = fopen(result_path, "w");
		if(result != NULL) {
			fprintf(result,
				"{\"source_objects\":%" PRIu64 ",\"source_bytes\":%" PRIu64
				",\"queued_objects\":%" PRIu64 ",\"queued_bytes\":%" PRIu64
				",\"deadline_ms\":%u,\"source_fully_queued\":%s"
				",\"workload_start_epoch_us\":%" G_GINT64_FORMAT
				",\"publisher_started_epoch_us\":%" G_GINT64_FORMAT
				",\"queue_budget_bytes\":%" PRIu64
				",\"scheduled\":true,\"scheduling_policy\":"
				"\"lowest-importance-rank-among-currently-eligible\","
				"\"validated\":%s}\n",
				source_objects, source_bytes, queued_objects, queued_bytes, deadline_ms,
				source_done ? "true" : "false", requested_epoch_us, started_real_us,
				queue_budget_bytes_v2,
				g_atomic_int_get(&failed) ? "false" : "true");
			fclose(result);
		}
	}

	close_active_connection("3dgs gated publisher complete");
	imquic_shutdown_endpoint(server);
	imquic_deinit();
	return g_atomic_int_get(&failed) ? 1 : 0;
}

int main(int argc, char **argv) {
	if(argc == 14 && !strcmp(argv[1], "publisher-scheduled-gated")) {
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
		admission_path = argv[10];
		ready_path_v2 = argv[11];
		go_path_v2 = argv[12];
		queue_budget_bytes_v2 = strtoull(argv[13], NULL, 10);
		if(queue_budget_bytes_v2 == 0)
			return 2;
		return run_gated_publisher_v2(
			argv[2], (uint16_t)strtoul(argv[3], NULL, 10), argv[4]);
	}
	return scheduled_v1_main(argc, argv);
}
