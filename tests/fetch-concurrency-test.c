#include <inttypes.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include <glib.h>
#include <imquic/imquic.h>
#include <imquic/moq.h>

#define CERT_PATH "../../picoquic/certs/cert.pem"
#define KEY_PATH "../../picoquic/certs/key.pem"
#define NAMESPACE_NAME "fetch-concurrency"
#define TRACK_NAME "payload"
#define OBJECT_BYTES (16U * 1024U)
#define MAX_QUEUED_BYTES (4U * 1024U * 1024U)
#define METRIC_INTERVAL_US (10 * 1000)

typedef struct fetch_state {
	uint64_t request_id;
	uint64_t expected_bytes;
	uint64_t sent_bytes;
	uint64_t received_bytes;
	uint64_t next_object;
	uint8_t priority;
	gboolean accepted;
	gboolean complete;
} fetch_state;

static volatile gint stop_requested;
static volatile gint failed;
static volatile gint ready;
static volatile gint received_requests;
static volatile gint accepted_requests;
static volatile gint rejected_requests;
static volatile gint completed_requests;
static imquic_connection *connection;
static imquic_moq_namespace moq_namespace;
static imquic_moq_track moq_track;
static fetch_state *fetches;
static uint32_t fetch_count;
static uint64_t total_bytes;
static uint32_t timeout_seconds;
static gint64 process_started_us;
static gint64 issue_started_us;
static gint64 issue_finished_us;
static gint64 all_open_us;
static gint64 transfer_finished_us;
static FILE *arrival_file;

static uint8_t fetch_priority(uint32_t index, uint32_t count) {
	if(count <= 1)
		return 128;
	return (uint8_t)(((uint64_t)index * 255U + (count - 1U) / 2U) /
		(count - 1U));
}

static uint64_t fetch_size(uint32_t index, uint32_t count, uint64_t bytes) {
	return bytes / count + (index < bytes % count ? 1U : 0U);
}

static void fail_case(const char *message) {
	if(g_atomic_int_compare_and_exchange(&failed, 0, 1))
		fprintf(stderr, "fetch-concurrency: %s\n", message);
}

static void request_stop(int signal_number) {
	(void)signal_number;
	g_atomic_int_set(&stop_requested, 1);
}

static void connection_failed(void *user_data) {
	(void)user_data;
	fail_case("QUIC connection failed");
	g_atomic_int_set(&stop_requested, 1);
}

static void connection_gone(imquic_connection *conn, uint64_t code,
		const char *reason) {
	fprintf(stderr, "fetch-concurrency: connection closed (%" PRIu64 ", %s)\n",
		code, reason == NULL ? "" : reason);
	if(conn == connection) {
		imquic_connection_unref(conn);
		connection = NULL;
	}
	g_atomic_int_set(&stop_requested, 1);
}

static void new_connection(imquic_connection *conn, void *user_data) {
	(void)user_data;
	connection = conn;
	imquic_connection_ref(conn);
}

static void server_ready(imquic_connection *conn) {
	if(connection == NULL)
		connection = conn;
	g_atomic_int_set(&ready, 1);
}

static void client_ready(imquic_connection *conn) {
	if(connection == NULL)
		connection = conn;
	issue_started_us = g_get_monotonic_time();
	for(uint32_t i = 0; i < fetch_count; i++) {
		imquic_moq_location_range range = {
			.start = { .group = i, .object = 0 },
			.end = { .group = i, .object = fetches[i].expected_bytes == 0 ? 0 :
				(fetches[i].expected_bytes - 1U) / OBJECT_BYTES }
		};
		imquic_moq_request_parameters parameters;
		imquic_moq_request_parameters_init_defaults(&parameters);
		parameters.subscriber_priority_set = TRUE;
		parameters.subscriber_priority = fetches[i].priority;
		parameters.group_order_set = TRUE;
		parameters.group_order = IMQUIC_MOQ_ORDERING_ASCENDING;
		fetches[i].request_id = imquic_moq_get_next_request_id(conn);
		if(imquic_moq_standalone_fetch(conn, fetches[i].request_id,
				&moq_namespace, &moq_track, &range, &parameters) < 0) {
			g_atomic_int_inc(&rejected_requests);
			fail_case("could not issue every FETCH request");
			break;
		}
	}
	issue_finished_us = g_get_monotonic_time();
	g_atomic_int_set(&ready, 1);
}

