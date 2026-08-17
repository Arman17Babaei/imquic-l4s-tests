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
#define TRACK_NAME "sustained"
#define OBJECT_BYTES (16 * 1024)
#define OUTSTANDING_OBJECTS 10
#define METRIC_INTERVAL_US (10 * 1000)

static volatile gint stop_requested, failed, subscribed;
static imquic_connection *connection;
static uint64_t request_id, publish_request_id, track_alias = 1;
static uint64_t publish_track_alias = 2, next_object, received_objects;
static uint32_t duration_seconds = 60;
static const char *metrics_path;
static const char *result_path;
static volatile gint publishing;
static imquic_moq_namespace moq_namespace;
static imquic_moq_track moq_track;

static void fail_case(const char *message) {
	if(g_atomic_int_compare_and_exchange(&failed, 0, 1))
		fprintf(stderr, "sustained-moq: %s\n", message);
	g_atomic_int_set(&stop_requested, 1);
}
static void connection_failed(void *user_data) { (void) user_data; fail_case("QUIC connection failed"); }
static void connection_gone(imquic_connection *conn, uint64_t code, const char *reason) {
	(void) conn; (void) code; (void) reason; g_atomic_int_set(&stop_requested, 1);
}
static void publish_done(imquic_connection *conn, uint64_t id,
		imquic_moq_pub_done_code code, uint64_t streams, const char *reason) {
	(void) conn; (void) id; (void) code; (void) streams; (void) reason;
	g_atomic_int_set(&stop_requested, 1);
}
static void publish_accepted(imquic_connection *conn, uint64_t id,
		imquic_moq_request_parameters *parameters) {
	(void) conn; (void) parameters;
	if(id == publish_request_id)
		g_atomic_int_set(&publishing, 1);
}
static void publish_error(imquic_connection *conn, uint64_t id,
		imquic_moq_request_error_code code, const char *reason,
		uint64_t retry_interval, imquic_moq_redirect *redirect) {
	(void) conn; (void) id; (void) code; (void) reason;
	(void) retry_interval; (void) redirect;
	fail_case("client rejected the MoQ publish request");
}
static void new_connection(imquic_connection *conn, void *user_data) {
	(void) user_data; connection = conn; imquic_connection_ref(conn);
}
static void moq_ready(imquic_connection *conn) {
	if(connection == NULL) connection = conn;
}

static void subscribe_track(imquic_connection *conn) {
	imquic_moq_request_parameters parameters;
	imquic_moq_request_parameters_init_defaults(&parameters);
	parameters.forward_set = TRUE;
	parameters.forward = TRUE;
	parameters.group_order_set = TRUE;
	parameters.group_order = IMQUIC_MOQ_ORDERING_ASCENDING;
	imquic_moq_subscribe(conn, 0, &moq_namespace, &moq_track, &parameters);
}

static void subscriber_ready(imquic_connection *conn) {
	if(connection == NULL) connection = conn;
	subscribe_track(conn);
}

static void incoming_subscribe(imquic_connection *conn, uint64_t id,
		imquic_moq_namespace *tns, imquic_moq_track *tn,
		imquic_moq_request_parameters *parameters) {
	char ns[128], track[128];
	const char *ns_name = imquic_moq_namespace_str(tns, ns, sizeof(ns), TRUE);
	const char *track_name = imquic_moq_track_str(tn, track, sizeof(track));
	IMQUIC_LOG(IMQUIC_LOG_INFO, "Incoming sustained subscription '%s--%s' id=%" PRIu64 "\n",
		ns_name, track_name, id);
	(void) parameters;
	if(!imquic_moq_namespace_equals(tns, &moq_namespace) ||
			strcmp(track_name, TRACK_NAME)) {
		imquic_moq_reject_subscribe(conn, id, IMQUIC_MOQ_REQERR_DOES_NOT_EXIST,
			"unknown sustained track", 0, NULL);
		return;
	}
	request_id = id;
	if(imquic_moq_accept_subscribe(conn, id, track_alias, NULL, NULL) < 0) {
		fail_case("could not accept MoQ subscription");
		return;
	}
	g_atomic_int_set(&subscribed, 1);
	imquic_moq_request_parameters publish_parameters;
	imquic_moq_request_parameters_init_defaults(&publish_parameters);
	publish_parameters.group_order_set = TRUE;
	publish_parameters.group_order = IMQUIC_MOQ_ORDERING_ASCENDING;
	publish_parameters.forward_set = TRUE;
	publish_parameters.forward = TRUE;
	publish_request_id = imquic_moq_get_next_request_id(conn);
	if(imquic_moq_publish(conn, publish_request_id, tns, tn,
			publish_track_alias, &publish_parameters, NULL) < 0)
		fail_case("could not send MoQ publish request");
}

