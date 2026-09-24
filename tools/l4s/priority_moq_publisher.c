/* Manifest-v2 publisher for the priority-aware 3DGS MOQT loopback MVP. */
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

typedef struct published_track {
	uint64_t request_id;
	uint64_t alias;
	char *name;
	char *file;
	json_t *objects;
} published_track;

static volatile sig_atomic_t stopping = 0;
static GHashTable *tracks = NULL;
static GPtrArray *catalog = NULL;
static char *scene = NULL;
static char *root = NULL;

static void library_log(int level, const char *format, ...) {
	(void)level;
	va_list arguments;
	va_start(arguments, format);
	vfprintf(stderr, format, arguments);
	va_end(arguments);
}

static void track_destroy(published_track *track) {
	if(track == NULL) return;
	g_free(track->name);
	g_free(track->file);
	json_decref(track->objects);
	g_free(track);
}

static void signal_handler(int signum) { (void)signum; stopping = 1; }

static void publish_ready(imquic_connection *conn) {
	for(guint index = 0; index < catalog->len; index++) {
		published_track *track = g_ptr_array_index(catalog, index);
		imquic_moq_namespace tns = { .buffer = (uint8_t *)scene, .length = strlen(scene) };
		imquic_moq_track tn = { .buffer = (uint8_t *)track->name, .length = strlen(track->name) };
		imquic_moq_request_parameters parameters;
		imquic_moq_request_parameters_init_defaults(&parameters);
		parameters.group_order_set = TRUE;
		parameters.group_order = IMQUIC_MOQ_ORDERING_ASCENDING;
		parameters.forward_set = TRUE;
		parameters.forward = TRUE;
		track->request_id = imquic_moq_get_next_request_id(conn);
		g_hash_table_insert(tracks, g_memdup2(&track->request_id, sizeof(track->request_id)), track);
		imquic_moq_publish(conn, track->request_id, &tns, &tn, track->alias, &parameters, NULL);
	}
}

static void publish_accepted(imquic_connection *conn, uint64_t request_id, imquic_moq_request_parameters *parameters) {
	(void)parameters;
	published_track *track = g_hash_table_lookup(tracks, &request_id);
	if(track == NULL) return;
	char *path = g_build_filename(root, track->file, NULL);
	char *contents = NULL;
	gsize length = 0;
	GError *error = NULL;
	if(!g_file_get_contents(path, &contents, &length, &error)) {
		fprintf(stderr, "publisher: %s\n", error->message);
		g_clear_error(&error); g_free(path); stopping = 1; return;
	}
	g_free(path);
	size_t count = json_array_size(track->objects);
	for(size_t index = 0; index < count; index++) {
		json_t *description = json_array_get(track->objects, index);
		size_t offset = (size_t)json_integer_value(json_object_get(description, "offset"));
		size_t bytes = (size_t)json_integer_value(json_object_get(description, "bytes"));
		int priority = (int)json_integer_value(json_object_get(description, "publisher_priority"));
		if(offset > length || bytes > length - offset) { stopping = 1; break; }
		imquic_moq_object object = {
			.request_id = request_id, .track_alias = track->alias,
			.group_id = 0, .subgroup_id = 0, .object_id = index,
			.priority = (uint8_t)priority, .payload = (uint8_t *)contents + offset,
			.payload_len = bytes, .first_of_subgroup = index == 0,
			.delivery = IMQUIC_MOQ_USE_SUBGROUP, .end_of_stream = index + 1 == count
		};
		if(imquic_moq_send_object(conn, &object) < 0) { stopping = 1; break; }
	}
	g_free(contents);
}

static void publish_error(imquic_connection *conn, uint64_t request_id,
		imquic_moq_request_error_code code, const char *reason, uint64_t retry, imquic_moq_redirect *redirect) {
	(void)conn; (void)request_id; (void)code; (void)retry; (void)redirect;
	fprintf(stderr, "publisher: PUBLISH rejected: %s\n", reason != NULL ? reason : "unknown error");
	stopping = 1;
}

