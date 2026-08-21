#define main legacy_fixture_main
#include "3dgs-moq-test.c"
#undef main

static const char *release_schedule_path_v2, *admission_path_v2;
static const char *ready_path_v2, *go_path_v2;
static uint64_t transport_queue_slack_bytes_v2 = 4096;

typedef struct scheduled_record_v2 {
	long payload_offset;
	uint32_t payload_length;
	uint64_t release_ms;
	uint64_t importance_rank;
	gboolean admitted;
} scheduled_record_v2;

static int read_schedule_entry_v2(FILE *schedule, uint64_t *release_ms,
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
	return *end == '\0' ? 0 : -1;
}

static int schedule_has_extra_values_v2(FILE *schedule) {
	char line[128];
	while(fgets(line, sizeof(line), schedule) != NULL) {
		char *cursor = line;
		while(*cursor == ' ' || *cursor == '\t' ||
				*cursor == '\r' || *cursor == '\n')
			cursor++;
		if(*cursor != '\0')
			return 1;
	}
	return 0;
}

static int index_scheduled_records_v2(FILE *source, FILE *schedule,
		scheduled_record_v2 **records_out) {
	if(source_objects == 0) {
		if(schedule_has_extra_values_v2(schedule))
			return -1;
		*records_out = NULL;
		return 0;
	}
	scheduled_record_v2 *records =
		calloc((size_t)source_objects, sizeof(*records));
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
		if(read_schedule_entry_v2(
				schedule,
				&records[i].release_ms,
				&records[i].importance_rank) < 0)
			goto fail;
		for(uint64_t prior = 0; prior < i; prior++) {
			if(records[prior].importance_rank == records[i].importance_rank)
				goto fail;
		}
		indexed_bytes += length;
	}
	if(indexed_bytes != source_bytes || fgetc(source) != EOF ||
			schedule_has_extra_values_v2(schedule))
		goto fail;
	*records_out = records;
	return 0;

fail:
	free(records);
	return -1;
}

static int select_scheduled_record_v2(scheduled_record_v2 *records,
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
		if(best < 0 ||
				records[i].importance_rank < records[best].importance_rank ||
				(records[i].importance_rank == records[best].importance_rank &&
				 i < (uint64_t)best))
			best = (int)i;
	}
	*next_release_ms = next_release;
	if(best >= 0)
		return best;
	return next_release == UINT64_MAX ? -2 : -1;
}

static uint64_t admission_threshold_v2(const scheduled_record_v2 *record) {
	return record->payload_length < transport_queue_slack_bytes_v2
		? record->payload_length
		: transport_queue_slack_bytes_v2;
}

static int read_scheduled_payload_v2(FILE *source,
		const scheduled_record_v2 *record, uint8_t **payload) {
	if(fseek(source, record->payload_offset, SEEK_SET) != 0)
		return -1;
	*payload = malloc(record->payload_length);
	if(*payload == NULL ||
			read_exact(source, *payload, record->payload_length) < 0) {
		free(*payload);
		*payload = NULL;
		return -1;
	}
	return 0;
}

static int schedule_self_test_v2(void) {
	scheduled_record_v2 records[] = {
		{.payload_length = 1000, .release_ms = 0, .importance_rank = 5},
		{.payload_length = 8000, .release_ms = 10, .importance_rank = 0},
		{.payload_length = 2000, .release_ms = 0, .importance_rank = 2},
	};
	uint64_t next_release = UINT64_MAX;
	int selected = select_scheduled_record_v2(records, 3, 0, &next_release);
	if(selected != 2)
		return 1;
	records[selected].admitted = TRUE;
	selected = select_scheduled_record_v2(records, 3, 0, &next_release);
	if(selected != 0)
		return 1;
	records[selected].admitted = TRUE;
	selected = select_scheduled_record_v2(records, 3, 0, &next_release);
	if(selected != -1 || next_release != 10)
		return 1;
	selected = select_scheduled_record_v2(records, 3, 10, &next_release);
	if(selected != 1)
		return 1;
	transport_queue_slack_bytes_v2 = 4096;
	if(admission_threshold_v2(&records[0]) != 1000)
		return 1;
	if(admission_threshold_v2(&records[1]) != 4096)
		return 1;
	return 0;
}

static int write_ready_file_v2(void) {
	FILE *ready = fopen(ready_path_v2, "w");
	if(ready == NULL)
		return -1;
	fputs("ready\n", ready);
	return fclose(ready) == 0 ? 0 : -1;
}

