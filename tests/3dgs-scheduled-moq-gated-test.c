#define main legacy_fixture_main
#include "3dgs-moq-test.c"
#undef main

#define CLASSIC_PATH 0
#define PRAGUE_PATH 1
#define PATH_COUNT 2
#define SELECT_NONE -1
#define SELECT_DONE -2
#define SELECT_BLOCKED -3
#define WARMUP_OBJECT_BYTES_V2 (16U * 1024U)
#define WARMUP_OUTSTANDING_OBJECTS_V2 10U

typedef struct scheduled_record_v2 {
	long payload_offset;
	uint32_t payload_length;
	uint64_t release_ms;
	uint64_t importance_rank;
	gboolean admitted;
} scheduled_record_v2;

typedef struct publisher_path_v2 {
	const char *name, *mode, *bundle_path, *metrics_path, *result_path;
	const char *schedule_path, *admission_path;
	uint16_t port;
	imquic_server *server;
	imquic_connection *connection;
	volatile gint publishing;
	uint64_t publish_request_id, outer_object_ids[3];
	FILE *source, *metrics, *admission, *gate;
	scheduled_record_v2 *records;
	uint64_t source_objects, source_bytes, admitted_objects, admitted_bytes;
	uint64_t warmup_objects, warmup_bytes;
} publisher_path_v2;

typedef struct scheduler_stats_v2 {
	uint64_t prague_blocked_events, prague_blocked_time_us;
	uint64_t prague_preempted_classic;
	gint64 warmup_started_epoch_us, warmup_finished_epoch_us;
	gint64 workload_start_epoch_us, publisher_started_epoch_us;
} scheduler_stats_v2;

static publisher_path_v2 paths_v2[PATH_COUNT];
static const char *ready_path_v2, *go_path_v2;
static const char *combined_path_v2, *scheduler_result_path_v2;
static uint64_t queue_slack_v2 = 4096;
static uint32_t prague_warmup_ms_v2;
static gboolean closing_v2;

static publisher_path_v2 *find_path_v2(imquic_connection *conn) {
	for(unsigned int i = 0; i < PATH_COUNT; i++)
		if(paths_v2[i].connection == conn)
			return &paths_v2[i];
	return NULL;
}

static int read_schedule_v2(FILE *stream, uint64_t *release, uint64_t *rank) {
	char line[128], *end = NULL, *rank_end = NULL;
	if(fgets(line, sizeof(line), stream) == NULL)
		return -1;
	unsigned long long value = strtoull(line, &end, 10);
	if(end == line)
		return -1;
	*release = value;
	while(*end == ' ' || *end == '\t') end++;
	value = strtoull(end, &rank_end, 10);
	if(rank_end == end)
		return -1;
	*rank = value;
	while(*rank_end == ' ' || *rank_end == '\t' ||
			*rank_end == '\r' || *rank_end == '\n') rank_end++;
	return *rank_end == '\0' ? 0 : -1;
}

static int extra_schedule_v2(FILE *stream) {
	char line[128];
	while(fgets(line, sizeof(line), stream) != NULL) {
		char *cursor = line;
		while(*cursor == ' ' || *cursor == '\t' ||
				*cursor == '\r' || *cursor == '\n') cursor++;
		if(*cursor != '\0') return 1;
	}
	return 0;
}

static int index_path_v2(publisher_path_v2 *path) {
	uint8_t header[BUNDLE_HEADER_BYTES];
	path->source = fopen(path->bundle_path, "rb");
	FILE *schedule = fopen(path->schedule_path, "r");
	if(path->source == NULL || schedule == NULL ||
			read_exact(path->source, header, sizeof(header)) < 0 ||
			memcmp(header, BUNDLE_MAGIC, 8) ||
			load_u32_le(&header[8]) != BUNDLE_VERSION)
		goto fail;
	path->source_objects = load_u32_le(&header[12]);
	path->source_bytes = load_u64_le(&header[16]);
	if(path->source_objects == 0) {
		int result = extra_schedule_v2(schedule) ? -1 : 0;
		fclose(schedule);
		return result;
	}
	path->records = calloc(path->source_objects, sizeof(*path->records));
	if(path->records == NULL) goto fail;
	uint64_t indexed_bytes = 0;
	for(uint64_t i = 0; i < path->source_objects; i++) {
		uint8_t encoded[4];
		if(read_exact(path->source, encoded, sizeof(encoded)) < 0) goto fail;
		uint32_t length = load_u32_le(encoded);
		if(length == 0 || length > MAX_RECORD_BYTES) goto fail;
		long offset = ftell(path->source);
		if(offset < 0 || fseek(path->source, length, SEEK_CUR) != 0) goto fail;
		path->records[i].payload_offset = offset;
		path->records[i].payload_length = length;
		if(read_schedule_v2(schedule, &path->records[i].release_ms,
				&path->records[i].importance_rank) < 0) goto fail;
		indexed_bytes += length;
	}
	if(indexed_bytes != path->source_bytes || fgetc(path->source) != EOF ||
			extra_schedule_v2(schedule)) goto fail;
	fclose(schedule);
	return 0;
fail:
	if(schedule != NULL) fclose(schedule);
	return -1;
}

