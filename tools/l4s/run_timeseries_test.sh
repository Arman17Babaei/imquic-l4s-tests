#!/usr/bin/env bash
set -Eeuo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
RESULT_DIR=${1:-"$ROOT/results/l4s/timeseries-$(date -u +%Y%m%dT%H%M%SZ)"}
RATE=${IMQUIC_L4S_RATE:-2mbit}
TARGET=${IMQUIC_L4S_TARGET:-1ms}
UPDATE=${IMQUIC_L4S_TUPDATE:-1ms}
SENDER=imq-l4s-sender
ROUTER=imq-l4s-router
RECEIVER=imq-l4s-receiver
CAPTURE_PIDS=()
SERVER_PID=

cleanup() {
	set +e
	[[ -n "$SERVER_PID" ]] && kill "$SERVER_PID" 2>/dev/null
	((${#CAPTURE_PIDS[@]})) && kill "${CAPTURE_PIDS[@]}" 2>/dev/null
	for ns in "$SENDER" "$ROUTER" "$RECEIVER"; do
		ip netns del "$ns" 2>/dev/null
	done
}
trap cleanup EXIT INT TERM

if [[ $EUID -ne 0 ]]; then
	echo "run as root: sudo make l4s-timeseries-check" >&2
	exit 77
fi
for command in ip tc tcpdump tshark python3 modprobe; do
	command -v "$command" >/dev/null || { echo "missing command: $command" >&2; exit 77; }
done
modprobe sch_dualpi2
modinfo sch_dualpi2 >/dev/null

mkdir -p "$RESULT_DIR"
make -C "$ROOT/src" imquic-l4s-test

for ns in "$SENDER" "$ROUTER" "$RECEIVER"; do
	ip netns del "$ns" 2>/dev/null || true
	ip netns add "$ns"
	ip -n "$ns" link set lo up
done
ip link add imq-l4s-s0 type veth peer name imq-l4s-rs
ip link add imq-l4s-d0 type veth peer name imq-l4s-rd
ip link set imq-l4s-s0 netns "$SENDER"
ip link set imq-l4s-rs netns "$ROUTER"
ip link set imq-l4s-d0 netns "$RECEIVER"
ip link set imq-l4s-rd netns "$ROUTER"
ip -n "$SENDER" addr add 10.10.1.2/24 dev imq-l4s-s0
ip -n "$ROUTER" addr add 10.10.1.1/24 dev imq-l4s-rs
ip -n "$RECEIVER" addr add 10.10.2.2/24 dev imq-l4s-d0
ip -n "$ROUTER" addr add 10.10.2.1/24 dev imq-l4s-rd
ip -n "$SENDER" link set imq-l4s-s0 up
ip -n "$ROUTER" link set imq-l4s-rs up
ip -n "$RECEIVER" link set imq-l4s-d0 up
ip -n "$ROUTER" link set imq-l4s-rd up
ip -n "$SENDER" route add default via 10.10.1.1
ip -n "$RECEIVER" route add default via 10.10.2.1
ip netns exec "$ROUTER" sysctl -qw net.ipv4.ip_forward=1

for device in imq-l4s-rs imq-l4s-rd; do
	ip netns exec "$ROUTER" tc qdisc add dev "$device" root handle 1: htb default 1
	ip netns exec "$ROUTER" tc class add dev "$device" parent 1: \
		classid 1:1 htb rate "$RATE" burst 16k
	ip netns exec "$ROUTER" tc qdisc add dev "$device" parent 1:1 \
		handle 10: dualpi2 target "$TARGET" tupdate "$UPDATE"
done

ip netns exec "$ROUTER" tcpdump -U -i imq-l4s-rs -w \
	"$RESULT_DIR/router-sender.pcap" udp port 4443 \
	>"$RESULT_DIR/tcpdump-sender.log" 2>&1 &
CAPTURE_PIDS+=("$!")
ip netns exec "$ROUTER" tcpdump -U -i imq-l4s-rd -w \
	"$RESULT_DIR/router-receiver.pcap" udp port 4443 \
	>"$RESULT_DIR/tcpdump-receiver.log" 2>&1 &
CAPTURE_PIDS+=("$!")

ip netns exec "$RECEIVER" bash -c \
	"cd '$ROOT/src' && ./imquic-l4s-test --server 10.10.2.2 4443" \
	>"$RESULT_DIR/server.log" 2>&1 &
SERVER_PID=$!
sleep 1

set +e
ip netns exec "$SENDER" bash -c \
	"cd '$ROOT/src' && ./imquic-l4s-test --client 10.10.2.2 4443 '$RESULT_DIR/metrics.csv'" \
	>"$RESULT_DIR/client.log" 2>&1
CLIENT_STATUS=$?
wait "$SERVER_PID"
SERVER_STATUS=$?
SERVER_PID=
set -e
kill "${CAPTURE_PIDS[@]}" 2>/dev/null || true
wait "${CAPTURE_PIDS[@]}" 2>/dev/null || true
CAPTURE_PIDS=()

for device in imq-l4s-rs imq-l4s-rd; do
	{
		echo "device=$device"
		ip netns exec "$ROUTER" tc -s qdisc show dev "$device"
	} >>"$RESULT_DIR/dualpi2-stats.txt"
done

[[ $CLIENT_STATUS -eq 0 ]] || { cat "$RESULT_DIR/client.log" >&2; exit 1; }
[[ $SERVER_STATUS -eq 0 ]] || { cat "$RESULT_DIR/server.log" >&2; exit 1; }
python3 "$ROOT/tools/l4s/analyze_timeseries.py" "$RESULT_DIR/metrics.csv" \
	--json-out "$RESULT_DIR/analysis.json"

ECT1=$(tshark -r "$RESULT_DIR/router-sender.pcap" -Y 'ip.dsfield.ecn == 1' \
	-T fields -e frame.number 2>/dev/null | wc -l)
CE=$(tshark -r "$RESULT_DIR/router-receiver.pcap" -Y 'ip.dsfield.ecn == 3' \
	-T fields -e frame.number 2>/dev/null | wc -l)
[[ $ECT1 -gt 0 ]] || { echo "no ECT(1) packets captured" >&2; exit 1; }
[[ $CE -gt 0 ]] || { echo "no CE packets captured" >&2; exit 1; }
printf 'ECT(1) packets=%s\nCE packets=%s\n' "$ECT1" "$CE" >"$RESULT_DIR/packet-counts.txt"
echo "IMQUIC Prague time-series test: PASS ($RESULT_DIR)"