static int wait_for_go_epoch_v2(gint64 *requested_epoch_us) {
	gint64 wait_deadline = g_get_monotonic_time() + 30 * G_USEC_PER_SEC;
	while(!g_atomic_int_get(&stop_requested) &&
			g_get_monotonic_time() < wait_deadline) {
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
		while(*end == ' ' || *end == '\t' ||
				*end == '\r' || *end == '\n')
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
	imquic_server *server = imquic_create_moq_server(
		"3dgs-moq-gated-publisher",
		IMQUIC_CONFIG_INIT,
		IMQUIC_CONFIG_TLS_CERT, CERT_PATH,
		IMQUIC_CONFIG_TLS_KEY, KEY_PATH,
		IMQUIC_CONFIG_LOCAL_BIND, bind_address,
		IMQUIC_CONFIG_LOCAL_PORT, port,
		IMQUIC_CONFIG_CONGESTION_CONTROL, cc,
		IMQUIC_CONFIG_ECN, ecn,
		IMQUIC_CONFIG_RAW_QUIC, TRUE,
		IMQUIC_CONFIG_MOQ_VERSION, IMQUIC_MOQ_VERSION_19,
		IMQUIC_CONFIG_DONE, NULL);
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

	while(!g_atomic_int_get(&stop_requested) &&
			!g_atomic_int_get(&publishing))
		g_usleep(1000);

	FILE *source = NULL;
	FILE *metrics = NULL;
	FILE *schedule = NULL;
	FILE *admission = NULL;
	FILE *admission_gate = NULL;
	scheduled_record_v2 *records = NULL;

	if(!g_atomic_int_get(&failed) && open_source_bundle(&source) < 0)
		fail_case("could not open source scene bundle");
	if(!g_atomic_int_get(&failed)) {
		schedule = fopen(release_schedule_path_v2, "r");
		if(schedule == NULL)
			fail_case("could not open release schedule");
	}
	if(!g_atomic_int_get(&failed) &&
			index_scheduled_records_v2(source, schedule, &records) < 0)
		fail_case("invalid source bundle or eligibility/rank schedule");

	if(!g_atomic_int_get(&failed) && admission_path_v2 != NULL) {
		admission = fopen(admission_path_v2, "w");
		if(admission == NULL) {
			fail_case("could not open admission-order log");
		} else {
			fputs(
				"admission_index,time_us,bundle_record_index,release_ms,"
				"importance_rank,subgroup_id,payload_bytes\n",
				admission);
			char *gate_path = g_strdup_printf("%s.gate.csv", admission_path_v2);
			if(gate_path == NULL) {
				fail_case("could not allocate admission-gate path");
			} else {
				admission_gate = fopen(gate_path, "w");
				g_free(gate_path);
				if(admission_gate == NULL) {
					fail_case("could not open admission-gate log");
				} else {
					fputs(
						"admission_index,time_us,bundle_record_index,payload_bytes,"
						"queued_stream_bytes_before,bytes_in_flight_before,"
						"cwnd_bytes_before,queue_threshold_bytes\n",
						admission_gate);
				}
			}
		}
	}

	if(metrics_path != NULL) {
		metrics = fopen(metrics_path, "w");
		if(metrics != NULL)
			fputs(
				"time_us,rtt_us,cwnd_bytes,bytes_in_flight,"
				"queued_stream_bytes,pacing_Bps,ect0_packets,"
				"ect1_packets,ce_packets,alpha_numerator,"
				"alpha_denominator\n",
				metrics);
	}

	gint64 requested_epoch_us = 0;
	if(!g_atomic_int_get(&failed) && write_ready_file_v2() < 0)
		fail_case("could not signal publisher readiness");
	if(!g_atomic_int_get(&failed) &&
			wait_for_go_epoch_v2(&requested_epoch_us) < 0)
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
		int selected = select_scheduled_record_v2(
			records, source_objects, elapsed_ms, &next_release_ms);
		if(selected == -2) {
			source_done = TRUE;
			continue;
		}
		if(selected < 0) {
			gint64 release_at =
				started + (gint64)next_release_ms * 1000;
			gint64 sleep_us = release_at - now;
			g_usleep((gulong)(sleep_us > 1000 ? 1000 : sleep_us));
			continue;
		}

		scheduled_record_v2 *record = &records[selected];
		uint64_t queue_threshold = admission_threshold_v2(record);
		imquic_transport_metrics transport = {0};
		if(connection == NULL ||
				imquic_get_transport_metrics(connection, &transport) < 0 ||
				transport.congestion_window_bytes == 0 ||
				transport.bytes_in_flight >= transport.congestion_window_bytes ||
				transport.queued_stream_bytes >= queue_threshold) {
			g_usleep(1000);
			continue;
		}

		uint8_t *payload = NULL;
		uint32_t length = record->payload_length;
		if(read_scheduled_payload_v2(source, record, &payload) < 0) {
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
		object.priority =
			subgroup_id == 0 ? 0 : (subgroup_id == 1 ? 64 : 128);
		object.payload = payload;
		object.payload_len = length;
		object.delivery = IMQUIC_MOQ_USE_SUBGROUP;
		object.first_of_subgroup = object_id == 0;

		if(imquic_moq_send_object(connection, &object) < 0) {
			fail_case("could not queue scheduled 3dgs object");
		} else {
			record->admitted = TRUE;
			if(admission != NULL) {
				/* Record when eligibility was evaluated, before bundle I/O and
				 * queueing can cross a later record's release boundary. */
				gint64 admission_time_us = now - started;
				fprintf(
					admission,
					"%" PRIu64 ",%" G_GINT64_FORMAT ",%d,%" PRIu64
					",%" PRIu64 ",%" PRIu64 ",%u\n",
					scheduled_records,
					admission_time_us,
					selected,
					record->release_ms,
					record->importance_rank,
					subgroup_id,
					length);
				if(admission_gate != NULL) {
					fprintf(
						admission_gate,
						"%" PRIu64 ",%" G_GINT64_FORMAT ",%d,%u,%" PRIu64
						",%" PRIu64 ",%" PRIu64 ",%" PRIu64 "\n",
						scheduled_records,
						admission_time_us,
						selected,
						length,
						transport.queued_stream_bytes,
						transport.bytes_in_flight,
						transport.congestion_window_bytes,
						queue_threshold);
				}
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
		imquic_moq_publish_done(
			connection,
			publish_request_id,
			IMQUIC_MOQ_PUBDONE_SUBSCRIPTION_ENDED,
			source_done
				? "scheduled scene complete"
				: "scheduled scene deadline elapsed");

	if(source != NULL)
		fclose(source);
	if(metrics != NULL)
		fclose(metrics);
	if(schedule != NULL)
		fclose(schedule);
	if(admission != NULL)
		fclose(admission);
	if(admission_gate != NULL)
		fclose(admission_gate);
	free(records);

	if(result_path != NULL) {
		FILE *result = fopen(result_path, "w");
		if(result != NULL) {
			fprintf(
				result,
				"{\"source_objects\":%" PRIu64
				",\"source_bytes\":%" PRIu64
				",\"queued_objects\":%" PRIu64
				",\"queued_bytes\":%" PRIu64
				",\"deadline_ms\":%u"
				",\"source_fully_queued\":%s"
				",\"workload_start_epoch_us\":%" G_GINT64_FORMAT
				",\"publisher_started_epoch_us\":%" G_GINT64_FORMAT
				",\"transport_queue_slack_bytes\":%" PRIu64
				",\"scheduled\":true"
				",\"scheduling_policy\":"
				"\"lowest-rank-eligible-when-cwnd-open-and-quic-queue-shallow\""
				",\"validated\":%s}\n",
				source_objects,
				source_bytes,
				queued_objects,
				queued_bytes,
				deadline_ms,
				source_done ? "true" : "false",
				requested_epoch_us,
				started_real_us,
				transport_queue_slack_bytes_v2,
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
	if(argc == 2 && !strcmp(argv[1], "schedule-self-test"))
		return schedule_self_test_v2();

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
		release_schedule_path_v2 = argv[9];
		admission_path_v2 = argv[10];
		ready_path_v2 = argv[11];
		go_path_v2 = argv[12];
		transport_queue_slack_bytes_v2 = strtoull(argv[13], NULL, 10);
		if(transport_queue_slack_bytes_v2 == 0)
			return 2;
		return run_gated_publisher_v2(
			argv[2],
			(uint16_t)strtoul(argv[3], NULL, 10),
			argv[4]);
	}
	return legacy_fixture_main(argc, argv);
}