static int select_record_v2(publisher_path_v2 *path, uint64_t elapsed_ms,
		uint64_t *next_release) {
	int best = SELECT_NONE;
	*next_release = UINT64_MAX;
	for(uint64_t i = 0; i < path->source_objects; i++) {
		if(path->records[i].admitted) continue;
		if(path->records[i].release_ms > elapsed_ms) {
			if(path->records[i].release_ms < *next_release)
				*next_release = path->records[i].release_ms;
			continue;
		}
		if(best < 0 || path->records[i].importance_rank <
				path->records[best].importance_rank)
			best = (int)i;
	}
	if(best >= 0) return best;
	return *next_release == UINT64_MAX ? SELECT_DONE : SELECT_NONE;
}

static uint64_t threshold_v2(const scheduled_record_v2 *record) {
	return record->payload_length < queue_slack_v2
		? record->payload_length : queue_slack_v2;
}

static int choose_path_v2(int high, int low, gboolean high_room,
		gboolean low_room) {
	if(high >= 0) return high_room ? PRAGUE_PATH : SELECT_BLOCKED;
	if(low >= 0) return low_room ? CLASSIC_PATH : SELECT_BLOCKED;
	return SELECT_NONE;
}

static int schedule_self_test_v2(void) {
	scheduled_record_v2 high_records[] = {
		{.payload_length = 8000, .release_ms = 10, .importance_rank = 0},
		{.payload_length = 2000, .release_ms = 20, .importance_rank = 1},
	};
	scheduled_record_v2 low_records[] = {
		{.payload_length = 1000, .release_ms = 0, .importance_rank = 5},
		{.payload_length = 1000, .release_ms = 20, .importance_rank = 6},
	};
	publisher_path_v2 high = {.records = high_records, .source_objects = 2};
	publisher_path_v2 low = {.records = low_records, .source_objects = 2};
	uint64_t hn, ln;
	int hi = select_record_v2(&high, 0, &hn);
	int lo = select_record_v2(&low, 0, &ln);
	if(hi != SELECT_NONE || lo != 0 ||
			choose_path_v2(hi, lo, TRUE, TRUE) != CLASSIC_PATH) return 1;
	hi = select_record_v2(&high, 10, &hn);
	if(hi != 0 || choose_path_v2(hi, lo, TRUE, TRUE) != PRAGUE_PATH ||
			choose_path_v2(hi, lo, FALSE, TRUE) != SELECT_BLOCKED) return 1;
	high_records[0].admitted = TRUE;
	low_records[0].admitted = TRUE;
	hi = select_record_v2(&high, 20, &hn);
	lo = select_record_v2(&low, 20, &ln);
	if(hi != 1 || lo != 1 ||
			choose_path_v2(hi, lo, TRUE, TRUE) != PRAGUE_PATH ||
			choose_path_v2(SELECT_NONE, lo, TRUE, FALSE) != SELECT_BLOCKED)
		return 1;
	queue_slack_v2 = 4096;
	if(threshold_v2(&high_records[0]) != 4096 ||
			threshold_v2(&low_records[0]) != 1000) return 1;
	return 0;
}

static void new_connection_v2(imquic_connection *conn, void *user_data) {
	publisher_path_v2 *path = user_data;
	if(path == NULL || path->connection != NULL) {
		fail_case("unexpected publisher connection");
		return;
	}
	path->connection = conn;
	imquic_connection_ref(conn);
}

