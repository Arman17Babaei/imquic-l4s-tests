#include "imquic/imquic.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define TEST_PAYLOAD_SIZE (256 * 1024)
#define TEST_TIMEOUT_STEPS 1000
#define TEST_CERT_PATH "../../picoquic/certs/cert.pem"
#define TEST_KEY_PATH "../../picoquic/certs/key.pem"

static uint8_t *test_payload;
static uint64_t test_payload_size = TEST_PAYLOAD_SIZE;
static uint64_t server_received;
static uint64_t client_received;
static volatile gint test_done;
static volatile gint test_failed;
static volatile gint server_complete;
static imquic_transport_metrics final_metrics;
static gpointer active_client_connection;
static gboolean expect_prague = TRUE;

static void fail_test(const char *message)
{
	fprintf(stderr, "%s\n", message);
	g_atomic_int_set(&test_failed, 1);
	g_atomic_int_set(&test_done, 1);
}

static gboolean payload_matches(uint64_t offset, uint8_t *bytes, uint64_t length)
{
	if(bytes == NULL || offset > test_payload_size ||
		length > test_payload_size - offset)
		return FALSE;
	return memcmp(test_payload + offset, bytes, length) == 0;
}

static void server_stream_incoming(imquic_connection *conn, uint64_t stream_id,
		uint8_t *bytes, uint64_t length, gboolean complete)
{
	if(!payload_matches(server_received, bytes, length)) {
		fail_test("server received corrupted Prague traffic sample");
		return;
	}
	server_received += length;
	if(imquic_send_on_stream(conn, stream_id, bytes, length, complete) != 0) {
		fail_test("server failed to echo Prague traffic sample");
		return;
	}
	if(complete && server_received != test_payload_size)
		fail_test("server received incomplete Prague traffic sample");
	else if(complete)
		g_atomic_int_set(&server_complete, 1);
}

static void client_stream_incoming(imquic_connection *conn, uint64_t stream_id,
		uint8_t *bytes, uint64_t length, gboolean complete)
{
	(void)stream_id;
	if(!payload_matches(client_received, bytes, length)) {
		fail_test("client received corrupted Prague traffic sample");
		return;
	}
	client_received += length;
	if(complete) {
		if(client_received != test_payload_size) {
			fail_test("client received incomplete Prague traffic sample");
			return;
		}
		if(imquic_get_transport_metrics(conn, &final_metrics) != 0 ||
			final_metrics.congestion_window_bytes == 0 ||
			final_metrics.pacing_rate_bytes_per_second == 0 ||
			(expect_prague && final_metrics.prague_alpha_denominator == 0)) {
			fail_test("Prague metrics were unavailable after IMQUIC traffic");
			return;
		}
		g_atomic_int_set(&test_done, 1);
	}
}

static void client_new_connection(imquic_connection *conn, void *user_data)
{
	uint64_t stream_id = 0;
	(void)user_data;
	imquic_connection_ref(conn);
	g_atomic_pointer_set(&active_client_connection, conn);
	if(imquic_new_stream_id(conn, TRUE, &stream_id) != 0 ||
		imquic_send_on_stream(conn, stream_id, test_payload,
			test_payload_size, TRUE) != 0) {
		fail_test("client failed to generate Prague traffic sample");
	}
}

static void client_connection_failed(void *user_data)
{
	(void)user_data;
	fail_test("Prague loopback connection failed");
}

static int invalid_options_test(void)
{
	imquic_server *server = imquic_create_server("invalid-prague",
		IMQUIC_CONFIG_INIT,
		IMQUIC_CONFIG_LOCAL_PORT, 0,
		IMQUIC_CONFIG_TLS_CERT, TEST_CERT_PATH,
		IMQUIC_CONFIG_TLS_KEY, TEST_KEY_PATH,
		IMQUIC_CONFIG_ALPN, "imquic-l4s-test",
		IMQUIC_CONFIG_CONGESTION_CONTROL, IMQUIC_CONGESTION_PRAGUE,
		IMQUIC_CONFIG_CONGESTION_OPTIONS, "alpha_gain=0/1",
		IMQUIC_CONFIG_DONE, NULL);
	if(server != NULL) {
		fprintf(stderr, "invalid Prague options were accepted\n");
		imquic_shutdown_endpoint(server);
		return -1;
	}
	return 0;
}

