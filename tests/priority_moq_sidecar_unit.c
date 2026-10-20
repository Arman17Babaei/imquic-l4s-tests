#define _GNU_SOURCE
#include <assert.h>
#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

static ssize_t (*writer_hook)(int, const void *, size_t) = NULL;
static ssize_t sidecar_test_write(int fd, const void *bytes, size_t length);

#define main priority_moq_sidecar_program_main
#define write sidecar_test_write
#include "../tools/l4s/priority_moq_sidecar.c"
#undef write
#undef main

static ssize_t sidecar_test_write(int fd, const void *bytes, size_t length) {
	return writer_hook ? writer_hook(fd, bytes, length) : write(fd, bytes, length);
}

typedef struct blocked_output {
	GMutex mutex;
	GCond condition;
	gboolean entered, released;
	GByteArray *bytes;
} blocked_output;

static ssize_t blocked_write(void *data, const char *bytes, size_t length) {
	blocked_output *sink = data;
	g_mutex_lock(&sink->mutex);
	sink->entered = TRUE;
	g_cond_broadcast(&sink->condition);
	while(!sink->released) g_cond_wait(&sink->condition, &sink->mutex);
	g_byte_array_append(sink->bytes, (const guint8 *)bytes, length);
	g_mutex_unlock(&sink->mutex);
	return (ssize_t)length;
}

static blocked_output *blocked_sink;
static ssize_t blocked_fd_write(int fd, const void *bytes, size_t length) {
	(void)fd;
	return blocked_write(blocked_sink, bytes, length);
}

static volatile gint producer_done = 0;
static gpointer enqueue_second(gpointer unused) {
	(void)unused;
	uint8_t payload[96]; memset(payload, 0x5a, sizeof(payload));
	json_t *header = json_pack("{s:i}", "sequence", 2);
	write_frame(header, payload, sizeof(payload));
	json_decref(header);
	g_atomic_int_set(&producer_done, 1);
	return NULL;
}

static void bounded_output_test(void) {
	blocked_output sink = { .bytes = g_byte_array_new() };
	FILE *original = stdout;
	stdout = tmpfile();
	assert(stdout != NULL);
	blocked_sink = &sink; writer_hook = blocked_fd_write;
	output_limit_bytes = 160;
	output_start();
	uint8_t payload[96]; memset(payload, 0x5a, sizeof(payload));
	json_t *header = json_pack("{s:i}", "sequence", 1);
	write_frame(header, payload, sizeof(payload));
	json_decref(header);
	/* The QUIC-side enqueue returned even though the writer cannot progress. */
	g_mutex_lock(&sink.mutex);
	while(!sink.entered) g_cond_wait(&sink.condition, &sink.mutex);
	g_mutex_unlock(&sink.mutex);
	GThread *producer = g_thread_new("bounded-producer", enqueue_second, NULL);
	g_usleep(20000);
	assert(!g_atomic_int_get(&producer_done));
	g_mutex_lock(&output_mutex);
	assert(output_bytes <= output_limit_bytes && output_peak_bytes <= output_limit_bytes);
	g_mutex_unlock(&output_mutex);
	g_mutex_lock(&sink.mutex);
	sink.released = TRUE;
	g_cond_broadcast(&sink.condition);
	g_mutex_unlock(&sink.mutex);
	g_thread_join(producer);
	output_stop();
	writer_hook = NULL; blocked_sink = NULL;
	fclose(stdout); stdout = original;
	gsize offset = 0;
	for(int sequence = 1; sequence <= 2; sequence++) {
		uint32_t length; memcpy(&length, sink.bytes->data + offset, 4); length = ntohl(length); offset += 4;
		json_error_t error;
		json_t *decoded = json_loadb((char *)sink.bytes->data + offset, length, 0, &error);
		assert(decoded && json_integer_value(json_object_get(decoded, "sequence")) == sequence);
		json_decref(decoded); offset += length;
		assert(memcmp(sink.bytes->data + offset, payload, sizeof(payload)) == 0); offset += sizeof(payload);
	}
	assert(offset == sink.bytes->len && output_bytes == 0);
	g_byte_array_unref(sink.bytes);
	g_mutex_clear(&sink.mutex); g_cond_clear(&sink.condition);
	output_limit_bytes = 4 * 1024 * 1024;
}

static GByteArray *fault_bytes;
static int fault_calls, fault_errno;
static ssize_t fault_write(int fd, const void *bytes, size_t length) {
	(void)fd;
	fault_calls++;
	if(fault_errno) { errno = fault_errno; return -1; }
	if(fault_calls == 2) { errno = EINTR; return -1; }
	if(fault_calls == 3) { errno = EAGAIN; return -1; }
	size_t accepted = MIN(length, 7);
	g_byte_array_append(fault_bytes, bytes, accepted);
	return accepted;
}

static void transient_output_test(void) {
	FILE *original = stdout;
	stdout = tmpfile(); assert(stdout);
	fault_bytes = g_byte_array_new(); fault_calls = fault_errno = 0;
	writer_hook = fault_write; stopping = 0;
	output_start();
	json_t *audit = json_pack("{s:s}", "type", "audit-complete");
	emit_message(audit);
	output_stop();
	assert(!output_failed);
	uint32_t length; assert(fault_bytes->len >= 4);
	memcpy(&length, fault_bytes->data, 4); length = ntohl(length);
	assert(fault_bytes->len == length + 4);
	json_t *frame = json_loadb((char *)fault_bytes->data + 4, length, 0, NULL);
	assert(frame && !strcmp(json_string_value(json_object_get(
		json_object_get(frame, "message"), "type")), "audit-complete"));
	json_decref(frame);
	writer_hook = NULL;
	g_byte_array_unref(fault_bytes);
	fclose(stdout); stdout = original;
}