static void connection_failed_v2(void *user_data) {
	(void)user_data;
	fail_case("publisher QUIC connection failed");
}

static void connection_gone_v2(imquic_connection *conn, uint64_t code,
		const char *reason) {
	(void)code; (void)reason;
	publisher_path_v2 *path = find_path_v2(conn);
	if(path != NULL) {
		imquic_connection_unref(conn);
		path->connection = NULL;
	}
	if(!closing_v2) fail_case("publisher QUIC connection closed early");
}

static void moq_ready_v2(imquic_connection *conn) {
	if(find_path_v2(conn) == NULL) fail_case("unknown ready connection");
}

static void accepted_v2(imquic_connection *conn, uint64_t id,
		imquic_moq_request_parameters *parameters) {
	(void)parameters;
	publisher_path_v2 *path = find_path_v2(conn);
	if(path == NULL || path->publish_request_id != id) {
		fail_case("unexpected publish acceptance");
		return;
	}
	g_atomic_int_set(&path->publishing, 1);
}

static void publish_error_v2(imquic_connection *conn, uint64_t id,
		imquic_moq_request_error_code code, const char *reason,
		uint64_t retry, imquic_moq_redirect *redirect) {
	(void)conn; (void)id; (void)code; (void)reason; (void)retry; (void)redirect;
	fail_case("peer rejected publish request");
}

static void subscribe_v2(imquic_connection *conn, uint64_t id,
		imquic_moq_namespace *tns, imquic_moq_track *tn,
		imquic_moq_request_parameters *parameters) {
	(void)parameters;
	publisher_path_v2 *path = find_path_v2(conn);
	if(path == NULL || !imquic_moq_namespace_equals(tns, &moq_namespace) ||
			!imquic_moq_track_equals(tn, &moq_track)) {
		imquic_moq_reject_subscribe(conn, id, IMQUIC_MOQ_REQERR_DOES_NOT_EXIST,
			"unknown 3dgs track", 0, NULL);
		return;
	}
	if(imquic_moq_accept_subscribe(conn, id, 1, NULL, NULL) < 0) {
		fail_case("could not accept publisher subscription");
		return;
	}
	imquic_moq_request_parameters output;
	imquic_moq_request_parameters_init_defaults(&output);
	output.group_order_set = TRUE;
	output.group_order = IMQUIC_MOQ_ORDERING_ASCENDING;
	output.forward_set = TRUE;
	output.forward = TRUE;
	path->publish_request_id = imquic_moq_get_next_request_id(conn);
	if(imquic_moq_publish(conn, path->publish_request_id, tns, tn, 2,
			&output, NULL) < 0) fail_case("could not send publish request");
}

static void metric_v2(publisher_path_v2 *path, FILE *stream, gint64 started) {
	imquic_transport_metrics value = {0};
	if(stream == NULL || path->connection == NULL ||
			imquic_get_transport_metrics(path->connection, &value) < 0) return;
	fprintf(stream, "%" G_GINT64_FORMAT ",%" PRIu64 ",%" PRIu64
		",%" PRIu64 ",%" PRIu64 ",%" PRIu64 ",%" PRIu64 ",%" PRIu64
		",%" PRIu64 ",%" PRIu64 ",%" PRIu64 "\n",
		g_get_monotonic_time() - started, value.smoothed_rtt_us,
		value.congestion_window_bytes, value.bytes_in_flight,
		value.queued_stream_bytes, value.pacing_rate_bytes_per_second,
		value.ect0_packets, value.ect1_packets, value.ce_packets,
		(uint64_t)value.prague_alpha_numerator,
		(uint64_t)value.prague_alpha_denominator);
}