static int load_manifest(const char *manifest_path) {
	json_error_t error;
	json_t *manifest = json_load_file(manifest_path, JSON_REJECT_DUPLICATES, &error);
	if(manifest == NULL) { fprintf(stderr, "publisher: %s\n", error.text); return -1; }
	json_t *scene_json = json_object_get(manifest, "scene");
	scene = g_strdup(json_string_value(json_object_get(scene_json, "id")));
	root = g_path_get_dirname(manifest_path);
	tracks = g_hash_table_new_full(g_int64_hash, g_int64_equal, g_free, NULL);
	catalog = g_ptr_array_new_with_free_func((GDestroyNotify)track_destroy);
	json_t *tiles = json_object_get(manifest, "tiles");
	size_t tile_index;
	json_t *tile;
	uint64_t alias = 1;
	json_array_foreach(tiles, tile_index, tile) {
		json_t *refinements = json_object_get(tile, "refinements");
		size_t refinement_index;
		json_t *refinement;
		json_array_foreach(refinements, refinement_index, refinement) {
			json_t *objects = json_object_get(refinement, "objects");
			if(!json_is_array(objects) || json_array_size(objects) == 0) continue;
			const char *full = json_string_value(json_object_get(refinement, "track"));
			const char *file = json_string_value(json_object_get(refinement, "file"));
			if(full == NULL || file == NULL || strncmp(full, scene, strlen(scene)) || full[strlen(scene)] != '/') continue;
			published_track *track = g_malloc0(sizeof(published_track));
			track->alias = alias++;
			track->name = g_strdup(full + strlen(scene) + 1);
			track->file = g_strdup(file);
			track->objects = json_incref(objects);
			g_ptr_array_add(catalog, track);
		}
	}
	json_decref(manifest);
	return scene != NULL && catalog->len > 0 ? 0 : -1;
}

int main(int argc, char **argv) {
	if(argc != 4) { fprintf(stderr, "usage: %s RELAY_HOST RELAY_PORT MANIFEST\n", argv[0]); return 2; }
	if(load_manifest(argv[3]) < 0) return 1;
	signal(SIGINT, signal_handler); signal(SIGTERM, signal_handler);
	imquic_set_log_function(library_log);
	imquic_set_log_level(IMQUIC_LOG_WARN);
	if(imquic_init(NULL) < 0) return 1;
	imquic_client *client = imquic_create_moq_client("3dgs-priority-publisher",
		IMQUIC_CONFIG_INIT, IMQUIC_CONFIG_TLS_NO_VERIFY, TRUE,
		IMQUIC_CONFIG_REMOTE_HOST, argv[1], IMQUIC_CONFIG_REMOTE_PORT, atoi(argv[2]),
		IMQUIC_CONFIG_RAW_QUIC, TRUE, IMQUIC_CONFIG_WEBTRANSPORT, FALSE,
		IMQUIC_CONFIG_MOQ_VERSION, IMQUIC_MOQ_VERSION_19,
		IMQUIC_CONFIG_MAX_BIDI_STREAMS, 8192, IMQUIC_CONFIG_MAX_UNI_STREAMS, 8192,
		IMQUIC_CONFIG_MOQ_MAX_REQUEST_UPDATES, 4096, IMQUIC_CONFIG_DONE, NULL);
	if(client == NULL) return 1;
	imquic_set_moq_ready_cb(client, publish_ready);
	imquic_set_publish_accepted_cb(client, publish_accepted);
	imquic_set_publish_error_cb(client, publish_error);
	imquic_start_endpoint(client);
	while(!stopping) g_usleep(100000);
	imquic_shutdown_endpoint(client); imquic_deinit();
	g_hash_table_unref(tracks); g_ptr_array_unref(catalog); g_free(scene); g_free(root);
	return 0;
}
