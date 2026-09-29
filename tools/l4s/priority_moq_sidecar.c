#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
/* Native draft-19 MOQT half of the localhost 3DGS sidecar.
 *
 * stdin: one compact JSON command per line (from priority_ws_bridge.mjs)
 * stdout: [u32be JSON bytes][JSON][optional Object payload]
 */
#include <arpa/inet.h>
#include <inttypes.h>
#include <jansson.h>
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <glib.h>
#include <imquic/imquic.h>
#include <imquic/moq.h>

typedef struct fetch_state {
	uint64_t request_id;
	uint64_t update_id;
	char *tile_id;
	char *track;
	int refinement;
	uint8_t applied_priority;
	uint8_t desired_priority;
	uint8_t pending_priority;
	uint64_t applied_epoch;
	uint64_t desired_epoch;
	uint64_t pending_epoch;
	gboolean update_pending;
	gboolean complete;
	GHashTable *objects;
} fetch_state;

static volatile sig_atomic_t stopping = 0;
static imquic_connection *connection = NULL;
static GHashTable *fetches = NULL;
static GHashTable *updates = NULL;
static GMutex state_mutex;
static GMutex output_mutex;

static gpointer duplicate_bytes(const void *source, gsize size) {
	gpointer copy = g_malloc(size);
	memcpy(copy, source, size);
	return copy;
}

static json_t *priority_event_message(fetch_state *fetch, const char *type,
		uint8_t old_priority, uint8_t new_priority, uint64_t epoch, uint64_t update_id) {
	return json_pack("{s:s,s:I,s:I,s:s,s:i,s:i,s:i,s:I,s:I}",
		"type", type, "request_id", (json_int_t)fetch->request_id,
		"update_id", (json_int_t)update_id, "tile_id", fetch->tile_id,
		"refinement", fetch->refinement, "old_priority", old_priority,
		"new_priority", new_priority, "epoch", (json_int_t)epoch,
		"monotonic_us", (json_int_t)g_get_monotonic_time());
}

static json_t *metrics_event_message(const imquic_transport_metrics *metrics, gint64 monotonic_us) {
	return json_pack("{s:s,s:I,s:I,s:I,s:I,s:I,s:I,s:I,s:I,s:I,s:I,s:I,s:I,s:I,s:I}",
		"type", "transport-metrics", "monotonic_us", (json_int_t)monotonic_us,
		"rtt_us", (json_int_t)metrics->smoothed_rtt_us,
		"min_rtt_us", (json_int_t)metrics->min_rtt_us,
		"cwnd_bytes", (json_int_t)metrics->congestion_window_bytes,
		"bytes_in_flight", (json_int_t)metrics->bytes_in_flight,
		"queued_stream_bytes", (json_int_t)metrics->queued_stream_bytes,
		"pacing_Bps", (json_int_t)metrics->pacing_rate_bytes_per_second,
		"ect0_packets", (json_int_t)metrics->ect0_packets,
		"ect1_packets", (json_int_t)metrics->ect1_packets,
		"ce_packets", (json_int_t)metrics->ce_packets,
		"alpha_numerator", (json_int_t)metrics->prague_alpha_numerator,
		"alpha_denominator", (json_int_t)metrics->prague_alpha_denominator,
		"data_sent_bytes", (json_int_t)metrics->data_sent_bytes,
		"data_received_bytes", (json_int_t)metrics->data_received_bytes);
}

static int parse_mode(const char *mode, imquic_congestion_controller *cc, imquic_ecn_mode *ecn) {
	if(!strcmp(mode, "prague")) {
		*cc = IMQUIC_CONGESTION_PRAGUE;
		*ecn = IMQUIC_ECN_ECT1;
		return 0;
	}
	if(!strcmp(mode, "reno")) {
		*cc = IMQUIC_CONGESTION_RENO;
		*ecn = IMQUIC_ECN_NOT_ECT;
		return 0;
	}
	return -1;
}

static void library_log(int level, const char *format, ...) {
	(void)level;
	va_list arguments;
	va_start(arguments, format);
	vfprintf(stderr, format, arguments);
	va_end(arguments);
}

static void fetch_destroy(fetch_state *fetch) {
	if(fetch == NULL)
		return;
	g_free(fetch->tile_id);
	g_free(fetch->track);
	if(fetch->objects != NULL)
		g_hash_table_unref(fetch->objects);
	g_free(fetch);
}

