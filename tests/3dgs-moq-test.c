#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include <imquic/imquic.h>
#include <imquic/moq.h>

#define CERT_PATH "../../picoquic/certs/cert.pem"
#define KEY_PATH "../../picoquic/certs/key.pem"
#define NAMESPACE_NAME "imquic-l4s"
#define TRACK_NAME "3dgs-scene"
#define BUNDLE_MAGIC "3DGSB001"
#define BUNDLE_VERSION 1
#define BUNDLE_HEADER_BYTES 24
#define MAX_RECORD_BYTES (64U * 1024U * 1024U)
#define MAX_QUEUED_BYTES (4U * 1024U * 1024U)
#define METRIC_INTERVAL_US (10 * 1000)

static volatile gint stop_requested, failed, publishing;
static gboolean allow_empty;
static imquic_connection *connection;
static uint64_t publish_request_id, publish_track_alias = 2;
static uint64_t received_objects, received_bytes;
static uint64_t received_gaussians, first_arrival_us, last_arrival_us;
static uint64_t queued_objects, queued_bytes;
static uint64_t source_objects, source_bytes;
static uint64_t outer_object_ids[3];
static uint32_t deadline_ms;
static gint64 subscriber_started_us, subscriber_started_real_us;
static volatile gint accepting_objects;
static GMutex receive_lock;
static const char *bundle_path, *metrics_path, *arrival_path, *result_path;
static FILE *received_bundle, *arrival_log;
static imquic_moq_namespace moq_namespace;
static imquic_moq_track moq_track;

static uint32_t load_u32_le(const uint8_t *bytes) {
	return ((uint32_t)bytes[0]) | ((uint32_t)bytes[1] << 8) |
		((uint32_t)bytes[2] << 16) | ((uint32_t)bytes[3] << 24);
}

static uint64_t load_u64_le(const uint8_t *bytes) {
	uint64_t value = 0;
	for(unsigned int i = 0; i < 8; i++) value |= ((uint64_t)bytes[i]) << (8 * i);
	return value;
}

static void store_u32_le(uint8_t *bytes, uint32_t value) {
	for(unsigned int i = 0; i < 4; i++) bytes[i] = (uint8_t)(value >> (8 * i));
}

static void store_u64_le(uint8_t *bytes, uint64_t value) {
	for(unsigned int i = 0; i < 8; i++) bytes[i] = (uint8_t)(value >> (8 * i));
}

static int read_exact(FILE *stream, void *buffer, size_t length) {
	return fread(buffer, 1, length, stream) == length ? 0 : -1;
}

static void fail_case(const char *message) {
	if(g_atomic_int_compare_and_exchange(&failed, 0, 1))
		fprintf(stderr, "3dgs-moq: %s\n", message);
	g_atomic_int_set(&stop_requested, 1);
}

static void connection_failed(void *user_data) {
	(void)user_data;
	fail_case("QUIC connection failed");
}