static gboolean valid_track(imquic_moq_namespace *tns, imquic_moq_track *tn) {
	return imquic_moq_namespace_equals(tns, &moq_namespace) &&
		tn->length == strlen(TRACK_NAME) &&
		memcmp(tn->buffer, TRACK_NAME, tn->length) == 0;
}

static void incoming_fetch(imquic_connection *conn, uint64_t id,
		imquic_moq_namespace *tns, imquic_moq_track *tn,
		imquic_moq_location_range *range,
		imquic_moq_request_parameters *parameters) {
	(void)conn;
	if(!valid_track(tns, tn) || range->start.group >= fetch_count ||
			range->end.group != range->start.group || range->start.object != 0) {
		imquic_moq_reject_fetch(conn, id, IMQUIC_MOQ_REQERR_DOES_NOT_EXIST,
			"invalid fetch range", 0, NULL);
		return;
	}
	uint32_t index = (uint32_t)range->start.group;
	uint64_t last_object = fetches[index].expected_bytes == 0 ? 0 :
		(fetches[index].expected_bytes - 1U) / OBJECT_BYTES;
	if(range->end.object != last_object || !parameters->subscriber_priority_set ||
			parameters->subscriber_priority != fetches[index].priority ||
			fetches[index].request_id != UINT64_MAX) {
		imquic_moq_reject_fetch(conn, id, IMQUIC_MOQ_REQERR_INVALID_RANGE,
			"unexpected fetch parameters", 0, NULL);
		return;
	}
	fetches[index].request_id = id;
	if(g_atomic_int_add(&received_requests, 1) + 1 == (gint)fetch_count)
		all_open_us = g_get_monotonic_time();
}

static void fetch_accepted(imquic_connection *conn, uint64_t id,
		imquic_moq_location *largest, imquic_moq_request_parameters *parameters,
		GList *track_properties) {
	(void)conn;
	(void)id;
	(void)largest;
	(void)parameters;
	(void)track_properties;
	g_atomic_int_inc(&accepted_requests);
}

static void fetch_error(imquic_connection *conn, uint64_t id,
		imquic_moq_request_error_code code, const char *reason,
		uint64_t retry_interval, imquic_moq_redirect *redirect) {
	(void)conn;
	(void)id;
	(void)code;
	(void)reason;
	(void)retry_interval;
	(void)redirect;
	g_atomic_int_inc(&rejected_requests);
	fail_case("a FETCH request was rejected");
}

static void incoming_object(imquic_connection *conn, imquic_moq_object *object) {
	(void)conn;
	if(object->group_id >= fetch_count) {
		fail_case("received an object for an unknown FETCH");
		return;
	}
	fetch_state *state = &fetches[object->group_id];
	uint64_t offset = object->object_id * OBJECT_BYTES;
	uint64_t remaining = state->expected_bytes - state->received_bytes;
	size_t expected = remaining > OBJECT_BYTES ? OBJECT_BYTES : (size_t)remaining;
	if(state->complete || object->object_id != state->next_object ||
			object->payload_len != expected) {
		fail_case("FETCH object sequence or size validation failed");
		return;
	}
	for(size_t i = 0; i < object->payload_len; i++) {
		uint8_t expected_byte = (uint8_t)((object->group_id + offset + i) & 0xff);
		if(object->payload[i] != expected_byte) {
			fail_case("FETCH object payload validation failed");
			return;
		}
	}
	state->next_object++;
	state->received_bytes += object->payload_len;
	if(arrival_file != NULL) {
		fprintf(arrival_file, "%" G_GINT64_FORMAT ",%" PRIu64 ",%" PRIu64
			",%zu,%u\n", g_get_monotonic_time() - process_started_us,
			object->group_id, object->object_id, object->payload_len, state->priority);
	}
	if(state->received_bytes == state->expected_bytes) {
		state->complete = TRUE;
		if(g_atomic_int_add(&completed_requests, 1) + 1 == (gint)fetch_count) {
			transfer_finished_us = g_get_monotonic_time();
			g_atomic_int_set(&stop_requested, 1);
		}
	}
}

static int parse_mode(const char *mode, imquic_congestion_controller *cc,
		imquic_ecn_mode *ecn) {
	if(!strcmp(mode, "prague") || !strcmp(mode, "l4s-on")) {
		*cc = IMQUIC_CONGESTION_PRAGUE;
		*ecn = IMQUIC_ECN_ECT1;
	} else if(!strcmp(mode, "reno") || !strcmp(mode, "l4s-off")) {
		*cc = IMQUIC_CONGESTION_RENO;
		*ecn = IMQUIC_ECN_NOT_ECT;
	} else {
		return -1;
	}
	return 0;
}