static int open_outputs_v2(publisher_path_v2 *path) {
	path->metrics = fopen(path->metrics_path, "w");
	path->admission = fopen(path->admission_path, "w");
	char *gate = g_strdup_printf("%s.gate.csv", path->admission_path);
	path->gate = gate == NULL ? NULL : fopen(gate, "w");
	g_free(gate);
	if(path->metrics == NULL || path->admission == NULL || path->gate == NULL)
		return -1;
	fputs("time_us,rtt_us,cwnd_bytes,bytes_in_flight,queued_stream_bytes,"
		"pacing_Bps,ect0_packets,ect1_packets,ce_packets,alpha_numerator,"
		"alpha_denominator\n", path->metrics);
	fputs("admission_index,time_us,bundle_record_index,release_ms,"
		"importance_rank,subgroup_id,payload_bytes\n", path->admission);
	fputs("admission_index,time_us,bundle_record_index,payload_bytes,"
		"queued_stream_bytes_before,bytes_in_flight_before,cwnd_bytes_before,"
		"queue_threshold_bytes\n", path->gate);
	return 0;
}

static int create_server_v2(publisher_path_v2 *path, const char *bind) {
	imquic_congestion_controller cc;
	imquic_ecn_mode ecn;
	if(parse_mode(path->mode, &cc, &ecn) < 0) return -1;
	path->server = imquic_create_moq_server(path->name,
		IMQUIC_CONFIG_INIT, IMQUIC_CONFIG_TLS_CERT, CERT_PATH,
		IMQUIC_CONFIG_TLS_KEY, KEY_PATH, IMQUIC_CONFIG_LOCAL_BIND, bind,
		IMQUIC_CONFIG_LOCAL_PORT, path->port,
		IMQUIC_CONFIG_CONGESTION_CONTROL, cc, IMQUIC_CONFIG_ECN, ecn,
		IMQUIC_CONFIG_USER_DATA, path, IMQUIC_CONFIG_RAW_QUIC, TRUE,
		IMQUIC_CONFIG_MOQ_VERSION, IMQUIC_MOQ_VERSION_19,
		IMQUIC_CONFIG_DONE, NULL);
	if(path->server == NULL) return -1;
	imquic_set_new_moq_connection_cb(path->server, new_connection_v2);
	imquic_set_moq_ready_cb(path->server, moq_ready_v2);
	imquic_set_incoming_subscribe_cb(path->server, subscribe_v2);
	imquic_set_publish_accepted_cb(path->server, accepted_v2);
	imquic_set_publish_error_cb(path->server, publish_error_v2);
	imquic_set_connection_failed_cb(path->server, connection_failed_v2);
	imquic_set_moq_connection_gone_cb(path->server, connection_gone_v2);
	imquic_start_endpoint(path->server);
	return 0;
}

static int wait_publishers_v2(void) {
	while(!g_atomic_int_get(&stop_requested)) {
		if(g_atomic_int_get(&paths_v2[CLASSIC_PATH].publishing) &&
				g_atomic_int_get(&paths_v2[PRAGUE_PATH].publishing)) return 0;
		g_usleep(1000);
	}
	return -1;
}

static int wait_go_v2(gint64 *epoch) {
	gint64 deadline = g_get_monotonic_time() + 30 * G_USEC_PER_SEC;
	while(!g_atomic_int_get(&stop_requested) && g_get_monotonic_time() < deadline) {
		FILE *stream = fopen(go_path_v2, "r");
		if(stream == NULL) { g_usleep(1000); continue; }
		long long value = 0;
		int parsed = fscanf(stream, "%lld", &value);
		fclose(stream);
		if(parsed != 1 || value <= 0) return -1;
		*epoch = value;
		while(!g_atomic_int_get(&stop_requested)) {
			gint64 remaining = *epoch - g_get_real_time();
			if(remaining <= 0) return 0;
			g_usleep((gulong)(remaining > 1000 ? 1000 : remaining));
		}
	}
	return -1;
}