static void connection_gone(imquic_connection *conn, uint64_t code, const char *reason) {
	(void)code; (void)reason;
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

static void moq_ready(imquic_connection *conn) {
	if(connection == NULL) connection = conn;
}

static void close_active_connection(const char *reason) {
	imquic_connection *active = connection;
	if(active == NULL)
		return;
	imquic_close_connection(active, 0, reason);
	gint64 deadline = g_get_monotonic_time() + G_USEC_PER_SEC;
	while(connection != NULL && g_get_monotonic_time() < deadline)
		g_usleep(1000);
}

static void publish_accepted(imquic_connection *conn, uint64_t id,
		imquic_moq_request_parameters *parameters) {
	(void)conn; (void)parameters;
	if(id == publish_request_id) g_atomic_int_set(&publishing, 1);
}

static void publish_error(imquic_connection *conn, uint64_t id,
		imquic_moq_request_error_code code, const char *reason,
		uint64_t retry_interval, imquic_moq_redirect *redirect) {
	(void)conn; (void)id; (void)code; (void)reason; (void)retry_interval; (void)redirect;
	fail_case("peer rejected publish request");
}

static void publish_done(imquic_connection *conn, uint64_t id,
		imquic_moq_pub_done_code code, uint64_t streams, const char *reason) {
	(void)conn; (void)id; (void)code; (void)streams; (void)reason;
	g_atomic_int_set(&stop_requested, 1);
}

static void subscribe_track(imquic_connection *conn) {
	imquic_moq_request_parameters parameters;
	imquic_moq_request_parameters_init_defaults(&parameters);
	parameters.forward_set = TRUE;
	parameters.forward = TRUE;
	parameters.group_order_set = TRUE;
	parameters.group_order = IMQUIC_MOQ_ORDERING_ASCENDING;
	parameters.object_delivery_timeout_set = TRUE;
	parameters.object_delivery_timeout = deadline_ms;
	imquic_moq_subscribe(conn, 0, &moq_namespace, &moq_track, &parameters);
}

static void subscriber_ready(imquic_connection *conn) {
	if(connection == NULL) connection = conn;
	subscribe_track(conn);
}

static void incoming_subscribe(imquic_connection *conn, uint64_t id,
		imquic_moq_namespace *tns, imquic_moq_track *tn,
		imquic_moq_request_parameters *parameters) {
	(void)parameters;
	if(!imquic_moq_namespace_equals(tns, &moq_namespace) ||
			!imquic_moq_track_equals(tn, &moq_track)) {
		imquic_moq_reject_subscribe(conn, id, IMQUIC_MOQ_REQERR_DOES_NOT_EXIST,
			"unknown 3dgs track", 0, NULL);
		return;
	}
	if(imquic_moq_accept_subscribe(conn, id, 1, NULL, NULL) < 0) {
		fail_case("could not accept subscription");
		return;
	}
	imquic_moq_request_parameters publish_parameters;
	imquic_moq_request_parameters_init_defaults(&publish_parameters);
	publish_parameters.group_order_set = TRUE;
	publish_parameters.group_order = IMQUIC_MOQ_ORDERING_ASCENDING;
	publish_parameters.forward_set = TRUE;
	publish_parameters.forward = TRUE;
	publish_request_id = imquic_moq_get_next_request_id(conn);
	if(imquic_moq_publish(conn, publish_request_id, tns, tn,
			publish_track_alias, &publish_parameters, NULL) < 0)
		fail_case("could not send publish request");
}

static void incoming_publish(imquic_connection *conn, uint64_t id,
		imquic_moq_namespace *tns, imquic_moq_track *tn,
		uint64_t alias, imquic_moq_request_parameters *parameters,
		GList *track_properties) {
	(void)parameters; (void)track_properties; (void)alias;
	if(!imquic_moq_namespace_equals(tns, &moq_namespace) ||
			!imquic_moq_track_equals(tn, &moq_track)) {
		imquic_moq_reject_publish(conn, id, IMQUIC_MOQ_REQERR_DOES_NOT_EXIST,
			"unknown 3dgs track", 0, NULL);
		return;
	}
	imquic_moq_request_parameters accepted;
	imquic_moq_request_parameters_init_defaults(&accepted);
	if(imquic_moq_accept_publish(conn, id, &accepted) < 0)
		fail_case("could not accept publish request");
}

static int write_bundle_header(FILE *stream, uint32_t count, uint64_t bytes) {
	uint8_t header[BUNDLE_HEADER_BYTES] = {0};
	memcpy(header, BUNDLE_MAGIC, 8);
	store_u32_le(&header[8], BUNDLE_VERSION);
	store_u32_le(&header[12], count);
	store_u64_le(&header[16], bytes);
	return fwrite(header, 1, sizeof(header), stream) == sizeof(header) ? 0 : -1;
}

static void incoming_object(imquic_connection *conn, imquic_moq_object *object) {
	(void)conn;
	g_mutex_lock(&receive_lock);
	if(!g_atomic_int_get(&accepting_objects)) {
		g_mutex_unlock(&receive_lock);
		return;
	}

	gint64 now = g_get_monotonic_time();
	gint64 arrival_time_us = now - subscriber_started_us;
	if(arrival_time_us < 0 || arrival_time_us > (gint64)deadline_ms * 1000) {
		g_mutex_unlock(&receive_lock);
		return;
	}
	if(received_bundle == NULL || arrival_log == NULL ||
			object->payload == NULL || object->payload_len == 0) {
		fail_case("invalid incoming 3dgs object");
		g_mutex_unlock(&receive_lock);
		return;
	}
	if(object->payload_len > UINT32_MAX) {
		fail_case("incoming object exceeds bundle format");
		g_mutex_unlock(&receive_lock);
		return;
	}
	if(object->payload_len < 32 ||
			load_u32_le(&object->payload[0]) != 0x47535033 ||
			load_u32_le(&object->payload[4]) != 1) {
		fail_case("incoming object has invalid embedded 3dgs header");
		g_mutex_unlock(&receive_lock);
		return;
	}
	uint32_t embedded_object_id = load_u32_le(&object->payload[16]);
	uint32_t num_gaussians = load_u32_le(&object->payload[20]);
	uint32_t subgroup_id = load_u32_le(&object->payload[28]);
	if(num_gaussians == 0 || subgroup_id > 2) {
		fail_case("incoming object has invalid embedded 3dgs metadata");
		g_mutex_unlock(&receive_lock);
		return;
	}

	uint64_t record_index = received_objects;
	uint64_t cumulative_bytes = received_bytes + object->payload_len;
	uint64_t cumulative_gaussians = received_gaussians + num_gaussians;
	uint8_t length[4];
	store_u32_le(length, (uint32_t)object->payload_len);
	if(fwrite(length, 1, sizeof(length), received_bundle) != sizeof(length) ||
			fwrite(object->payload, 1, object->payload_len, received_bundle) != object->payload_len) {
		fail_case("could not write received bundle");
		g_mutex_unlock(&receive_lock);
		return;
	}
	if(fprintf(arrival_log,
			"%" G_GINT64_FORMAT ",%" PRIu64 ",%zu,%" PRIu64 ",%u,%" PRIu64 ",%u,%u\n",
			arrival_time_us, record_index, object->payload_len, cumulative_bytes,
			num_gaussians, cumulative_gaussians, subgroup_id, embedded_object_id) < 0) {
		fail_case("could not write object arrival timeline");
		g_mutex_unlock(&receive_lock);
		return;
	}
	received_objects++;
	received_bytes = cumulative_bytes;
	received_gaussians = cumulative_gaussians;
	if(record_index == 0) first_arrival_us = (uint64_t)arrival_time_us;
	last_arrival_us = (uint64_t)arrival_time_us;
	g_mutex_unlock(&receive_lock);
}

static int parse_mode(const char *mode, imquic_congestion_controller *cc, imquic_ecn_mode *ecn) {
	if(!strcmp(mode, "reno")) { *cc = IMQUIC_CONGESTION_RENO; *ecn = IMQUIC_ECN_NOT_ECT; }
	else if(!strcmp(mode, "bbr")) { *cc = IMQUIC_CONGESTION_BBR; *ecn = IMQUIC_ECN_NOT_ECT; }
	else if(!strcmp(mode, "prague")) { *cc = IMQUIC_CONGESTION_PRAGUE; *ecn = IMQUIC_ECN_ECT1; }
	else return -1;
	return 0;
}

static void write_metric(FILE *metrics, gint64 started_us) {
	imquic_transport_metrics value = {0};
	if(metrics == NULL || connection == NULL || imquic_get_transport_metrics(connection, &value) < 0)
		return;
	fprintf(metrics, "%" G_GINT64_FORMAT ",%" G_GUINT64_FORMAT ",%" G_GUINT64_FORMAT
		",%" G_GUINT64_FORMAT ",%" G_GUINT64_FORMAT ",%" G_GUINT64_FORMAT
		",%" G_GUINT64_FORMAT ",%" G_GUINT64_FORMAT ",%" G_GUINT64_FORMAT
		",%" G_GUINT64_FORMAT ",%" G_GUINT64_FORMAT "\n",
		g_get_monotonic_time() - started_us, value.smoothed_rtt_us,
		value.congestion_window_bytes, value.bytes_in_flight,
		value.queued_stream_bytes, value.pacing_rate_bytes_per_second,
		value.ect0_packets, value.ect1_packets, value.ce_packets,
		(uint64_t)value.prague_alpha_numerator, (uint64_t)value.prague_alpha_denominator);
}

static uint64_t embedded_subgroup(const uint8_t *payload, size_t length) {
	/* 3dgs_over_moq protocol.py: subgroup_id is uint32 at byte offset 28. */
	if(length < 32) return 0;
	uint64_t subgroup_id = load_u32_le(&payload[28]);
	return subgroup_id <= 2 ? subgroup_id : 0;
}

static int open_source_bundle(FILE **source) {
	uint8_t header[BUNDLE_HEADER_BYTES];
	*source = fopen(bundle_path, "rb");
	if(*source == NULL || read_exact(*source, header, sizeof(header)) < 0) return -1;
	if(memcmp(header, BUNDLE_MAGIC, 8) || load_u32_le(&header[8]) != BUNDLE_VERSION)
		return -1;
	source_objects = load_u32_le(&header[12]);
	source_bytes = load_u64_le(&header[16]);
	return 0;
}

static int read_record(FILE *source, uint8_t **payload, uint32_t *length) {
	uint8_t encoded_len[4];
	size_t got = fread(encoded_len, 1, sizeof(encoded_len), source);
	if(got == 0 && feof(source)) return 1;
	if(got != sizeof(encoded_len)) return -1;
	*length = load_u32_le(encoded_len);
	if(*length == 0 || *length > MAX_RECORD_BYTES) return -1;
	*payload = malloc(*length);
	if(*payload == NULL || read_exact(source, *payload, *length) < 0) {
		free(*payload);
		*payload = NULL;
		return -1;
	}
	return 0;
}

static int run_publisher(const char *bind_address, uint16_t port, const char *mode) {
	imquic_congestion_controller cc;
	imquic_ecn_mode ecn;
	if(parse_mode(mode, &cc, &ecn) < 0 || imquic_init(NULL) < 0) return 1;
	imquic_server *server = imquic_create_moq_server("3dgs-moq-publisher",
		IMQUIC_CONFIG_INIT, IMQUIC_CONFIG_TLS_CERT, CERT_PATH,
		IMQUIC_CONFIG_TLS_KEY, KEY_PATH, IMQUIC_CONFIG_LOCAL_BIND, bind_address,
		IMQUIC_CONFIG_LOCAL_PORT, port, IMQUIC_CONFIG_CONGESTION_CONTROL, cc,
		IMQUIC_CONFIG_ECN, ecn, IMQUIC_CONFIG_RAW_QUIC, TRUE,
		IMQUIC_CONFIG_MOQ_VERSION, IMQUIC_MOQ_VERSION_19, IMQUIC_CONFIG_DONE, NULL);
	if(server == NULL) return 1;
	imquic_set_new_moq_connection_cb(server, new_connection);
	imquic_set_moq_ready_cb(server, moq_ready);
	imquic_set_incoming_subscribe_cb(server, incoming_subscribe);
	imquic_set_publish_accepted_cb(server, publish_accepted);
	imquic_set_publish_error_cb(server, publish_error);
	imquic_set_connection_failed_cb(server, connection_failed);
	imquic_set_moq_connection_gone_cb(server, connection_gone);
	imquic_start_endpoint(server);

	while(!g_atomic_int_get(&stop_requested) && !g_atomic_int_get(&publishing)) g_usleep(1000);
	FILE *source = NULL, *metrics = NULL;
	if(!g_atomic_int_get(&failed) && open_source_bundle(&source) < 0)
		fail_case("could not open source scene bundle");
	if(metrics_path != NULL) {
		metrics = fopen(metrics_path, "w");
		if(metrics != NULL)
			fputs("time_us,rtt_us,cwnd_bytes,bytes_in_flight,queued_stream_bytes,pacing_Bps,ect0_packets,ect1_packets,ce_packets,alpha_numerator,alpha_denominator\n", metrics);
	}
	gint64 started = g_get_monotonic_time();
	gint64 deadline = started + (gint64)deadline_ms * 1000;
	gint64 next_metric = started;
	gboolean source_done = FALSE;
	while(!g_atomic_int_get(&stop_requested) && g_get_monotonic_time() < deadline) {
		gint64 now = g_get_monotonic_time();
		if(now >= next_metric) { write_metric(metrics, started); next_metric += METRIC_INTERVAL_US; }
		if(source_done) {
			imquic_transport_metrics transport = {0};
			if(connection != NULL && imquic_get_transport_metrics(connection, &transport) == 0 &&
					transport.bytes_in_flight == 0 && transport.queued_stream_bytes == 0)
				break;
			g_usleep(1000);
			continue;
		}
		imquic_transport_metrics transport = {0};
		if(connection == NULL || imquic_get_transport_metrics(connection, &transport) < 0 ||
				transport.bytes_in_flight + transport.queued_stream_bytes >= MAX_QUEUED_BYTES) {
			g_usleep(1000);
			continue;
		}
		uint8_t *payload = NULL;
		uint32_t length = 0;
		int read_status = read_record(source, &payload, &length);
		if(read_status == 1) { source_done = TRUE; continue; }
		if(read_status < 0) { fail_case("invalid source scene bundle record"); break; }
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
			fail_case("could not queue 3dgs object");
		else { queued_objects++; queued_bytes += length; }
		free(payload);
	}
	write_metric(metrics, started);
	if(g_atomic_int_get(&publishing) && connection != NULL)
		imquic_moq_publish_done(connection, publish_request_id,
			IMQUIC_MOQ_PUBDONE_SUBSCRIPTION_ENDED,
			source_done ? "scene complete" : "scene deadline elapsed");
	if(source != NULL) fclose(source);
	if(metrics != NULL) fclose(metrics);
	if(result_path != NULL) {
		FILE *result = fopen(result_path, "w");
		if(result != NULL) {
			fprintf(result,
				"{\"source_objects\":%" PRIu64 ",\"source_bytes\":%" PRIu64
				",\"queued_objects\":%" PRIu64 ",\"queued_bytes\":%" PRIu64
				",\"deadline_ms\":%u,\"source_fully_queued\":%s,\"validated\":%s}\n",
				source_objects, source_bytes, queued_objects, queued_bytes, deadline_ms,
				source_done ? "true" : "false", g_atomic_int_get(&failed) ? "false" : "true");
			fclose(result);
		}
	}
	close_active_connection("3dgs publisher complete");
	imquic_shutdown_endpoint(server);
	imquic_deinit();
	return g_atomic_int_get(&failed) ? 1 : 0;
}

static int run_subscriber(const char *host, uint16_t port, const char *mode) {
	imquic_congestion_controller cc;
	imquic_ecn_mode ecn;
	if(parse_mode(mode, &cc, &ecn) < 0 || imquic_init(NULL) < 0) return 1;
	received_bundle = fopen(bundle_path, "wb+");
	arrival_log = arrival_path != NULL ? fopen(arrival_path, "w") : NULL;
	if(received_bundle == NULL || arrival_log == NULL ||
			write_bundle_header(received_bundle, 0, 0) < 0) {
		if(received_bundle != NULL) fclose(received_bundle);
		if(arrival_log != NULL) fclose(arrival_log);
		return 1;
	}
	fputs("arrival_time_us,bundle_record_index,payload_bytes,cumulative_bytes,"
		"num_gaussians,cumulative_gaussians,subgroup_id,object_id\n", arrival_log);
	imquic_client *client = imquic_create_moq_client("3dgs-moq-subscriber",
		IMQUIC_CONFIG_INIT, IMQUIC_CONFIG_TLS_CERT, CERT_PATH,
		IMQUIC_CONFIG_TLS_KEY, KEY_PATH, IMQUIC_CONFIG_TLS_NO_VERIFY, TRUE,
		IMQUIC_CONFIG_REMOTE_HOST, host, IMQUIC_CONFIG_REMOTE_PORT, port,
		IMQUIC_CONFIG_CONGESTION_CONTROL, cc, IMQUIC_CONFIG_ECN, ecn,
		IMQUIC_CONFIG_RAW_QUIC, TRUE, IMQUIC_CONFIG_MOQ_VERSION,
		IMQUIC_MOQ_VERSION_19, IMQUIC_CONFIG_DONE, NULL);
	if(client == NULL) {
		fclose(received_bundle);
		fclose(arrival_log);
		received_bundle = NULL;
		arrival_log = NULL;
		return 1;
	}
	imquic_set_new_moq_connection_cb(client, new_connection);
	imquic_set_moq_ready_cb(client, subscriber_ready);
	imquic_set_incoming_object_cb(client, incoming_object);
	imquic_set_incoming_publish_cb(client, incoming_publish);
	imquic_set_publish_done_cb(client, publish_done);
	imquic_set_connection_failed_cb(client, connection_failed);
	imquic_set_moq_connection_gone_cb(client, connection_gone);
	subscriber_started_us = g_get_monotonic_time();
	subscriber_started_real_us = g_get_real_time();
	gint64 deadline = subscriber_started_us + (gint64)deadline_ms * 1000;
	g_atomic_int_set(&accepting_objects, 1);
	imquic_start_endpoint(client);
	while(!g_atomic_int_get(&stop_requested) && g_get_monotonic_time() < deadline) g_usleep(1000);
	g_atomic_int_set(&accepting_objects, 0);

	/* Drain an object callback already inside the critical section before finalizing. */
	g_mutex_lock(&receive_lock);
	if(received_objects == 0 && !allow_empty)
		fail_case("subscriber received no 3dgs objects before deadline");
	gint64 finalized_us = g_get_monotonic_time() - subscriber_started_us;
	if(fseek(received_bundle, 0, SEEK_SET) != 0 ||
			write_bundle_header(received_bundle, (uint32_t)received_objects, received_bytes) < 0)
		fail_case("could not finalize received bundle header");
	if(fflush(received_bundle) != 0 || fflush(arrival_log) != 0)
		fail_case("could not flush received 3dgs artifacts");
	g_mutex_unlock(&receive_lock);
	fclose(received_bundle);
	fclose(arrival_log);
	received_bundle = NULL;
	arrival_log = NULL;
	if(result_path != NULL) {
		FILE *result = fopen(result_path, "w");
		if(result != NULL) {
			fprintf(result,
				"{\"received_objects\":%" PRIu64 ",\"received_bytes\":%" PRIu64
				",\"received_gaussians\":%" PRIu64 ",\"deadline_ms\":%u"
				",\"first_object_time_us\":%" PRIu64 ",\"last_object_time_us\":%" PRIu64
				",\"bundle_finalized_time_us\":%" G_GINT64_FORMAT
				",\"started_epoch_us\":%" G_GINT64_FORMAT
				",\"timeline_origin\":\"subscriber_endpoint_start\""
				",\"elapsed_ms\":%.3f,\"validated\":%s}\n",
				received_objects, received_bytes, received_gaussians, deadline_ms,
				first_arrival_us, last_arrival_us, finalized_us, subscriber_started_real_us,
				(g_get_monotonic_time() - subscriber_started_us) / 1000.0,
				g_atomic_int_get(&failed) ? "false" : "true");
			fclose(result);
		}
	}
	close_active_connection("3dgs subscriber complete");
	imquic_shutdown_endpoint(client);
	imquic_deinit();
	return g_atomic_int_get(&failed) ? 1 : 0;
}

int main(int argc, char **argv) {
	if(argc != 9 && argc != 10) {
		fprintf(stderr,
			"usage: %s publisher BIND PORT MODE BUNDLE DEADLINE_MS METRICS RESULT\n"
			"       %s subscriber HOST PORT MODE OUTPUT_BUNDLE DEADLINE_MS ARRIVALS RESULT [allow-empty]\n",
			argv[0], argv[0]);
		return 2;
	}
	moq_namespace.buffer = (uint8_t *)NAMESPACE_NAME;
	moq_namespace.length = strlen(NAMESPACE_NAME);
	moq_track.buffer = (uint8_t *)TRACK_NAME;
	moq_track.length = strlen(TRACK_NAME);
	bundle_path = argv[5];
	deadline_ms = (uint32_t)strtoul(argv[6], NULL, 10);
	if(deadline_ms == 0) return 2;
	result_path = argv[8];
	if(!strcmp(argv[1], "publisher")) {
		if(argc != 9) return 2;
		metrics_path = strcmp(argv[7], "-") ? argv[7] : NULL;
		return run_publisher(argv[2], (uint16_t)strtoul(argv[3], NULL, 10), argv[4]);
	}
	if(!strcmp(argv[1], "subscriber")) {
		if(argc == 10) {
			if(strcmp(argv[9], "allow-empty")) return 2;
			allow_empty = TRUE;
		}
		arrival_path = strcmp(argv[7], "-") ? argv[7] : NULL;
		if(arrival_path == NULL) return 2;
		return run_subscriber(argv[2], (uint16_t)strtoul(argv[3], NULL, 10), argv[4]);
	}
	return 2;
}