static int initialize_fetches(void) {
	fetches = calloc(fetch_count, sizeof(*fetches));
	if(fetches == NULL)
		return -1;
	for(uint32_t i = 0; i < fetch_count; i++) {
		fetches[i].request_id = UINT64_MAX;
		fetches[i].expected_bytes = fetch_size(i, fetch_count, total_bytes);
		fetches[i].priority = fetch_priority(i, fetch_count);
	}
	return 0;
}

static void write_metric(FILE *metrics) {
	imquic_transport_metrics value = { 0 };
	if(connection == NULL || imquic_get_transport_metrics(connection, &value) < 0)
		return;
	fprintf(metrics, "%" G_GINT64_FORMAT ",%" PRIu64 ",%" PRIu64
		",%" PRIu64 ",%" PRIu64 ",%" PRIu64 ",%" PRIu64
		",%" PRIu64 ",%" PRIu64 ",%" PRIu64 ",%" PRIu64
		",%u,%u,%u,%u\n",
		g_get_monotonic_time() - process_started_us, value.smoothed_rtt_us,
		value.min_rtt_us, value.congestion_window_bytes, value.bytes_in_flight,
		value.queued_stream_bytes, value.data_sent_bytes,
		value.pacing_rate_bytes_per_second, value.ect0_packets,
		value.ect1_packets, value.ce_packets,
		(uint32_t)g_atomic_int_get(&received_requests),
		(uint32_t)g_atomic_int_get(&accepted_requests),
		(uint32_t)g_atomic_int_get(&rejected_requests),
		(uint32_t)g_atomic_int_get(&completed_requests));
}

static void initialize_names(void) {
	moq_namespace.buffer = (uint8_t *)NAMESPACE_NAME;
	moq_namespace.length = strlen(NAMESPACE_NAME);
	moq_track.buffer = (uint8_t *)TRACK_NAME;
	moq_track.length = strlen(TRACK_NAME);
}

static void close_connection(const char *reason) {
	imquic_connection *active = connection;
	if(active == NULL)
		return;
	imquic_close_connection(active, 0, reason);
	gint64 deadline = g_get_monotonic_time() + G_USEC_PER_SEC;
	while(connection != NULL && g_get_monotonic_time() < deadline)
		g_usleep(1000);
}