static void incoming_publish(imquic_connection *conn, uint64_t id,
		imquic_moq_namespace *tns, imquic_moq_track *tn,
		uint64_t alias, imquic_moq_request_parameters *parameters,
		GList *track_properties) {
	(void) parameters; (void) track_properties;
	if(!imquic_moq_namespace_equals(tns, &moq_namespace) ||
			strlen(TRACK_NAME) != tn->length ||
			memcmp(tn->buffer, TRACK_NAME, tn->length) != 0) {
		imquic_moq_reject_publish(conn, id, IMQUIC_MOQ_REQERR_DOES_NOT_EXIST,
			"unknown sustained track", 0, NULL);
		return;
	}
	imquic_moq_request_parameters accepted;
	imquic_moq_request_parameters_init_defaults(&accepted);
	if(imquic_moq_accept_publish(conn, id, &accepted) < 0)
		fail_case("could not accept MoQ publish request");
	(void) alias;
}

static void incoming_object(imquic_connection *conn, imquic_moq_object *object) {
	(void) conn;
	if(object->payload_len != OBJECT_BYTES || object->object_id != received_objects) {
		fail_case("MoQ object sequence or size validation failed");
		return;
	}
	for(size_t i = 0; i < object->payload_len; i++)
		if(object->payload[i] != (uint8_t)((object->object_id + i) & 0xff)) {
			fail_case("MoQ object payload validation failed");
			return;
		}
	received_objects++;
}

static int parse_mode(const char *mode, imquic_congestion_controller *cc, imquic_ecn_mode *ecn) {
	if(!strcmp(mode, "reno") || !strcmp(mode, "l4s-off")) { *cc = IMQUIC_CONGESTION_RENO; *ecn = IMQUIC_ECN_NOT_ECT; }
	else if(!strcmp(mode, "bbr")) { *cc = IMQUIC_CONGESTION_BBR; *ecn = IMQUIC_ECN_NOT_ECT; }
	else if(!strcmp(mode, "l4s-ect0")) { *cc = IMQUIC_CONGESTION_RENO; *ecn = IMQUIC_ECN_ECT0; }
	else if(!strcmp(mode, "prague") || !strcmp(mode, "l4s-on")) { *cc = IMQUIC_CONGESTION_PRAGUE; *ecn = IMQUIC_ECN_ECT1; }
	else return -1;
	return 0;
}

static void write_metric(FILE *metrics, gint64 started_us) {
	imquic_transport_metrics value = { 0 };
	if(connection == NULL || imquic_get_transport_metrics(connection, &value) < 0) return;
	fprintf(metrics, "%" G_GINT64_FORMAT ",%" G_GUINT64_FORMAT ",%" G_GUINT64_FORMAT
		",%" G_GUINT64_FORMAT ",%" G_GUINT64_FORMAT ",%" G_GUINT64_FORMAT
		",%" G_GUINT64_FORMAT ",%" G_GUINT64_FORMAT ",%" G_GUINT64_FORMAT
		",%" G_GUINT64_FORMAT ",%" G_GUINT64_FORMAT "\n",
		g_get_monotonic_time() - started_us, value.smoothed_rtt_us,
		value.congestion_window_bytes, value.bytes_in_flight,
		value.queued_stream_bytes, value.pacing_rate_bytes_per_second,
		value.ect0_packets, value.ect1_packets,
		value.ce_packets,
		(uint64_t)value.prague_alpha_numerator, (uint64_t)value.prague_alpha_denominator);
}

