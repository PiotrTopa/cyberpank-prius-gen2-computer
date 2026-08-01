import zmq
import json

ctx = zmq.Context()
sock = ctx.socket(zmq.SUB)
sock.connect("tcp://127.0.0.1:8081")
sock.setsockopt(zmq.SUBSCRIBE, b"")
print("Connected, waiting for frames...")
while True:
    msg = sock.recv_string()
    print("Received:", len(msg), "bytes")