static int run_server(const char *bind_address, uint16_t port, const char *mode,
		const char *metrics_path, const char *result_path) {
	imquic_congestion_controller cc;
	imquic_ecn_mode ecn;
	if(parse_mode(mode, &cc, &ecn) < 0 || initialize_fetches() < 0 ||
			imquic_init(NULL) < 0)
		return 1;
	imquic_server *server = imquic_create_moq_server("fetch-concurrency-server",
		IMQUIC_CONFIG_INIT, IMQUIC_CONFIG_TLS_CERT, CERT_PATH,
		IMQUIC_CONFIG_TLS_KEY, KEY_PATH, IMQUIC_CONFIG_LOCAL_BIND, bind_address,
		IMQUIC_CONFIG_LOCAL_PORT, port, IMQUIC_CONFIG_CONGESTION_CONTROL, cc,
		IMQUIC_CONFIG_ECN, ecn, IMQUIC_CONFIG_RAW_QUIC, TRUE,
		IMQUIC_CONFIG_MOQ_VERSION, IMQUIC_MOQ_VERSION_ANY,
		IMQUIC_CONFIG_MAX_BIDI_STREAMS, (int)fetch_count + 64,
		IMQUIC_CONFIG_MAX_UNI_STREAMS, (int)fetch_count + 64,
		IMQUIC_CONFIG_MOQ_MAX_REQUEST_UPDATES, (int)fetch_count + 64,
		IMQUIC_CONFIG_DONE, NULL);
	if(server == NULL)
		return 1;
	imquic_set_new_moq_connection_cb(server, new_connection);
	imquic_set_moq_ready_cb(server, server_ready);
	imquic_set_incoming_standalone_fetch_cb(server, incoming_fetch);
	imquic_set_connection_failed_cb(server, connection_failed);
	imquic_set_moq_connection_gone_cb(server, connection_gone);
	process_started_us = g_get_monotonic_time();
	imquic_start_endpoint(server);
	FILE *metrics = fopen(metrics_path, "w");
	if(metrics == NULL) {
		imquic_shutdown_endpoint(server);
		imquic_deinit();
		return 1;
	}
	fputs("time_us,rtt_us,min_rtt_us,cwnd_bytes,bytes_in_flight,"
		"queued_stream_bytes,data_sent_bytes,pacing_Bps,ect0_packets,"
		"ect1_packets,ce_packets,requests_received,requests_accepted,"
		"requests_rejected,requests_completed\n", metrics);
	gint64 deadline = process_started_us + (gint64)timeout_seconds * G_USEC_PER_SEC;
	gint64 next_metric = process_started_us;
	while(!g_atomic_int_get(&stop_requested) &&
			g_atomic_int_get(&received_requests) < (gint)fetch_count &&
			g_get_monotonic_time() < deadline) {
		gint64 now = g_get_monotonic_time();
		if(now >= next_metric) {
			write_metric(metrics);
			next_metric += METRIC_INTERVAL_US;
		}
		g_usleep(1000);
	}
	if(g_atomic_int_get(&received_requests) == (gint)fetch_count) {
		for(uint32_t i = 0; i < fetch_count; i++) {
			imquic_moq_location largest = {
				.group = i,
				.object = (fetches[i].expected_bytes - 1U) / OBJECT_BYTES
			};
			imquic_moq_request_parameters parameters;
			imquic_moq_request_parameters_init_defaults(&parameters);
			parameters.group_order_set = TRUE;
			parameters.group_order = IMQUIC_MOQ_ORDERING_ASCENDING;
			if(imquic_moq_accept_fetch(connection, fetches[i].request_id,
					&largest, &parameters, NULL) < 0) {
				fail_case("could not accept every FETCH request");
				break;
			}
			fetches[i].accepted = TRUE;
			g_atomic_int_inc(&accepted_requests);
		}
	} else if(!g_atomic_int_get(&stop_requested)) {
		fail_case("timeout before every FETCH request arrived");
	}
	uint8_t payload[OBJECT_BYTES];
	uint64_t finished_sending = 0;
	uint64_t sequence = 0;
	while(!g_atomic_int_get(&stop_requested) && !g_atomic_int_get(&failed) &&
			finished_sending < fetch_count && g_get_monotonic_time() < deadline) {
		gint64 now = g_get_monotonic_time();
		if(now >= next_metric) {
			write_metric(metrics);
			next_metric += METRIC_INTERVAL_US;
		}
		imquic_transport_metrics transport = { 0 };
		if(connection == NULL || imquic_get_transport_metrics(connection, &transport) < 0 ||
				transport.bytes_in_flight + transport.queued_stream_bytes >= MAX_QUEUED_BYTES) {
			g_usleep(1000);
			continue;
		}
		uint32_t index = (uint32_t)((sequence * UINT64_C(2654435761)) % fetch_count);
		sequence++;
		fetch_state *state = &fetches[index];
		if(!state->accepted || state->sent_bytes == state->expected_bytes)
			continue;
		size_t length = (size_t)(state->expected_bytes - state->sent_bytes);
		if(length > OBJECT_BYTES)
			length = OBJECT_BYTES;
		for(size_t i = 0; i < length; i++)
			payload[i] = (uint8_t)((index + state->sent_bytes + i) & 0xff);
		imquic_moq_object object = { 0 };
		object.request_id = state->request_id;
		object.group_id = index;
		object.subgroup_id = 0;
		object.object_id = state->next_object++;
		object.priority = state->priority;
		object.payload = payload;
		object.payload_len = length;
		object.delivery = IMQUIC_MOQ_USE_FETCH;
		object.end_of_stream = state->sent_bytes + length == state->expected_bytes;
		if(imquic_moq_send_object(connection, &object) < 0) {
			fail_case("could not queue a FETCH object");
			break;
		}
		state->sent_bytes += length;
		if(state->sent_bytes == state->expected_bytes)
			finished_sending++;
	}
	/* Queued bytes are not delivered yet. Keep the connection alive until the
	 * validating client closes it after receiving every FETCH, or time expires. */
	while(!g_atomic_int_get(&stop_requested) && !g_atomic_int_get(&failed) &&
			g_get_monotonic_time() < deadline) {
		gint64 now = g_get_monotonic_time();
		if(now >= next_metric) {
			write_metric(metrics);
			next_metric += METRIC_INTERVAL_US;
		}
		g_usleep(1000);
	}
	write_metric(metrics);
	fflush(metrics);
	fclose(metrics);
	FILE *result = fopen(result_path, "w");
	if(result != NULL) {
		uint64_t queued_bytes = 0;
		for(uint32_t i = 0; i < fetch_count; i++)
			queued_bytes += fetches[i].sent_bytes;
		fprintf(result, "{\"role\":\"server\",\"fetches\":%u,"
			"\"total_bytes\":%" PRIu64 ",\"requests_received\":%u,"
			"\"requests_accepted\":%u,\"queued_bytes\":%" PRIu64 ","
			"\"fetches_fully_queued\":%" PRIu64 ",\"all_open_seconds\":%.6f,"
			"\"timed_out\":%s,\"validated\":%s}\n",
			fetch_count, total_bytes, (uint32_t)g_atomic_int_get(&received_requests),
			(uint32_t)g_atomic_int_get(&accepted_requests), queued_bytes,
			finished_sending, all_open_us > 0 ?
			(double)(all_open_us - process_started_us) / G_USEC_PER_SEC : -1.0,
			g_get_monotonic_time() >= deadline ? "true" : "false",
			g_atomic_int_get(&failed) ? "false" : "true");
		fclose(result);
	}
	close_connection("fetch concurrency server complete");
	imquic_shutdown_endpoint(server);
	imquic_deinit();
	free(fetches);
	return g_atomic_int_get(&failed) ? 1 : 0;
}