static int warmup_v2(publisher_path_v2 *path, scheduler_stats_v2 *stats) {
	if(prague_warmup_ms_v2 == 0) return 0;
	char *name = g_strdup_printf("%s.warmup.csv", path->metrics_path);
	FILE *stream = name == NULL ? NULL : fopen(name, "w");
	g_free(name);
	uint8_t *payload = malloc(WARMUP_OBJECT_BYTES_V2);
	if(stream == NULL || payload == NULL) { free(payload); return -1; }
	fputs("time_us,rtt_us,cwnd_bytes,bytes_in_flight,queued_stream_bytes,"
		"pacing_Bps,ect0_packets,ect1_packets,ce_packets,alpha_numerator,"
		"alpha_denominator\n", stream);
	memset(payload, 0xa5, WARMUP_OBJECT_BYTES_V2);
	memcpy(payload, WARMUP_MAGIC, WARMUP_MAGIC_BYTES);
	gint64 started = g_get_monotonic_time();
	stats->warmup_started_epoch_us = g_get_real_time();
	gint64 deadline = started + (gint64)prague_warmup_ms_v2 * 1000;
	while(!g_atomic_int_get(&stop_requested) && g_get_monotonic_time() < deadline) {
		metric_v2(path, stream, started);
		imquic_transport_metrics transport = {0};
		if(path->connection == NULL ||
				imquic_get_transport_metrics(path->connection, &transport) < 0) {
			g_usleep(1000); continue;
		}
		uint64_t outstanding = transport.bytes_in_flight + transport.queued_stream_bytes;
		uint64_t target = WARMUP_OUTSTANDING_OBJECTS_V2 * WARMUP_OBJECT_BYTES_V2;
		unsigned int count = outstanding < target
			? (unsigned int)((target - outstanding + WARMUP_OBJECT_BYTES_V2 - 1) /
				WARMUP_OBJECT_BYTES_V2) : 0;
		for(unsigned int i = 0; i < count; i++) {
			imquic_moq_object object = {0};
			object.request_id = path->publish_request_id;
			object.track_alias = 2;
			object.subgroup_id = 0;
			object.object_id = path->outer_object_ids[0]++;
			object.priority = 0;
			object.payload = payload;
			object.payload_len = WARMUP_OBJECT_BYTES_V2;
			object.delivery = IMQUIC_MOQ_USE_SUBGROUP;
			object.first_of_subgroup = object.object_id == 0;
			if(imquic_moq_send_object(path->connection, &object) < 0) {
				free(payload); fclose(stream); return -1;
			}
			path->warmup_objects++;
			path->warmup_bytes += WARMUP_OBJECT_BYTES_V2;
		}
		g_usleep(1000);
	}
	free(payload);
	fclose(stream);
	deadline = g_get_monotonic_time() + 5 * G_USEC_PER_SEC;
	while(!g_atomic_int_get(&stop_requested) && g_get_monotonic_time() < deadline) {
		imquic_transport_metrics transport = {0};
		if(path->connection != NULL &&
				imquic_get_transport_metrics(path->connection, &transport) == 0 &&
				transport.bytes_in_flight == 0 && transport.queued_stream_bytes == 0) {
			stats->warmup_finished_epoch_us = g_get_real_time();
			return 0;
		}
		g_usleep(1000);
	}
	return -1;
}

static gboolean room_v2(publisher_path_v2 *path, scheduled_record_v2 *record,
		imquic_transport_metrics *transport) {
	memset(transport, 0, sizeof(*transport));
	return path->connection != NULL &&
		imquic_get_transport_metrics(path->connection, transport) == 0 &&
		transport->congestion_window_bytes > 0 &&
		transport->bytes_in_flight < transport->congestion_window_bytes &&
		transport->queued_stream_bytes < threshold_v2(record);
}

