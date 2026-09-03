#define main legacy_fixture_main
#include "3dgs-moq-test.c"
#undef main

static const char *release_schedule_path, *admission_path;

typedef struct scheduled_record {
	long payload_offset;
	uint32_t payload_length;
	uint64_t release_ms;
	uint64_t importance_rank;
	gboolean admitted;
} scheduled_record;

static int read_schedule_entry(FILE *schedule, uint64_t *release_ms,
		uint64_t *importance_rank) {
	char line[128];
	if(fgets(line, sizeof(line), schedule) == NULL)
		return -1;
	char *end = NULL;
	unsigned long long value = strtoull(line, &end, 10);
	if(end == line)
		return -1;
	*release_ms = (uint64_t)value;
	while(*end == ' ' || *end == '\t')
		end++;
	char *rank_end = NULL;
	value = strtoull(end, &rank_end, 10);
	if(rank_end == end)
		return -1;
	*importance_rank = (uint64_t)value;
	end = rank_end;
	while(*end == ' ' || *end == '\t' || *end == '\r' || *end == '\n')
		end++;
	if(*end != '\0')
		return -1;
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

static int index_scheduled_records(FILE *source, FILE *schedule,
		scheduled_record **records_out) {
	if(source_objects == 0) {
		if(schedule_has_extra_values(schedule))
			return -1;
		*records_out = NULL;
		return 0;
	}
	scheduled_record *records = calloc((size_t)source_objects, sizeof(*records));
	if(records == NULL)
		return -1;
	uint64_t indexed_bytes = 0;
	for(uint64_t i = 0; i < source_objects; i++) {
		uint8_t encoded_len[4];
		if(read_exact(source, encoded_len, sizeof(encoded_len)) < 0)
			goto fail;
		uint32_t length = load_u32_le(encoded_len);
		if(length == 0 || length > MAX_RECORD_BYTES)
			goto fail;
		long offset = ftell(source);
		if(offset < 0 || fseek(source, (long)length, SEEK_CUR) != 0)
			goto fail;
		records[i].payload_offset = offset;
		records[i].payload_length = length;
		if(read_schedule_entry(schedule, &records[i].release_ms,
				&records[i].importance_rank) < 0)
			goto fail;
		for(uint64_t prior = 0; prior < i; prior++) {
			if(records[prior].importance_rank == records[i].importance_rank)
				goto fail;
		}
		indexed_bytes += length;
	}
	if(indexed_bytes != source_bytes || fgetc(source) != EOF ||
			schedule_has_extra_values(schedule))
		goto fail;
	*records_out = records;
	return 0;

fail:
	free(records);
	return -1;
}

static int select_scheduled_record(scheduled_record *records,
		uint64_t count, uint64_t elapsed_ms, uint64_t *next_release_ms) {
	int best = -1;
	uint64_t next_release = UINT64_MAX;
	for(uint64_t i = 0; i < count; i++) {
		if(records[i].admitted)
			continue;
		if(records[i].release_ms > elapsed_ms) {
			if(records[i].release_ms < next_release)
				next_release = records[i].release_ms;
			continue;
		}
		if(best < 0 || records[i].importance_rank < records[best].importance_rank ||
				(records[i].importance_rank == records[best].importance_rank &&
				 i < (uint64_t)best))
			best = (int)i;
	}
	*next_release_ms = next_release;
	if(best >= 0)
		return best;
	return next_release == UINT64_MAX ? -2 : -1;
}

static int read_scheduled_payload(FILE *source, const scheduled_record *record,
		uint8_t **payload) {
	if(fseek(source, record->payload_offset, SEEK_SET) != 0)
		return -1;
	*payload = malloc(record->payload_length);
	if(*payload == NULL || read_exact(source, *payload, record->payload_length) < 0) {
		free(*payload);
		*payload = NULL;
		return -1;
	}
	return 0;
}

static int schedule_self_test(void) {
	scheduled_record records[] = {
		{.release_ms = 0, .importance_rank = 5},
		{.release_ms = 10, .importance_rank = 0},
		{.release_ms = 0, .importance_rank = 2},
	};
	uint64_t next_release = UINT64_MAX;
	int selected = select_scheduled_record(records, 3, 0, &next_release);
	if(selected != 2)
		return 1;
	records[selected].admitted = TRUE;
	selected = select_scheduled_record(records, 3, 0, &next_release);
	if(selected != 0)
		return 1;
	records[selected].admitted = TRUE;
	selected = select_scheduled_record(records, 3, 0, &next_release);
	if(selected != -1 || next_release != 10)
		return 1;
	selected = select_scheduled_record(records, 3, 10, &next_release);
	if(selected != 1)
		return 1;
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
				transport.bytes_in_flight + transport.queued_stream_bytes >= MAX_QUEUED_BYTES) {
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

		if(scheduled_records >= source_objects) {
			source_done = TRUE;
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
	if(admission != NULL)
		fclose(admission);
	free(records);

	if(result_path != NULL) {
		FILE *result = fopen(result_path, "w");
		if(result != NULL) {
			fprintf(result,
				"{\"source_objects\":%" PRIu64 ",\"source_bytes\":%" PRIu64
				",\"queued_objects\":%" PRIu64 ",\"queued_bytes\":%" PRIu64
				",\"deadline_ms\":%u,\"source_fully_queued\":%s"
				",\"publisher_started_epoch_us\":%" G_GINT64_FORMAT
				",\"scheduled\":true,\"scheduling_policy\":"
				"\"lowest-importance-rank-among-currently-eligible\","
				"\"validated\":%s}\n",
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
	if(argc == 2 && !strcmp(argv[1], "schedule-self-test"))
		return schedule_self_test();
	if(argc == 11 && !strcmp(argv[1], "publisher-scheduled")) {
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
		return run_scheduled_publisher(
			argv[2], (uint16_t)strtoul(argv[3], NULL, 10), argv[4]);
	}
	return legacy_fixture_main(argc, argv);
}