static int run_client(const char *host, uint16_t port, const char *mode,
		const char *arrival_path, const char *result_path) {
	imquic_congestion_controller cc;
	imquic_ecn_mode ecn;
	if(parse_mode(mode, &cc, &ecn) < 0 || initialize_fetches() < 0 ||
			imquic_init(NULL) < 0)
		return 1;
	arrival_file = fopen(arrival_path, "w");
	if(arrival_file == NULL) {
		imquic_deinit();
		return 1;
	}
	fputs("time_us,fetch_index,object_id,payload_bytes,priority\n", arrival_file);
	imquic_client *client = imquic_create_moq_client("fetch-concurrency-client",
		IMQUIC_CONFIG_INIT, IMQUIC_CONFIG_TLS_CERT, CERT_PATH,
		IMQUIC_CONFIG_TLS_KEY, KEY_PATH, IMQUIC_CONFIG_TLS_NO_VERIFY, TRUE,
		IMQUIC_CONFIG_REMOTE_HOST, host, IMQUIC_CONFIG_REMOTE_PORT, port,
		IMQUIC_CONFIG_CONGESTION_CONTROL, cc, IMQUIC_CONFIG_ECN, ecn,
		IMQUIC_CONFIG_RAW_QUIC, TRUE, IMQUIC_CONFIG_MOQ_VERSION,
		IMQUIC_MOQ_VERSION_ANY,
		IMQUIC_CONFIG_MAX_BIDI_STREAMS, (int)fetch_count + 64,
		IMQUIC_CONFIG_MAX_UNI_STREAMS, (int)fetch_count + 64,
		IMQUIC_CONFIG_MOQ_MAX_REQUEST_UPDATES, (int)fetch_count + 64,
		IMQUIC_CONFIG_DONE, NULL);
	if(client == NULL) {
		fclose(arrival_file);
		imquic_deinit();
		return 1;
	}
	imquic_set_new_moq_connection_cb(client, new_connection);
	imquic_set_moq_ready_cb(client, client_ready);
	imquic_set_fetch_accepted_cb(client, fetch_accepted);
	imquic_set_fetch_error_cb(client, fetch_error);
	imquic_set_incoming_object_cb(client, incoming_object);
	imquic_set_connection_failed_cb(client, connection_failed);
	imquic_set_moq_connection_gone_cb(client, connection_gone);
	process_started_us = g_get_monotonic_time();
	imquic_start_endpoint(client);
	gint64 deadline = process_started_us + (gint64)timeout_seconds * G_USEC_PER_SEC;
	while(!g_atomic_int_get(&stop_requested) && g_get_monotonic_time() < deadline)
		g_usleep(1000);
	uint64_t received_bytes = 0;
	for(uint32_t i = 0; i < fetch_count; i++)
		received_bytes += fetches[i].received_bytes;
	gboolean timed_out = g_get_monotonic_time() >= deadline &&
		g_atomic_int_get(&completed_requests) < (gint)fetch_count;
	if(g_atomic_int_get(&completed_requests) != (gint)fetch_count)
		fail_case("not every FETCH completed before shutdown");
	fflush(arrival_file);
	fclose(arrival_file);
	FILE *result = fopen(result_path, "w");
	if(result != NULL) {
		fprintf(result, "{\"role\":\"client\",\"fetches\":%u,"
			"\"total_bytes\":%" PRIu64 ",\"issue_seconds\":%.6f,"
			"\"requests_accepted\":%u,\"requests_rejected\":%u,"
			"\"fetches_completed\":%u,\"received_bytes\":%" PRIu64 ","
			"\"finish_seconds\":%.6f,\"timed_out\":%s,\"validated\":%s}\n",
			fetch_count, total_bytes,
			issue_finished_us > issue_started_us ?
			(double)(issue_finished_us - issue_started_us) / G_USEC_PER_SEC : -1.0,
			(uint32_t)g_atomic_int_get(&accepted_requests),
			(uint32_t)g_atomic_int_get(&rejected_requests),
			(uint32_t)g_atomic_int_get(&completed_requests), received_bytes,
			transfer_finished_us > 0 ?
			(double)(transfer_finished_us - process_started_us) / G_USEC_PER_SEC : -1.0,
			timed_out ? "true" : "false",
			g_atomic_int_get(&failed) ? "false" : "true");
		fclose(result);
	}
	close_connection("fetch concurrency client complete");
	imquic_shutdown_endpoint(client);
	imquic_deinit();
	free(fetches);
	return g_atomic_int_get(&failed) ? 1 : 0;
}