static int admit_v2(publisher_path_v2 *path, int selected, gint64 time_us,
		uint64_t global_index, imquic_transport_metrics *transport, FILE *combined) {
	scheduled_record_v2 *record = &path->records[selected];
	if(fseek(path->source, record->payload_offset, SEEK_SET) != 0) return -1;
	uint8_t *payload = malloc(record->payload_length);
	if(payload == NULL || read_exact(path->source, payload, record->payload_length) < 0) {
		free(payload); return -1;
	}
	uint64_t subgroup = embedded_subgroup(payload, record->payload_length);
	imquic_moq_object object = {0};
	object.request_id = path->publish_request_id;
	object.track_alias = 2;
	object.subgroup_id = subgroup;
	object.object_id = path->outer_object_ids[subgroup]++;
	object.priority = subgroup == 0 ? 0 : (subgroup == 1 ? 64 : 128);
	object.payload = payload;
	object.payload_len = record->payload_length;
	object.delivery = IMQUIC_MOQ_USE_SUBGROUP;
	object.first_of_subgroup = object.object_id == 0;
	if(imquic_moq_send_object(path->connection, &object) < 0) {
		free(payload); return -1;
	}
	free(payload);
	record->admitted = TRUE;
	uint64_t limit = threshold_v2(record);
	fprintf(path->admission, "%" PRIu64 ",%" G_GINT64_FORMAT ",%d,%" PRIu64
		",%" PRIu64 ",%" PRIu64 ",%u\n", path->admitted_objects, time_us,
		selected, record->release_ms, record->importance_rank, subgroup,
		record->payload_length);
	fprintf(path->gate, "%" PRIu64 ",%" G_GINT64_FORMAT ",%d,%u,%" PRIu64
		",%" PRIu64 ",%" PRIu64 ",%" PRIu64 "\n", path->admitted_objects,
		time_us, selected, record->payload_length, transport->queued_stream_bytes,
		transport->bytes_in_flight, transport->congestion_window_bytes, limit);
	fprintf(combined, "%" PRIu64 ",%" G_GINT64_FORMAT ",%s,%d,%" PRIu64
		",%" PRIu64 ",%" PRIu64 ",%u,%" PRIu64 ",%" PRIu64 ",%" PRIu64
		",%" PRIu64 "\n", global_index, time_us, path->name, selected,
		record->release_ms, record->importance_rank, subgroup,
		record->payload_length, transport->queued_stream_bytes,
		transport->bytes_in_flight, transport->congestion_window_bytes, limit);
	path->admitted_objects++;
	path->admitted_bytes += record->payload_length;
	return 0;
}

static void path_result_v2(publisher_path_v2 *path, uint32_t deadline,
		scheduler_stats_v2 *stats) {
	FILE *stream = fopen(path->result_path, "w");
	if(stream == NULL) return;
	fprintf(stream, "{\"source_objects\":%" PRIu64 ",\"source_bytes\":%" PRIu64
		",\"queued_objects\":%" PRIu64 ",\"queued_bytes\":%" PRIu64
		",\"deadline_ms\":%u,\"source_fully_queued\":%s"
		",\"workload_start_epoch_us\":%" G_GINT64_FORMAT
		",\"publisher_started_epoch_us\":%" G_GINT64_FORMAT
		",\"transport_queue_slack_bytes\":%" PRIu64
		",\"prague_warmup_ms\":%u,\"warmup_queued_objects\":%" PRIu64
		",\"warmup_queued_bytes\":%" PRIu64
		",\"scheduled\":true,\"scheduling_policy\":"
		"\"prague-first-high-biased-two-queue-select\",\"validated\":%s}\n",
		path->source_objects, path->source_bytes, path->admitted_objects,
		path->admitted_bytes, deadline,
		path->admitted_objects == path->source_objects ? "true" : "false",
		stats->workload_start_epoch_us, stats->publisher_started_epoch_us,
		queue_slack_v2, prague_warmup_ms_v2, path->warmup_objects,
		path->warmup_bytes, g_atomic_int_get(&failed) ? "false" : "true");
	fclose(stream);
}

static void scheduler_result_v2(scheduler_stats_v2 *stats) {
	FILE *stream = fopen(scheduler_result_path_v2, "w");
	if(stream == NULL) return;
	fprintf(stream, "{\"scheduling_policy\":"
		"\"prague-first-high-biased-two-queue-select\""
		",\"prague_blocked_events\":%" PRIu64
		",\"prague_blocked_time_us\":%" PRIu64
		",\"prague_preempted_classic\":%" PRIu64
		",\"warmup_started_epoch_us\":%" G_GINT64_FORMAT
		",\"warmup_finished_epoch_us\":%" G_GINT64_FORMAT
		",\"workload_start_epoch_us\":%" G_GINT64_FORMAT
		",\"validated\":%s}\n", stats->prague_blocked_events,
		stats->prague_blocked_time_us, stats->prague_preempted_classic,
		stats->warmup_started_epoch_us, stats->warmup_finished_epoch_us,
		stats->workload_start_epoch_us,
		g_atomic_int_get(&failed) ? "false" : "true");
	fclose(stream);
}

static void shutdown_v2(void) {
	closing_v2 = TRUE;
	for(unsigned int i = 0; i < PATH_COUNT; i++)
		if(paths_v2[i].connection != NULL)
			imquic_close_connection(paths_v2[i].connection, 0, "dual publisher done");
	g_usleep(100000);
	for(unsigned int i = 0; i < PATH_COUNT; i++)
		if(paths_v2[i].server != NULL) imquic_shutdown_endpoint(paths_v2[i].server);
}

