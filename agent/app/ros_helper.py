"""ROS 2 probe process. Started by the panel with ROS sourced:
    bash -c 'source /opt/ros/<distro>/setup.bash && exec python3 ros_helper.py'
Protocol: one JSON request per line on stdin {"id", "cmd", "args"},
one JSON reply per line on stdout {"id", "ok", "result" | "error"}.
Standalone on purpose: must not import anything from the panel.
"""
import json
import os
import sys
import threading
import time
from collections import deque

import rclpy
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from rosidl_runtime_py.convert import message_to_ordereddict
from rosidl_runtime_py.utilities import get_message

NODE_NAME = "drone_panel_probe"
WINDOW = 5.0  # seconds of samples for rate / bandwidth

out_lock = threading.Lock()


def send(obj):
    line = json.dumps(obj, default=str)
    with out_lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


class Probe(Node):
    def __init__(self):
        super().__init__(NODE_NAME)
        self.lock = threading.Lock()
        self.subs = {}      # topic -> subscription
        self.samples = {}   # topic -> deque[(t, size)]
        self.expiry = {}    # topic -> monotonic time to drop (None = keep)
        self.types = {}

    # ------------------------------------------------------------ graph
    def graph(self):
        me = "/" + NODE_NAME
        nodes = []
        for name, ns in self.get_node_names_and_namespaces():
            full = (ns.rstrip("/") + "/" + name) if ns != "/" else "/" + name
            if full != me:
                nodes.append({"name": name, "ns": ns, "full": full})
        topics = []
        for t, types in self.get_topic_names_and_types():
            def ends(infos):
                res = []
                for i in infos:
                    full = (i.node_namespace.rstrip("/") + "/" + i.node_name) if i.node_namespace != "/" else "/" + i.node_name
                    if full == me:
                        continue
                    q = i.qos_profile
                    res.append({"node": full,
                                "reliability": getattr(q.reliability, "name", str(q.reliability)),
                                "durability": getattr(q.durability, "name", str(q.durability))})
                return res
            pubs = ends(self.get_publishers_info_by_topic(t))
            subs = ends(self.get_subscriptions_info_by_topic(t))
            topics.append({"name": t, "types": types, "pubs": pubs, "subs": subs})
        nodes.sort(key=lambda n: n["full"])
        topics.sort(key=lambda t: t["name"])
        return {"nodes": nodes, "topics": topics}

    # ------------------------------------------------------------ rates
    def _type_of(self, topic):
        for t, types in self.get_topic_names_and_types():
            if t == topic and types:
                return types[0]
        return None

    def _ensure_sub(self, topic):
        if topic in self.subs:
            return True
        type_name = self._type_of(topic)
        if not type_name:
            return False
        msg_type = get_message(type_name)
        self.samples[topic] = deque(maxlen=5000)

        def cb(raw, topic=topic):
            with self.lock:
                self.samples[topic].append((time.monotonic(), len(raw)))

        self.subs[topic] = self.create_subscription(msg_type, topic, cb, qos_profile_sensor_data, raw=True)
        self.types[topic] = type_name
        return True

    def _drop_sub(self, topic):
        sub = self.subs.pop(topic, None)
        if sub is not None:
            self.destroy_subscription(sub)
        self.samples.pop(topic, None)
        self.expiry.pop(topic, None)

    def watch(self, topics, ttl=None):
        """Subscribe to topics. ttl=None: keep until unwatched (UI);
        ttl=N: temporary (preflight). Topics not listed and with no ttl are dropped
        when replace=True."""
        missing = []
        for t in topics:
            if not self._ensure_sub(t):
                missing.append(t)
                continue
            if ttl is None:
                self.expiry[t] = None
            elif self.expiry.get(t, 0) is not None:
                self.expiry[t] = max(self.expiry.get(t) or 0, time.monotonic() + ttl)
        return missing

    def set_watch(self, topics):
        topics = set(topics)
        for t in list(self.subs):
            if self.expiry.get(t) is None and t not in topics:
                self._drop_sub(t)
        return self.watch(sorted(topics))

    def gc(self):
        now = time.monotonic()
        for t, exp in list(self.expiry.items()):
            if exp is not None and exp < now:
                self._drop_sub(t)

    def rates(self, topics=None):
        now = time.monotonic()
        res = {}
        with self.lock:
            for t, dq in self.samples.items():
                if topics and t not in topics:
                    continue
                while dq and dq[0][0] < now - WINDOW:
                    dq.popleft()
                n = len(dq)
                hz = (n - 1) / (dq[-1][0] - dq[0][0]) if n >= 2 and dq[-1][0] > dq[0][0] else 0.0
                bw = sum(s for _, s in dq) / WINDOW if n else 0.0
                res[t] = {"hz": round(hz, 2), "bw": round(bw), "count": n,
                          "last_age": round(now - dq[-1][0], 2) if n else None, "type": self.types.get(t)}
        return res

    # ------------------------------------------------------------ echo
    def echo(self, topic, timeout=3.0):
        type_name = self._type_of(topic)
        if not type_name:
            raise ValueError(f"topic not found: {topic}")
        msg_type = get_message(type_name)
        got = threading.Event()
        box = {}

        def cb(msg):
            if not got.is_set():
                box["msg"] = msg
                got.set()

        sub = self.create_subscription(msg_type, topic, cb, qos_profile_sensor_data)
        try:
            if not got.wait(timeout):
                return {"type": type_name, "message": None, "note": f"no message within {timeout:.0f}s"}
            d = message_to_ordereddict(box["msg"], truncate_length=48)
            return {"type": type_name, "message": d}
        finally:
            self.destroy_subscription(sub)


def main():
    rclpy.init()
    node = Probe()
    from rclpy.executors import MultiThreadedExecutor
    ex = MultiThreadedExecutor(num_threads=2)
    ex.add_node(node)
    threading.Thread(target=ex.spin, daemon=True).start()
    send({"id": 0, "ok": True, "result": "ready"})

    for line in sys.stdin:
        try:
            req = json.loads(line)
        except ValueError:
            continue
        rid, cmd, args = req.get("id"), req.get("cmd"), req.get("args") or {}

        def handle(rid=rid, cmd=cmd, args=args):
            try:
                node.gc()
                if cmd == "graph":
                    res = node.graph()
                elif cmd == "set_watch":
                    res = {"missing": node.set_watch(args.get("topics", [])), "rates": node.rates()}
                elif cmd == "rates":
                    res = node.rates(args.get("topics"))
                elif cmd == "measure":
                    topics = args.get("topics", [])
                    missing = node.watch(topics, ttl=float(args.get("duration", 3)) + 5)
                    time.sleep(float(args.get("duration", 3)))
                    res = {"missing": missing, "rates": node.rates(topics)}
                elif cmd == "echo":
                    res = node.echo(args["topic"], float(args.get("timeout", 3)))
                elif cmd == "ping":
                    res = "pong"
                else:
                    raise ValueError(f"unknown command {cmd}")
                send({"id": rid, "ok": True, "result": res})
            except Exception as e:
                send({"id": rid, "ok": False, "error": f"{type(e).__name__}: {e}"})

        # slow commands (measure/echo) must not block the others
        threading.Thread(target=handle, daemon=True).start()

    # stdin closed: the panel stopped us. Exit hard; a graceful rclpy shutdown
    # with executor threads still running can abort() inside the C++ layer.
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
