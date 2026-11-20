# L50: native code in the car backend: measured, nothing taken

Lane L50 (2026-11-20; owner's ruling: compiled code wherever it makes a tangible difference in execution
time, across the system). Branch `l50-native` from `l48-opt` c7a8b81. The perception side of this lane is in the
roadsense repository (`docs/lanes/L50-native.md` there: chris-integrator got a native core). This note is the
backend's part: what its hot loops are after L48's pacing fix, what compiled code could take out of each, and
why none of it was done. No code of the backend was changed.

## What is hot

L48-B measured the backend per thread on the phone (`docs/lanes/L48-backend-cost.md`, bench, 59 % of the one
little core it owns): the CAN reader 38 %, the engine loop 9.3 %, the roadsense scene feed 5.2 %, everything
else under 2 % each. The reader's 38 % was an empty-read spin, not work; `520d816` paces it. What is left of
the reader on the bench is a few percent, and in the car it is the frame path at about 1,690 frames a second.

| loop | cost (L48-B, phone) | what the time is | what compiled code could take | verdict |
|---|---|---|---|---|
| CAN reader, unpacking a bulk transfer (`io/panda_can.py` `unpack_can_buffer_v2`) | 0.62 us a frame on the laptop (measured here, 64 frames a transfer), about 4 us on the phone: 0.7 % of the core at 1,690 frames/s | byte slicing in Python | all of it | not tangible |
| CAN reader, the three filters (`io/ingress.py` `ingest_broadcast_can`: unknown id, repeated payload, dispatch cap) | a few dict operations a frame; 85 % of the frames end here | already the cheap path | a part of a few percent | not tangible |
| CAN reader, a changed frame: trip-log entry (dict, `RawMessage`, JSON line), about 800 a second in the car | 10-20 % of the core while driving (L48-B's estimate from the laptop profile) | object building and one JSON line per frame | little: the cost is the per-frame entry itself. One entry per transfer with compact rows (L48-B candidate 2) removes most of it in Python; it changes the trip file format and waits for the owner | a format decision, not a language one |
| CAN reader, store dispatch of the hot ids, about 110 a second | about 0.6 ms each, 7 % of the core | reducers and the rules engine: the backend's logic | nothing short of rewriting the state layer | no |
| engine loop, 50 Hz, 25 tick functions | 9.3 % | mostly the price of asking 25 functions whether it is time | nothing; a slower or event-driven loop is L48-B candidate 6 | no |
| roadsense scene feed | 5.2 % | `json.loads` of `lead`, `lanes`, `view` for every frame (45 a second) and the scene build (10 a second) | a faster parser would take perhaps a third of it (2 % of the core); decoding only the newest message of each topic when a scene is built would take as much without any compiled code | not tangible either way; noted |

The scene feed's parser is where chris-integrator's native JSON reader would apply (2.5 times `json.loads` on
its messages). It lives in another repository's package; importing it into the backend would couple the two
for 2 % of a core. Not done.

## Verdict

After the pacing fix nothing in the backend is a kernel: its remaining cost is Python object traffic spread
over the frame path, the state layer and the engine loop, and the large item on the frame path (the per-frame
trip log) is cut by an order of magnitude with a format change, not with a compiler. Compiled code here would
buy a few percent of one little core at the price of a build chain for the backend (which has none) on a
pinned phone.

## Not verified

The phone numbers above are L48-B's (bench, before its fix was deployed) and its estimates for the car. The
backend after the pacing fix was not re-measured in this lane: see the roadsense report for the state of the
phone at the lane's end. The car's frame path can only be measured in the car.