static int run_dual_v2(const char *bind, uint32_t deadline_ms) {
	scheduler_stats_v2 stats = {0};
	FILE *combined = NULL;
	if(imquic_init(NULL) < 0) return 1;
	for(unsigned int i = 0; i < PATH_COUNT; i++) {
		if(index_path_v2(&paths_v2[i]) < 0 || open_outputs_v2(&paths_v2[i]) < 0 ||
				create_server_v2(&paths_v2[i], bind) < 0) {
			fail_case("could not initialize dual publisher path");
			break;
		}
	}
	combined = fopen(combined_path_v2, "w");
	if(combined == NULL) fail_case("could not open combined admission log");
	else fputs("admission_index,time_us,path,bundle_record_index,release_ms,"
		"importance_rank,subgroup_id,payload_bytes,queued_stream_bytes_before,"
		"bytes_in_flight_before,cwnd_bytes_before,queue_threshold_bytes\n", combined);
	if(!g_atomic_int_get(&failed) && wait_publishers_v2() < 0)
		fail_case("publishers did not become ready");
	if(!g_atomic_int_get(&failed) && warmup_v2(&paths_v2[PRAGUE_PATH], &stats) < 0)
		fail_case("Prague warm-up or drain failed");
	if(!g_atomic_int_get(&failed)) {
		FILE *ready = fopen(ready_path_v2, "w");
		if(ready == NULL || fputs("ready\n", ready) < 0 || fclose(ready) != 0)
			fail_case("could not signal publisher readiness");
	}
	if(!g_atomic_int_get(&failed) && wait_go_v2(&stats.workload_start_epoch_us) < 0)
		fail_case("could not synchronize workload start");

	gint64 started = g_get_monotonic_time();
	stats.publisher_started_epoch_us = g_get_real_time();
	gint64 deadline = started + (gint64)deadline_ms * 1000;
	gint64 next_metric = started, blocked_started = 0;
	uint64_t global_index = 0;
	gboolean classic_waiting = FALSE, prague_blocked = FALSE;
	while(!g_atomic_int_get(&stop_requested) && g_get_monotonic_time() < deadline) {
		gint64 now = g_get_monotonic_time();
		if(now >= next_metric) {
			for(unsigned int i = 0; i < PATH_COUNT; i++)
				metric_v2(&paths_v2[i], paths_v2[i].metrics, started);
			next_metric += METRIC_INTERVAL_US;
		}
		uint64_t elapsed = (uint64_t)((now - started) / 1000);
		uint64_t hn = UINT64_MAX, ln = UINT64_MAX;
		int high = select_record_v2(&paths_v2[PRAGUE_PATH], elapsed, &hn);
		int low = high < 0 ? select_record_v2(&paths_v2[CLASSIC_PATH], elapsed, &ln)
			: SELECT_NONE;
		if(high == SELECT_DONE && low == SELECT_DONE) {
			gboolean drained = TRUE;
			for(unsigned int i = 0; i < PATH_COUNT; i++) {
				imquic_transport_metrics value = {0};
				if(paths_v2[i].connection == NULL ||
						imquic_get_transport_metrics(paths_v2[i].connection, &value) < 0 ||
						value.bytes_in_flight || value.queued_stream_bytes) drained = FALSE;
			}
			if(drained) break;
			g_usleep(1000); continue;
		}
		if(high < 0 && low < 0) {
			uint64_t next = hn < ln ? hn : ln;
			gint64 sleep = next == UINT64_MAX ? 1000 :
				started + (gint64)next * 1000 - now;
			if(sleep < 1) sleep = 1;
			if(sleep > 1000) sleep = 1000;
			g_usleep((gulong)sleep); continue;
		}
		publisher_path_v2 *path = high >= 0 ? &paths_v2[PRAGUE_PATH]
			: &paths_v2[CLASSIC_PATH];
		int selected = high >= 0 ? high : low;
		if(high >= 0 && classic_waiting) {
			stats.prague_preempted_classic++;
			classic_waiting = FALSE;
		}
		imquic_transport_metrics transport = {0};
		if(!room_v2(path, &path->records[selected], &transport)) {
			if(high >= 0 && !prague_blocked) {
				prague_blocked = TRUE;
				blocked_started = now;
				stats.prague_blocked_events++;
			} else if(high < 0) classic_waiting = TRUE;
			g_usleep(1000); continue;
		}
		if(prague_blocked) {
			stats.prague_blocked_time_us += now - blocked_started;
			prague_blocked = FALSE;
		}
		classic_waiting = FALSE;
		if(path == &paths_v2[CLASSIC_PATH]) {
			uint64_t ignored;
			gint64 refreshed = g_get_monotonic_time();
			int recheck = select_record_v2(&paths_v2[PRAGUE_PATH],
				(uint64_t)((refreshed - started) / 1000), &ignored);
			if(recheck >= 0) {
				stats.prague_preempted_classic++;
				continue;
			}
			now = refreshed;
		}
		if(admit_v2(path, selected, now - started, global_index,
				&transport, combined) < 0) {
			fail_case("could not admit scheduled object"); break;
		}
		global_index++;
	}
	if(prague_blocked)
		stats.prague_blocked_time_us += g_get_monotonic_time() - blocked_started;
	for(unsigned int i = 0; i < PATH_COUNT; i++) {
		publisher_path_v2 *path = &paths_v2[i];
		metric_v2(path, path->metrics, started);
		if(path->connection != NULL && g_atomic_int_get(&path->publishing))
			imquic_moq_publish_done(path->connection, path->publish_request_id,
				IMQUIC_MOQ_PUBDONE_SUBSCRIPTION_ENDED,
				path->admitted_objects == path->source_objects ?
				"scheduled scene complete" : "scheduled scene deadline elapsed");
		path_result_v2(path, deadline_ms, &stats);
	}
	scheduler_result_v2(&stats);
	if(combined != NULL) fclose(combined);
	shutdown_v2();
	for(unsigned int i = 0; i < PATH_COUNT; i++) {
		if(paths_v2[i].source != NULL) fclose(paths_v2[i].source);
		if(paths_v2[i].metrics != NULL) fclose(paths_v2[i].metrics);
		if(paths_v2[i].admission != NULL) fclose(paths_v2[i].admission);
		if(paths_v2[i].gate != NULL) fclose(paths_v2[i].gate);
		free(paths_v2[i].records);
	}
	imquic_deinit();
	return g_atomic_int_get(&failed) ? 1 : 0;
}