static int ecn_configuration_test(void)
{
	imquic_server *server = imquic_create_server("ect0-smoke",
		IMQUIC_CONFIG_INIT,
		IMQUIC_CONFIG_LOCAL_PORT, 0,
		IMQUIC_CONFIG_TLS_CERT, TEST_CERT_PATH,
		IMQUIC_CONFIG_TLS_KEY, TEST_KEY_PATH,
		IMQUIC_CONFIG_ALPN, "imquic-l4s-test",
		IMQUIC_CONFIG_CONGESTION_CONTROL, IMQUIC_CONGESTION_RENO,
		IMQUIC_CONFIG_ECN, IMQUIC_ECN_ECT0,
		IMQUIC_CONFIG_DONE, NULL);
	if(server == NULL) {
		fprintf(stderr, "ECT(0) endpoint configuration was rejected\n");
		return -1;
	}
	imquic_shutdown_endpoint(server);
	server = imquic_create_server("invalid-ecn",
		IMQUIC_CONFIG_INIT,
		IMQUIC_CONFIG_LOCAL_PORT, 0,
		IMQUIC_CONFIG_TLS_CERT, TEST_CERT_PATH,
		IMQUIC_CONFIG_TLS_KEY, TEST_KEY_PATH,
		IMQUIC_CONFIG_ALPN, "imquic-l4s-test",
		IMQUIC_CONFIG_ECN, 99,
		IMQUIC_CONFIG_DONE, NULL);
	if(server != NULL) {
		fprintf(stderr, "invalid ECN mode was accepted\n");
		imquic_shutdown_endpoint(server);
		return -1;
	}
	return 0;
}

static int loopback_traffic_test(void)
{
	static const char *prague_options =
		"alpha_gain=1/16,ce_response=1/2,loss_beta=1/2,sudden_ce_threshold=1/2";
	imquic_server *server = NULL;
	imquic_client *client = NULL;
	uint16_t server_port;
	int ret = 0;

	test_payload_size = TEST_PAYLOAD_SIZE;
	expect_prague = TRUE;
	test_payload = g_malloc(TEST_PAYLOAD_SIZE);
	for(uint64_t i = 0; i < TEST_PAYLOAD_SIZE; i++)
		test_payload[i] = (uint8_t)(i % 251);
	server_received = 0;
	client_received = 0;
	g_atomic_int_set(&test_done, 0);
	g_atomic_int_set(&test_failed, 0);
	memset(&final_metrics, 0, sizeof(final_metrics));

	server = imquic_create_server("prague-traffic-server",
		IMQUIC_CONFIG_INIT,
		IMQUIC_CONFIG_LOCAL_BIND, "127.0.0.1",
		IMQUIC_CONFIG_LOCAL_PORT, 0,
		IMQUIC_CONFIG_TLS_CERT, TEST_CERT_PATH,
		IMQUIC_CONFIG_TLS_KEY, TEST_KEY_PATH,
		IMQUIC_CONFIG_ALPN, "imquic-l4s-test",
		IMQUIC_CONFIG_CONGESTION_CONTROL, IMQUIC_CONGESTION_PRAGUE,
		IMQUIC_CONFIG_CONGESTION_OPTIONS, prague_options,
		IMQUIC_CONFIG_DONE, NULL);
	if(server == NULL) {
		ret = -1;
		goto done;
	}
	server_port = imquic_get_endpoint_port(server);
	imquic_set_stream_incoming_cb(server, server_stream_incoming);
	imquic_start_endpoint(server);

	client = imquic_create_client("prague-traffic-client",
		IMQUIC_CONFIG_INIT,
		IMQUIC_CONFIG_LOCAL_BIND, "127.0.0.1",
		IMQUIC_CONFIG_LOCAL_PORT, 0,
		IMQUIC_CONFIG_REMOTE_HOST, "127.0.0.1",
		IMQUIC_CONFIG_REMOTE_PORT, server_port,
		IMQUIC_CONFIG_SNI, "localhost",
		IMQUIC_CONFIG_TLS_NO_VERIFY, TRUE,
		IMQUIC_CONFIG_ALPN, "imquic-l4s-test",
		IMQUIC_CONFIG_CONGESTION_CONTROL, IMQUIC_CONGESTION_PRAGUE,
		IMQUIC_CONFIG_CONGESTION_OPTIONS, prague_options,
		IMQUIC_CONFIG_DONE, NULL);
	if(client == NULL) {
		ret = -1;
		goto done;
	}
	imquic_set_new_connection_cb(client, client_new_connection);
	imquic_set_stream_incoming_cb(client, client_stream_incoming);
	imquic_set_connection_failed_cb(client, client_connection_failed);
	imquic_start_endpoint(client);

	for(int step = 0; step < TEST_TIMEOUT_STEPS &&
			!g_atomic_int_get(&test_done); step++)
		g_usleep(10000);
	if(!g_atomic_int_get(&test_done)) {
		fprintf(stderr, "timed out waiting for IMQUIC Prague traffic\n");
		ret = -1;
	} else if(g_atomic_int_get(&test_failed)) {
		ret = -1;
	} else {
		printf("IMQUIC Prague traffic: sent=%u, echoed=%" G_GUINT64_FORMAT
			", rtt_us=%" G_GUINT64_FORMAT ", cwin=%" G_GUINT64_FORMAT
			", pacing_Bps=%" G_GUINT64_FORMAT ", ect1=%" G_GUINT64_FORMAT
			", ce=%" G_GUINT64_FORMAT ", alpha=%u/%u\n",
			TEST_PAYLOAD_SIZE, client_received, final_metrics.smoothed_rtt_us,
			final_metrics.congestion_window_bytes,
			final_metrics.pacing_rate_bytes_per_second,
			final_metrics.ect1_packets, final_metrics.ce_packets,
			final_metrics.prague_alpha_numerator,
			final_metrics.prague_alpha_denominator);
	}

done:
	if(client != NULL)
		imquic_shutdown_endpoint(client);
	if(server != NULL)
		imquic_shutdown_endpoint(server);
	g_free(test_payload);
	test_payload = NULL;
	return ret;
}