static void write_frame(json_t *header, const uint8_t *payload, size_t payload_len) {
	char *encoded = json_dumps(header, JSON_COMPACT | JSON_ENSURE_ASCII);
	if(encoded == NULL)
		return;
	uint32_t header_len = (uint32_t)strlen(encoded);
	uint32_t network_len = htonl(header_len);
	g_mutex_lock(&output_mutex);
	if(fwrite(&network_len, sizeof(network_len), 1, stdout) == 1 &&
			fwrite(encoded, 1, header_len, stdout) == header_len && payload_len > 0)
		fwrite(payload, 1, payload_len, stdout);
	fflush(stdout);
	g_mutex_unlock(&output_mutex);
	free(encoded);
}

static void emit_message(json_t *message) {
	json_t *header = json_pack("{s:s,s:o}", "wire", "control", "message", message);
	write_frame(header, NULL, 0);
	json_decref(header);
}

static void emit_status(fetch_state *fetch, const char *state, const char *detail) {
	json_t *message = json_pack("{s:s,s:I,s:s,s:i,s:s}",
		"type", "fetch-status", "request_id", (json_int_t)fetch->request_id,
		"tile_id", fetch->tile_id, "refinement", fetch->refinement, "state", state);
	if(detail != NULL)
		json_object_set_new(message, "detail", json_string(detail));
	emit_message(message);
}

static fetch_state *lookup_fetch(uint64_t request_id) {
	return g_hash_table_lookup(fetches, &request_id);
}

static void send_next_update(fetch_state *fetch) {
	if(connection == NULL || fetch == NULL || fetch->complete || fetch->update_pending ||
			fetch->desired_priority == fetch->applied_priority)
		return;
	imquic_moq_request_parameters parameters;
	imquic_moq_request_parameters_init_defaults(&parameters);
	parameters.subscriber_priority_set = TRUE;
	parameters.subscriber_priority = fetch->desired_priority;
	uint64_t update_id = imquic_moq_get_next_request_id(connection);
	if(imquic_moq_update_request(connection, update_id, fetch->request_id, &parameters) < 0) {
		emit_message(priority_event_message(fetch, "priority-failed", fetch->applied_priority,
			fetch->desired_priority, fetch->desired_epoch, update_id));
		emit_status(fetch, "failed", "REQUEST_UPDATE send failed");
		return;
	}
	fetch->update_id = update_id;
	fetch->pending_priority = fetch->desired_priority;
	fetch->pending_epoch = fetch->desired_epoch;
	fetch->update_pending = TRUE;
	g_hash_table_insert(updates, duplicate_bytes(&update_id, sizeof(update_id)), fetch);
	emit_message(priority_event_message(fetch, "priority-sent", fetch->applied_priority,
		fetch->pending_priority, fetch->pending_epoch, update_id));
	emit_status(fetch, "updating", NULL);
}

static void ready(imquic_connection *conn) {
	g_mutex_lock(&state_mutex);
	connection = conn;
	json_t *message = json_pack("{s:s}", "type", "ready");
	emit_message(message);
	g_mutex_unlock(&state_mutex);
}

static void failed(void *user_data) {
	(void)user_data;
	json_t *message = json_pack("{s:s,s:s}", "type", "error", "message", "MOQT connection failed");
	emit_message(message);
}

static void gone(imquic_connection *conn, uint64_t error_code, const char *reason) {
	(void)conn;
	g_mutex_lock(&state_mutex);
	connection = NULL;
	json_t *message = json_pack("{s:s,s:s,s:I}", "type", "error", "message",
		reason != NULL ? reason : "MOQT connection closed", "code", (json_int_t)error_code);
	emit_message(message);
	g_mutex_unlock(&state_mutex);
}

static void fetch_accepted(imquic_connection *conn, uint64_t request_id,
		imquic_moq_location *largest, imquic_moq_request_parameters *parameters, GList *properties) {
	(void)conn; (void)largest; (void)parameters; (void)properties;
	g_mutex_lock(&state_mutex);
	fetch_state *fetch = lookup_fetch(request_id);
	if(fetch != NULL)
		emit_status(fetch, "accepted", NULL);
	g_mutex_unlock(&state_mutex);
}

static void fetch_error(imquic_connection *conn, uint64_t request_id,
		imquic_moq_request_error_code error_code, const char *reason, uint64_t retry_interval,
		imquic_moq_redirect *redirect) {
	(void)conn; (void)error_code; (void)retry_interval; (void)redirect;
	g_mutex_lock(&state_mutex);
	fetch_state *fetch = lookup_fetch(request_id);
	if(fetch != NULL)
		emit_status(fetch, "failed", reason);
	g_mutex_unlock(&state_mutex);
}

