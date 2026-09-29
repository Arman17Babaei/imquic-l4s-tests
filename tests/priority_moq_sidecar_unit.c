#define _GNU_SOURCE
#include <assert.h>
#include <string.h>

#define main priority_moq_sidecar_program_main
#include "../tools/l4s/priority_moq_sidecar.c"
#undef main

int main(void) {
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
	imquic_transport_metrics metrics = { .smoothed_rtt_us = 80000,
		.min_rtt_us = 79000, .congestion_window_bytes = 12345,
		.bytes_in_flight = 6789, .queued_stream_bytes = 456 };
	message = metrics_event_message(&metrics, 9000);
	assert(json_integer_value(json_object_get(message, "cwnd_bytes")) == 12345);
	assert(json_integer_value(json_object_get(message, "queued_stream_bytes")) == 456);
	assert(json_integer_value(json_object_get(message, "monotonic_us")) == 9000);
	json_decref(message);
	return 0;
}