static void prepare_payload(void)
{
	test_payload = g_malloc(test_payload_size);
	for(uint64_t i = 0; i < test_payload_size; i++)
		test_payload[i] = (uint8_t)(i % 251);
	server_received = 0;
	client_received = 0;
	g_atomic_int_set(&test_done, 0);
	g_atomic_int_set(&test_failed, 0);
	g_atomic_int_set(&server_complete, 0);
	memset(&final_metrics, 0, sizeof(final_metrics));
}

static int network_server(const char *bind_address, uint16_t port,
		imquic_congestion_controller controller, imquic_ecn_mode ecn_mode)
{
	static const char *options =
		"alpha_gain=1/16,ce_response=1/2,loss_beta=1/2,sudden_ce_threshold=1/2";
	imquic_server *server;
	int ret = 0;

	prepare_payload();
	server = imquic_create_server("prague-network-server",
		IMQUIC_CONFIG_INIT,
		IMQUIC_CONFIG_LOCAL_BIND, bind_address,
		IMQUIC_CONFIG_LOCAL_PORT, port,
		IMQUIC_CONFIG_TLS_CERT, TEST_CERT_PATH,
		IMQUIC_CONFIG_TLS_KEY, TEST_KEY_PATH,
		IMQUIC_CONFIG_ALPN, "imquic-l4s-test",
		IMQUIC_CONFIG_CONGESTION_CONTROL, controller,
		IMQUIC_CONFIG_CONGESTION_OPTIONS,
			controller == IMQUIC_CONGESTION_PRAGUE ? options : NULL,
		IMQUIC_CONFIG_ECN, ecn_mode,
		IMQUIC_CONFIG_DONE, NULL);
	if(server == NULL) {
		ret = -1;
		goto done;
	}
	imquic_set_stream_incoming_cb(server, server_stream_incoming);
	imquic_start_endpoint(server);
	printf("IMQUIC Prague server listening on %s:%u\n", bind_address, port);
	fflush(stdout);
	for(int step = 0; step < 6000 &&
			!g_atomic_int_get(&server_complete) &&
			!g_atomic_int_get(&test_failed); step++)
		g_usleep(10000);
	if(!g_atomic_int_get(&server_complete)) {
		fprintf(stderr, "timed out waiting for network Prague traffic\n");
		ret = -1;
	} else {
		g_usleep(500000);
		printf("IMQUIC Prague server: received=%" G_GUINT64_FORMAT "\n",
			server_received);
	}
	imquic_shutdown_endpoint(server);

done:
	g_free(test_payload);
	test_payload = NULL;
	return ret;
}