static int configure_dual_v2(int argc, char **argv) {
	if(argc != 20) return 2;
	uint32_t deadline = strtoul(argv[3], NULL, 10);
	queue_slack_v2 = strtoull(argv[4], NULL, 10);
	prague_warmup_ms_v2 = strtoul(argv[5], NULL, 10);
	if(deadline == 0 || queue_slack_v2 == 0) return 2;
	ready_path_v2 = argv[6];
	go_path_v2 = argv[7];
	combined_path_v2 = argv[8];
	scheduler_result_path_v2 = argv[9];
	paths_v2[PRAGUE_PATH] = (publisher_path_v2){
		.name="high-prague", .mode="prague", .port=4444,
		.bundle_path=argv[10], .metrics_path=argv[11], .result_path=argv[12],
		.schedule_path=argv[13], .admission_path=argv[14]};
	paths_v2[CLASSIC_PATH] = (publisher_path_v2){
		.name="low-reno", .mode="reno", .port=4443,
		.bundle_path=argv[15], .metrics_path=argv[16], .result_path=argv[17],
		.schedule_path=argv[18], .admission_path=argv[19]};
	return run_dual_v2(argv[2], deadline);
}

int main(int argc, char **argv) {
	if(argc == 2 && !strcmp(argv[1], "schedule-self-test"))
		return schedule_self_test_v2();
	if(argc >= 2 && !strcmp(argv[1], "publisher-scheduled-dual-gated")) {
		moq_namespace.buffer = (uint8_t *)NAMESPACE_NAME;
		moq_namespace.length = strlen(NAMESPACE_NAME);
		moq_track.buffer = (uint8_t *)TRACK_NAME;
		moq_track.length = strlen(TRACK_NAME);
		return configure_dual_v2(argc, argv);
	}
	return legacy_fixture_main(argc, argv);
}