static void update_accepted(imquic_connection *conn, uint64_t request_id,
		imquic_moq_request_parameters *parameters) {
	(void)conn; (void)parameters;
	g_mutex_lock(&state_mutex);
	fetch_state *fetch = g_hash_table_lookup(updates, &request_id);
	if(fetch != NULL) {
		g_hash_table_remove(updates, &request_id);
		fetch->update_pending = FALSE;
		uint8_t old_priority = fetch->applied_priority;
		fetch->applied_priority = fetch->pending_priority;
		fetch->applied_epoch = fetch->pending_epoch;
		emit_message(priority_event_message(fetch, "priority-accepted", old_priority,
			fetch->applied_priority, fetch->applied_epoch, request_id));
		emit_status(fetch, "accepted", NULL);
		send_next_update(fetch);
	}
	g_mutex_unlock(&state_mutex);
}

static void update_error(imquic_connection *conn, uint64_t request_id,
		imquic_moq_request_error_code error_code, const char *reason, uint64_t retry_interval,
		imquic_moq_redirect *redirect) {
	(void)conn; (void)error_code; (void)retry_interval; (void)redirect;
	g_mutex_lock(&state_mutex);
	fetch_state *fetch = g_hash_table_lookup(updates, &request_id);
	if(fetch != NULL) {
		g_hash_table_remove(updates, &request_id);
		fetch->update_pending = FALSE;
		emit_message(priority_event_message(fetch, "priority-failed", fetch->applied_priority,
			fetch->pending_priority, fetch->pending_epoch, request_id));
		emit_status(fetch, "failed", reason);
		/* A later browser epoch may retry; the FETCH and its cursor survive. */
	}
	g_mutex_unlock(&state_mutex);
}

static void incoming_object(imquic_connection *conn, imquic_moq_object *object) {
	(void)conn;
	g_mutex_lock(&state_mutex);
	fetch_state *fetch = lookup_fetch(object->request_id);
	if(fetch == NULL || fetch->complete) {
		g_mutex_unlock(&state_mutex);
		return;
	}
	if(g_hash_table_contains(fetch->objects, &object->object_id)) {
		g_mutex_unlock(&state_mutex);
		return;
	}
	g_hash_table_add(fetch->objects, duplicate_bytes(&object->object_id, sizeof(object->object_id)));
	json_t *header = json_pack("{s:s,s:i,s:I,s:I,s:i,s:i,s:i,s:I,s:I}",
		"tile_id", fetch->tile_id, "refinement", fetch->refinement,
		"object_id", (json_int_t)object->object_id, "request_id", (json_int_t)object->request_id,
		"publisher_priority", object->priority, "payload_bytes", (int)object->payload_len,
		"subscriber_priority", fetch->applied_priority,
		"subscriber_epoch", (json_int_t)fetch->applied_epoch,
		"monotonic_us", (json_int_t)g_get_monotonic_time());
	write_frame(header, object->payload, object->payload_len);
	json_decref(header);
	if(object->end_of_stream) {
		fetch->complete = TRUE;
		emit_status(fetch, "complete", NULL);
	}
	g_mutex_unlock(&state_mutex);
}

static gboolean track_parts(const char *scene, const char *full, imquic_moq_namespace *tns, imquic_moq_track *tn) {
	size_t scene_len = strlen(scene);
	if(strncmp(full, scene, scene_len) != 0 || full[scene_len] != '/')
		return FALSE;
	tns->buffer = (uint8_t *)scene;
	tns->length = scene_len;
	tns->next = NULL;
	tn->buffer = (uint8_t *)(full + scene_len + 1);
	tn->length = strlen(full + scene_len + 1);
	return tn->length > 0;
}