static void write_metrics_sample(FILE *csv, gint64 started_us)
{
	imquic_connection *conn = g_atomic_pointer_get(&active_client_connection);
	imquic_transport_metrics metrics = { 0 };
	if(csv == NULL || conn == NULL ||
			imquic_get_transport_metrics(conn, &metrics) != 0)
		return;
	fprintf(csv, "%" G_GINT64_FORMAT ",%" G_GUINT64_FORMAT
		",%" G_GUINT64_FORMAT ",%" G_GUINT64_FORMAT
		",%" G_GUINT64_FORMAT ",%" G_GUINT64_FORMAT
		",%" G_GUINT64_FORMAT ",%u,%u\n",
		g_get_monotonic_time() - started_us,
		metrics.smoothed_rtt_us, metrics.congestion_window_bytes,
		metrics.bytes_in_flight, metrics.pacing_rate_bytes_per_second,
		metrics.ect1_packets, metrics.ce_packets,
		metrics.prague_alpha_numerator, metrics.prague_alpha_denominator);
	fflush(csv);
}

static int network_client(const char *remote_host, uint16_t port,
		const char *metrics_csv_path, imquic_congestion_controller controller,
		imquic_ecn_mode ecn_mode)
{
	static const char *options =
		"alpha_gain=1/16,ce_response=1/2,loss_beta=1/2,sudden_ce_threshold=1/2";
	imquic_client *client;
	FILE *metrics_csv = NULL;
	gint64 started_us;
	int ret = 0;

	prepare_payload();
	g_atomic_pointer_set(&active_client_connection, NULL);
	if(metrics_csv_path != NULL) {
		metrics_csv = fopen(metrics_csv_path, "w");
		if(metrics_csv == NULL) {
			perror("failed to create Prague time-series CSV");
			ret = -1;
			goto done;
		}
		fprintf(metrics_csv, "time_us,rtt_us,cwnd_bytes,bytes_in_flight,"
			"pacing_Bps,ect1_packets,ce_packets,alpha_numerator,"
			"alpha_denominator\n");
	}
	client = imquic_create_client("prague-network-client",
		IMQUIC_CONFIG_INIT,
		IMQUIC_CONFIG_LOCAL_PORT, 0,
		IMQUIC_CONFIG_REMOTE_HOST, remote_host,
		IMQUIC_CONFIG_REMOTE_PORT, port,
		IMQUIC_CONFIG_SNI, "localhost",
		IMQUIC_CONFIG_TLS_NO_VERIFY, TRUE,
		IMQUIC_CONFIG_ALPN, "imquic-l4s-test",
		IMQUIC_CONFIG_CONGESTION_CONTROL, controller,
		IMQUIC_CONFIG_CONGESTION_OPTIONS,
			controller == IMQUIC_CONGESTION_PRAGUE ? options : NULL,
		IMQUIC_CONFIG_ECN, ecn_mode,
		IMQUIC_CONFIG_DONE, NULL);
	if(client == NULL) {
		ret = -1;
		goto done;
	}
	imquic_set_new_connection_cb(client, client_new_connection);
	imquic_set_stream_incoming_cb(client, client_stream_incoming);
	imquic_set_connection_failed_cb(client, client_connection_failed);
	started_us = g_get_monotonic_time();
	imquic_start_endpoint(client);
	for(int step = 0; step < 6000 && !g_atomic_int_get(&test_done); step++) {
		write_metrics_sample(metrics_csv, started_us);
		g_usleep(10000);
	}
	write_metrics_sample(metrics_csv, started_us);
	if(!g_atomic_int_get(&test_done) || g_atomic_int_get(&test_failed)) {
		fprintf(stderr, "network Prague traffic failed or timed out\n");
		ret = -1;
	} else {
		printf("IMQUIC %s network traffic: sent=%" G_GUINT64_FORMAT
			", echoed=%" G_GUINT64_FORMAT
			", rtt_us=%" G_GUINT64_FORMAT ", cwin=%" G_GUINT64_FORMAT
			", pacing_Bps=%" G_GUINT64_FORMAT ", ect1=%" G_GUINT64_FORMAT
			", ce=%" G_GUINT64_FORMAT ", alpha=%u/%u\n",
			controller == IMQUIC_CONGESTION_PRAGUE ? "Prague" :
				(ecn_mode == IMQUIC_ECN_ECT0 ? "Reno-ECT0" : "Reno"),
			test_payload_size, client_received, final_metrics.smoothed_rtt_us,
			final_metrics.congestion_window_bytes,
			final_metrics.pacing_rate_bytes_per_second,
			final_metrics.ect1_packets, final_metrics.ce_packets,
			final_metrics.prague_alpha_numerator,
			final_metrics.prague_alpha_denominator);
	}
	imquic_shutdown_endpoint(client);
	client = NULL;

done:
	if(metrics_csv != NULL)
		fclose(metrics_csv);
	imquic_connection *conn = g_atomic_pointer_get(&active_client_connection);
	if(conn != NULL) {
		g_atomic_pointer_set(&active_client_connection, NULL);
		imquic_connection_unref(conn);
	}
	g_free(test_payload);
	test_payload = NULL;
	return ret;
}