static int self_test(void) {
	const uint32_t counts[] = { 1, 10, 100, 1000, 10000, 100000 };
	for(size_t c = 0; c < G_N_ELEMENTS(counts); c++) {
		uint64_t sum = 0;
		for(uint32_t i = 0; i < counts[c]; i++)
			sum += fetch_size(i, counts[c], UINT64_C(100000000));
		if(sum != UINT64_C(100000000))
			return 1;
		if(counts[c] == 1 && fetch_priority(0, 1) != 128)
			return 1;
		if(counts[c] > 1 && (fetch_priority(0, counts[c]) != 0 ||
				fetch_priority(counts[c] - 1, counts[c]) != 255))
			return 1;
	}
	puts("fetch-concurrency self-test: PASS");
	return 0;
}

int main(int argc, char **argv) {
	if(argc == 2 && !strcmp(argv[1], "self-test"))
		return self_test();
	if(argc != 10 || (strcmp(argv[1], "server") && strcmp(argv[1], "client"))) {
		fprintf(stderr,
			"usage: %s server BIND PORT MODE N TOTAL_BYTES TIMEOUT METRICS RESULT\n"
			"       %s client HOST PORT MODE N TOTAL_BYTES TIMEOUT ARRIVALS RESULT\n"
			"       %s self-test\n", argv[0], argv[0], argv[0]);
		return 2;
	}
	char *end = NULL;
	unsigned long parsed_port = strtoul(argv[3], &end, 10);
	if(*argv[3] == '\0' || *end != '\0' || parsed_port == 0 || parsed_port > 65535)
		return 2;
	unsigned long parsed_count = strtoul(argv[5], &end, 10);
	if(*argv[5] == '\0' || *end != '\0' || parsed_count == 0 || parsed_count > 1000000)
		return 2;
	unsigned long long parsed_bytes = strtoull(argv[6], &end, 10);
	if(*argv[6] == '\0' || *end != '\0' || parsed_bytes < parsed_count)
		return 2;
	unsigned long parsed_timeout = strtoul(argv[7], &end, 10);
	if(*argv[7] == '\0' || *end != '\0' || parsed_timeout == 0 || parsed_timeout > 3600)
		return 2;
	fetch_count = (uint32_t)parsed_count;
	total_bytes = (uint64_t)parsed_bytes;
	timeout_seconds = (uint32_t)parsed_timeout;
	initialize_names();
	signal(SIGINT, request_stop);
	signal(SIGTERM, request_stop);
	if(!strcmp(argv[1], "server"))
		return run_server(argv[2], (uint16_t)parsed_port, argv[4], argv[8], argv[9]);
	return run_client(argv[2], (uint16_t)parsed_port, argv[4], argv[8], argv[9]);
}