static void handle_open(json_t *command) {
	const char *scene = json_string_value(json_object_get(command, "scene"));
	json_t *items = json_object_get(command, "fetches");
	json_int_t epoch = json_integer_value(json_object_get(command, "epoch"));
	if(connection == NULL || scene == NULL || !json_is_array(items))
		return;
	size_t index;
	json_t *item;
	json_array_foreach(items, index, item) {
		const char *tile = json_string_value(json_object_get(item, "tile_id"));
		const char *track = json_string_value(json_object_get(item, "track"));
		json_int_t refinement = json_integer_value(json_object_get(item, "refinement"));
		json_int_t priority = json_integer_value(json_object_get(item, "priority"));
		if(tile == NULL || track == NULL || priority < 0 || priority > 255)
			continue;
		imquic_moq_namespace tns = { 0 };
		imquic_moq_track tn = { 0 };
		if(!track_parts(scene, track, &tns, &tn))
			continue;
		uint64_t request_id = imquic_moq_get_next_request_id(connection);
		fetch_state *fetch = g_malloc0(sizeof(fetch_state));
		fetch->request_id = request_id;
		fetch->tile_id = g_strdup(tile);
		fetch->track = g_strdup(track);
		fetch->refinement = (int)refinement;
		fetch->applied_priority = (uint8_t)priority;
		fetch->desired_priority = (uint8_t)priority;
		fetch->applied_epoch = (uint64_t)epoch;
		fetch->desired_epoch = (uint64_t)epoch;
		fetch->objects = g_hash_table_new_full(g_int64_hash, g_int64_equal, g_free, NULL);
		g_hash_table_insert(fetches, duplicate_bytes(&request_id, sizeof(request_id)), fetch);
		imquic_moq_location_range range = { .start = { 0, 0 }, .end = { 1, 0 } };
		imquic_moq_request_parameters parameters;
		imquic_moq_request_parameters_init_defaults(&parameters);
		parameters.group_order_set = TRUE;
		parameters.group_order = IMQUIC_MOQ_ORDERING_ASCENDING;
		parameters.subscriber_priority_set = TRUE;
		parameters.subscriber_priority = (uint8_t)priority;
		if(imquic_moq_standalone_fetch(connection, request_id, &tns, &tn, &range, &parameters) < 0) {
			emit_status(fetch, "failed", "FETCH send failed");
			continue;
		}
		json_t *opened = json_pack("{s:s,s:s,s:i,s:I,s:i,s:I,s:I}",
			"type", "fetch-opened", "tile_id", tile, "refinement", (int)refinement,
			"request_id", (json_int_t)request_id, "initial_priority", (int)priority,
			"epoch", epoch, "monotonic_us", (json_int_t)g_get_monotonic_time());
		emit_message(opened);
	}
}

static void handle_priorities(json_t *command) {
	json_t *items = json_object_get(command, "updates");
	json_int_t epoch = json_integer_value(json_object_get(command, "epoch"));
	if(!json_is_array(items))
		return;
	size_t index;
	json_t *item;
	json_array_foreach(items, index, item) {
		uint64_t request_id = (uint64_t)json_integer_value(json_object_get(item, "request_id"));
		json_int_t priority = json_integer_value(json_object_get(item, "priority"));
		fetch_state *fetch = lookup_fetch(request_id);
		if(fetch == NULL || priority < 0 || priority > 255) {
			json_t *message = json_pack("{s:s,s:s,s:I}", "type", "error", "message",
				"unknown request ID or invalid priority", "request_id", (json_int_t)request_id);
			emit_message(message);
			continue;
		}
		if(fetch->desired_priority == (uint8_t)priority)
			continue;
		if(fetch->update_pending)
			emit_message(priority_event_message(fetch, "priority-superseded",
				fetch->desired_priority, (uint8_t)priority, (uint64_t)epoch, fetch->update_id));
		fetch->desired_priority = (uint8_t)priority;
		fetch->desired_epoch = (uint64_t)epoch;
		send_next_update(fetch);
	}
}

static void handle_close(void) {
	GHashTableIter iterator;
	gpointer value;
	uint64_t unresolved = 0;
	g_hash_table_iter_init(&iterator, fetches);
	while(g_hash_table_iter_next(&iterator, NULL, &value)) {
		fetch_state *fetch = value;
		if(!fetch->update_pending)
			continue;
		json_t *message = priority_event_message(fetch, "priority-failed", fetch->applied_priority,
			fetch->pending_priority, fetch->pending_epoch, fetch->update_id);
		json_object_set_new(message, "detail", json_string("shutdown-unresolved"));
		emit_message(message);
		fetch->update_pending = FALSE;
		unresolved++;
	}
	g_hash_table_remove_all(updates);
	json_t *complete = json_pack("{s:s,s:I,s:I}", "type", "audit-complete",
		"unresolved_updates", (json_int_t)unresolved,
		"monotonic_us", (json_int_t)g_get_monotonic_time());
	emit_message(complete);
	stopping = 1;
}