static void fatal_output_test(void) {
	FILE *original = stdout, *original_error = stderr;
	stdout = tmpfile(); stderr = tmpfile(); assert(stdout && stderr);
	fault_calls = 0; fault_errno = EPIPE; writer_hook = fault_write; stopping = 0;
	output_start();
	emit_message(json_pack("{s:s}", "type", "audit-complete"));
	output_stop();
	assert(output_failed && fault_calls == 1);
	fflush(stderr); rewind(stderr);
	char diagnostic[1024] = { 0 };
	assert(fread(diagnostic, 1, sizeof(diagnostic) - 1, stderr) > 0);
	assert(strstr(diagnostic, "native output write failed") && strstr(diagnostic, "errno=32"));
	assert(strstr(diagnostic, "written=0") && strstr(diagnostic, "fd_flags="));
	writer_hook = NULL; fault_errno = 0; stopping = 0;
	fclose(stdout); fclose(stderr); stdout = original; stderr = original_error;
}

int main(void) {
	bounded_output_test();
	transient_output_test();
	fatal_output_test();
	imquic_congestion_controller cc = IMQUIC_CONGESTION_DEFAULT;
	imquic_ecn_mode ecn = IMQUIC_ECN_DEFAULT;
	assert(parse_mode("prague", &cc, &ecn) == 0);
	assert(cc == IMQUIC_CONGESTION_PRAGUE && ecn == IMQUIC_ECN_ECT1);
	assert(parse_mode("reno", &cc, &ecn) == 0);
	assert(cc == IMQUIC_CONGESTION_RENO && ecn == IMQUIC_ECN_NOT_ECT);
	assert(parse_mode("cubic", &cc, &ecn) < 0);
	fetch_state fetch = { .request_id = 7, .update_id = 8, .tile_id = "tile",
		.refinement = 2, .applied_priority = 48, .desired_priority = 28,
		.applied_epoch = 3, .desired_epoch = 4 };
	json_t *message = priority_event_message(&fetch, "priority-accepted", 48, 28, 4, 8);
	assert(json_integer_value(json_object_get(message, "request_id")) == 7);
	assert(json_integer_value(json_object_get(message, "old_priority")) == 48);
	assert(json_integer_value(json_object_get(message, "new_priority")) == 28);
	assert(json_integer_value(json_object_get(message, "epoch")) == 4);
	assert(json_integer_value(json_object_get(message, "monotonic_us")) > 0);
	json_decref(message);
	/* Rejecting a priority update must leave the FETCH usable. */
	updates = g_hash_table_new(g_int64_hash, g_int64_equal);
	fetch.update_pending = TRUE;
	g_hash_table_insert(updates, &fetch.update_id, &fetch);
	FILE *capture = tmpfile();
	assert(capture != NULL);
	int saved_stdout = dup(STDOUT_FILENO);
	assert(saved_stdout >= 0 && dup2(fileno(capture), STDOUT_FILENO) >= 0);
	update_error(NULL, fetch.update_id, 0, "completed update", 0, NULL);
	fflush(stdout);
	assert(dup2(saved_stdout, STDOUT_FILENO) >= 0);
	close(saved_stdout);
	assert(!fetch.update_pending && !fetch.complete);
	assert(g_hash_table_size(updates) == 0);
	rewind(capture);
	for(int index = 0; index < 2; index++) {
		uint32_t length;
		assert(fread(&length, sizeof(length), 1, capture) == 1);
		length = ntohl(length);
		char *text = g_malloc(length + 1);
		assert(fread(text, 1, length, capture) == length);
		text[length] = '\0';
		json_t *frame = json_loads(text, 0, NULL);
		assert(frame != NULL);
		json_t *event = json_object_get(frame, "message");
		assert(!strcmp(json_string_value(json_object_get(event, index ? "state" : "type")), "priority-failed"));
		json_decref(frame);
		g_free(text);
	}
	fclose(capture);
	g_hash_table_destroy(updates);
	imquic_transport_metrics metrics = { .smoothed_rtt_us = 80000,
		.min_rtt_us = 79000, .congestion_window_bytes = 12345,
		.bytes_in_flight = 6789, .queued_stream_bytes = 456,
		.prague_pacing_interval_us = 1000, .pacing_quantum_bytes = 1440,
		.packets_sent = 4294967300ULL, .packets_lost = 17, .spurious_losses = 12,
		.timer_losses = 3, .max_reorder_delay_us = 5000, .max_reorder_gap = 9 };
	message = metrics_event_message(&metrics, 9000);
	assert(json_integer_value(json_object_get(message, "cwnd_bytes")) == 12345);
	assert(json_integer_value(json_object_get(message, "prague_pacing_interval_us")) == 1000);
	assert(json_integer_value(json_object_get(message, "pacing_quantum_bytes")) == 1440);
	assert(json_integer_value(json_object_get(message, "queued_stream_bytes")) == 456);
	assert(json_integer_value(json_object_get(message, "monotonic_us")) == 9000);
	assert(json_integer_value(json_object_get(message, "packets_sent")) == 4294967300LL);
	assert(json_integer_value(json_object_get(message, "packets_lost")) == 17);
	assert(json_integer_value(json_object_get(message, "spurious_losses")) == 12);
	assert(json_integer_value(json_object_get(message, "timer_losses")) == 3);
	assert(json_integer_value(json_object_get(message, "max_reorder_delay_us")) == 5000);
	assert(json_integer_value(json_object_get(message, "max_reorder_gap")) == 9);
	json_decref(message);
	return 0;
}