static int run_publisher(const char *bind_address, uint16_t port, const char *mode) {
	imquic_congestion_controller cc; imquic_ecn_mode ecn;
	if(parse_mode(mode, &cc, &ecn) < 0 || imquic_init(NULL) < 0) return 1;
	imquic_server *server = imquic_create_moq_server("sustained-moq-publisher",
		IMQUIC_CONFIG_INIT, IMQUIC_CONFIG_TLS_CERT, CERT_PATH,
		IMQUIC_CONFIG_TLS_KEY, KEY_PATH, IMQUIC_CONFIG_LOCAL_BIND, bind_address,
		IMQUIC_CONFIG_LOCAL_PORT, port, IMQUIC_CONFIG_CONGESTION_CONTROL, cc,
		IMQUIC_CONFIG_ECN, ecn, IMQUIC_CONFIG_RAW_QUIC, TRUE,
		IMQUIC_CONFIG_MOQ_VERSION, IMQUIC_MOQ_VERSION_ANY, IMQUIC_CONFIG_DONE, NULL);
	if(server == NULL) return 1;
	moq_namespace.buffer = (uint8_t *)NAMESPACE_NAME; moq_namespace.length = strlen(NAMESPACE_NAME);
	moq_track.buffer = (uint8_t *)TRACK_NAME; moq_track.length = strlen(TRACK_NAME);
	imquic_set_new_moq_connection_cb(server, new_connection);
	imquic_set_moq_ready_cb(server, moq_ready);
	imquic_set_incoming_subscribe_cb(server, incoming_subscribe);
	imquic_set_publish_accepted_cb(server, publish_accepted);
	imquic_set_publish_error_cb(server, publish_error);
	imquic_set_connection_failed_cb(server, connection_failed);
	imquic_set_moq_connection_gone_cb(server, connection_gone);
	imquic_start_endpoint(server);
	gint64 started = g_get_monotonic_time();
	gint64 deadline = started + (gint64)duration_seconds * G_USEC_PER_SEC;
	FILE *metrics = fopen(metrics_path, "w");
	uint8_t *payload = malloc(OBJECT_BYTES);
	if(metrics == NULL || payload == NULL) return 1;
	fputs("time_us,rtt_us,cwnd_bytes,bytes_in_flight,queued_stream_bytes,pacing_Bps,ect0_packets,ect1_packets,ce_packets,alpha_numerator,alpha_denominator\n", metrics);
	gint64 next_metric_time = started;
	while(!g_atomic_int_get(&stop_requested) && g_get_monotonic_time() < deadline) {
		gint64 now = g_get_monotonic_time();
		if(g_atomic_int_get(&publishing)) {
			imquic_transport_metrics transport = { 0 };
			if(imquic_get_transport_metrics(connection, &transport) == 0) {
				uint64_t outstanding_bytes = transport.bytes_in_flight + transport.queued_stream_bytes;
				uint64_t target_bytes = (uint64_t)OUTSTANDING_OBJECTS * OBJECT_BYTES;
				unsigned int objects_to_queue = outstanding_bytes < target_bytes ?
					(unsigned int)((target_bytes - outstanding_bytes + OBJECT_BYTES - 1) / OBJECT_BYTES) : 0;
				for(unsigned int buffered = 0; buffered < objects_to_queue; buffered++) {
					for(size_t i = 0; i < OBJECT_BYTES; i++)
						payload[i] = (uint8_t)((next_object + i) & 0xff);
					imquic_moq_object object = { 0 };
					object.request_id = publish_request_id;
					object.track_alias = publish_track_alias;
					object.object_id = next_object++; object.payload = payload;
					object.payload_len = OBJECT_BYTES; object.delivery = IMQUIC_MOQ_USE_SUBGROUP;
					object.first_of_subgroup = object.object_id == 0;
					if(imquic_moq_send_object(connection, &object) < 0) {
						fail_case("could not send MoQ object");
						break;
					}
				}
			}
		}
		if(now >= next_metric_time) { write_metric(metrics, started); next_metric_time += METRIC_INTERVAL_US; }
		g_usleep(1000);
	}
	if(g_atomic_int_get(&publishing))
		imquic_moq_publish_done(connection, publish_request_id,
			IMQUIC_MOQ_PUBDONE_SUBSCRIPTION_ENDED, "duration elapsed");
	else
		fail_case("publisher completed without a MoQ subscription");
	free(payload); fclose(metrics); imquic_shutdown_endpoint(server); imquic_deinit();
	return g_atomic_int_get(&failed) ? 1 : 0;
}