static void handle_command(const char *line) {
	json_error_t error;
	json_t *command = json_loads(line, JSON_REJECT_DUPLICATES, &error);
	if(command == NULL || !json_is_object(command)) {
		json_t *message = json_pack("{s:s,s:s}", "type", "error", "message", "invalid bridge JSON");
		emit_message(message);
		json_decref(command);
		return;
	}
	const char *type = json_string_value(json_object_get(command, "type"));
	g_mutex_lock(&state_mutex);
	if(type != NULL && !strcmp(type, "open"))
		handle_open(command);
	else if(type != NULL && !strcmp(type, "priorities"))
		handle_priorities(command);
	else if(type != NULL && !strcmp(type, "close"))
		handle_close();
	g_mutex_unlock(&state_mutex);
	json_decref(command);
}

static void signal_handler(int signum) {
	(void)signum;
	stopping = 1;
}

static gpointer metrics_worker(gpointer user_data) {
	(void)user_data;
	while(!stopping) {
		imquic_transport_metrics metrics = { 0 };
		gboolean available = FALSE;
		g_mutex_lock(&state_mutex);
		if(connection != NULL && imquic_get_transport_metrics(connection, &metrics) == 0)
			available = TRUE;
		g_mutex_unlock(&state_mutex);
		if(available)
			emit_message(metrics_event_message(&metrics, g_get_monotonic_time()));
		g_usleep(100000);
	}
	return NULL;
}

int main(int argc, char **argv) {
	if(argc != 3 && argc != 4) {
		fprintf(stderr, "usage: %s RELAY_HOST RELAY_PORT [reno|prague]\n", argv[0]);
		return 2;
	}
	imquic_congestion_controller cc;
	imquic_ecn_mode ecn;
	if(parse_mode(argc == 4 ? argv[3] : "reno", &cc, &ecn) < 0) {
		fprintf(stderr, "invalid transport mode: %s\n", argv[3]);
		return 2;
	}
	signal(SIGINT, signal_handler);
	signal(SIGTERM, signal_handler);
	fetches = g_hash_table_new_full(g_int64_hash, g_int64_equal, g_free, (GDestroyNotify)fetch_destroy);
	updates = g_hash_table_new_full(g_int64_hash, g_int64_equal, g_free, NULL);
	imquic_set_log_function(library_log);
	imquic_set_log_level(IMQUIC_LOG_WARN);
	if(imquic_init(NULL) < 0)
		return 1;
	imquic_client *client = imquic_create_moq_client("3dgs-priority-sidecar",
		IMQUIC_CONFIG_INIT,
		IMQUIC_CONFIG_TLS_NO_VERIFY, TRUE,
		IMQUIC_CONFIG_REMOTE_HOST, argv[1],
		IMQUIC_CONFIG_REMOTE_PORT, atoi(argv[2]),
		IMQUIC_CONFIG_CONGESTION_CONTROL, cc,
		IMQUIC_CONFIG_ECN, ecn,
		IMQUIC_CONFIG_RAW_QUIC, TRUE,
		IMQUIC_CONFIG_WEBTRANSPORT, FALSE,
		IMQUIC_CONFIG_MOQ_VERSION, IMQUIC_MOQ_VERSION_19,
		IMQUIC_CONFIG_MAX_BIDI_STREAMS, 8192,
		IMQUIC_CONFIG_MAX_UNI_STREAMS, 8192,
		IMQUIC_CONFIG_MOQ_MAX_REQUEST_UPDATES, 4096,
		IMQUIC_CONFIG_DONE, NULL);
	if(client == NULL)
		return 1;
	imquic_set_moq_ready_cb(client, ready);
	imquic_set_connection_failed_cb(client, failed);
	imquic_set_moq_connection_gone_cb(client, gone);
	imquic_set_fetch_accepted_cb(client, fetch_accepted);
	imquic_set_fetch_error_cb(client, fetch_error);
	imquic_set_request_update_accepted_cb(client, update_accepted);
	imquic_set_request_update_error_cb(client, update_error);
	imquic_set_incoming_object_cb(client, incoming_object);
	imquic_start_endpoint(client);
	GThread *metrics_thread = g_thread_new("transport-metrics", metrics_worker, NULL);
	char *line = NULL;
	size_t capacity = 0;
	while(!stopping && getline(&line, &capacity, stdin) >= 0) {
		if(strlen(line) > 1024 * 1024) {
			json_t *message = json_pack("{s:s,s:s}", "type", "error", "message", "bridge command too large");
			emit_message(message);
			continue;
		}
		handle_command(line);
	}
	free(line);
	stopping = 1;
	g_thread_join(metrics_thread);
	imquic_shutdown_endpoint(client);
	imquic_deinit();
	g_hash_table_unref(updates);
	g_hash_table_unref(fetches);
	return 0;
}