static int parse_network_options(int argc, char *argv[], gboolean client,
		const char **metrics_csv, imquic_congestion_controller *controller,
		imquic_ecn_mode *ecn_mode)
{
	int cc_index = client ? 5 : 4;
	int size_index = client ? 6 : 5;
	if(metrics_csv != NULL)
		*metrics_csv = argc > 4 && client ? argv[4] : NULL;
	*controller = IMQUIC_CONGESTION_PRAGUE;
	*ecn_mode = IMQUIC_ECN_DEFAULT;
	if(argc > cc_index) {
		if(strcmp(argv[cc_index], "prague") == 0)
			*controller = IMQUIC_CONGESTION_PRAGUE;
		else if(strcmp(argv[cc_index], "reno") == 0)
			*controller = IMQUIC_CONGESTION_RENO;
		else if(strcmp(argv[cc_index], "reno-ect0") == 0) {
			*controller = IMQUIC_CONGESTION_RENO;
			*ecn_mode = IMQUIC_ECN_ECT0;
		}
		else
			return -1;
	}
	if(argc > size_index) {
		char *end = NULL;
		uint64_t size = g_ascii_strtoull(argv[size_index], &end, 10);
		if(end == argv[size_index] || *end != '\0' || size == 0)
			return -1;
		test_payload_size = size;
	} else {
		test_payload_size = TEST_PAYLOAD_SIZE;
	}
	expect_prague = *controller == IMQUIC_CONGESTION_PRAGUE;
	return 0;
}

int main(int argc, char *argv[])
{
	int ret = imquic_init(NULL);
	imquic_congestion_controller controller = IMQUIC_CONGESTION_PRAGUE;
	imquic_ecn_mode ecn_mode = IMQUIC_ECN_DEFAULT;
	const char *metrics_csv = NULL;
	imquic_set_log_level(IMQUIC_LOG_WARN);
	if(ret == 0 && argc >= 4 && argc <= 6 && strcmp(argv[1], "--server") == 0) {
		if(parse_network_options(argc, argv, FALSE, NULL, &controller,
				&ecn_mode) != 0)
			ret = -1;
		else
			ret = network_server(argv[2], (uint16_t)strtoul(argv[3], NULL, 10),
				controller, ecn_mode);
	} else if(ret == 0 && argc >= 4 && argc <= 7 &&
			strcmp(argv[1], "--client") == 0) {
		if(parse_network_options(argc, argv, TRUE, &metrics_csv, &controller,
				&ecn_mode) != 0)
			ret = -1;
		else
			ret = network_client(argv[2], (uint16_t)strtoul(argv[3], NULL, 10),
				metrics_csv, controller, ecn_mode);
	}
	else if(ret == 0 && argc != 1) {
		fprintf(stderr, "usage: %s [--server BIND PORT [CC [BYTES]] | "
			"--client HOST PORT [CSV [CC [BYTES]]]]\n",
			argv[0]);
		ret = -1;
	} else if(ret == 0)
		ret = invalid_options_test();
	if(ret == 0 && argc == 1)
		ret = ecn_configuration_test();
	if(ret == 0 && argc == 1)
		ret = loopback_traffic_test();
	imquic_deinit();
	return ret;
}