static int run_subscriber(const char *host, uint16_t port, const char *mode) {
	imquic_congestion_controller cc; imquic_ecn_mode ecn;
	if(parse_mode(mode, &cc, &ecn) < 0 || imquic_init(NULL) < 0) return 1;
	imquic_client *client = imquic_create_moq_client("sustained-moq-subscriber",
		IMQUIC_CONFIG_INIT, IMQUIC_CONFIG_TLS_CERT, CERT_PATH,
		IMQUIC_CONFIG_TLS_KEY, KEY_PATH, IMQUIC_CONFIG_TLS_NO_VERIFY, TRUE,
		IMQUIC_CONFIG_REMOTE_HOST, host, IMQUIC_CONFIG_REMOTE_PORT, port,
		IMQUIC_CONFIG_CONGESTION_CONTROL, cc, IMQUIC_CONFIG_ECN, ecn,
		IMQUIC_CONFIG_RAW_QUIC, TRUE, IMQUIC_CONFIG_MOQ_VERSION,
		IMQUIC_MOQ_VERSION_ANY, IMQUIC_CONFIG_DONE, NULL);
	if(client == NULL) return 1;
	imquic_set_new_moq_connection_cb(client, new_connection);
	imquic_set_moq_ready_cb(client, subscriber_ready);
	imquic_set_incoming_object_cb(client, incoming_object);
	imquic_set_incoming_publish_cb(client, incoming_publish);
	imquic_set_publish_done_cb(client, publish_done);
	imquic_set_connection_failed_cb(client, connection_failed);
	imquic_set_moq_connection_gone_cb(client, connection_gone);
	imquic_start_endpoint(client);
	gint64 subscriber_deadline = g_get_monotonic_time() +
		((gint64)duration_seconds + 5) * G_USEC_PER_SEC;
	while(!g_atomic_int_get(&stop_requested) &&
			g_get_monotonic_time() < subscriber_deadline)
		g_usleep(10000);
	if(received_objects == 0)
		fail_case("subscriber received no MoQ objects");
	if(result_path != NULL) {
		FILE *result = fopen(result_path, "w");
		if(result != NULL) {
			fprintf(result, "{\"received_objects\":%" PRIu64 ",\"received_bytes\":%" PRIu64 ",\"validated\":%s}\n",
				received_objects, received_objects * OBJECT_BYTES,
				g_atomic_int_get(&failed) ? "false" : "true");
			fclose(result);
		}
	}
	imquic_shutdown_endpoint(client); imquic_deinit();
	return g_atomic_int_get(&failed) ? 1 : 0;
}

int main(int argc, char **argv) {
	if(argc < 6) {
		fprintf(stderr, "usage: %s publisher BIND PORT MODE METRICS [DURATION]\n"
		"       %s subscriber HOST PORT MODE DURATION RESULT\n", argv[0], argv[0]);
		return 2;
	}
	moq_namespace.buffer = (uint8_t *)NAMESPACE_NAME;
	moq_namespace.length = strlen(NAMESPACE_NAME);
	moq_track.buffer = (uint8_t *)TRACK_NAME;
	moq_track.length = strlen(TRACK_NAME);
	if(!strcmp(argv[1], "publisher")) {
		metrics_path = argv[5]; if(argc > 6) duration_seconds = strtoul(argv[6], NULL, 10);
		return run_publisher(argv[2], (uint16_t)strtoul(argv[3], NULL, 10), argv[4]);
	}
	if(!strcmp(argv[1], "subscriber")) {
		duration_seconds = strtoul(argv[5], NULL, 10);
		if(argc > 6) result_path = argv[6];
		return run_subscriber(argv[2], (uint16_t)strtoul(argv[3], NULL, 10), argv[4]);
	}
	return 2;
}
