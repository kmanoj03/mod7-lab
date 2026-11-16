# Distributed order processing

A small Python simulation of an order moving through versioned states:

`version 1 CREATED -> version 2 PAID -> version 3 SHIPPED`

Events can arrive late, out of order, or more than once. Two workers can also race to update the same order. The processor applies versions in order, remembers a gap when an earlier version never arrives, and rejects a worker whose fencing token is older than the one already accepted.

No third-party packages. Python 3.10 or newer is enough.

## How to run

From the repository root:

```bash
git clone https://github.com/kmanoj03/mod7-lab.git
cd mod7-lab
python3 simulate.py
python3 -m unittest discover -s tests -t .
```

`python3 simulate.py` prints the three scenarios and writes the same log to `evidence/demo-output.txt`. The unit tests live in `tests/test_order_processor.py`. Their verbose output is saved in `evidence/unittest-output.txt`.

| File | Role |
| --- | --- |
| `order_processor.py` | Version buffer, duplicate suppression, fencing tokens |
| `simulate.py` | The three required scenarios |
| `tests/test_order_processor.py` | Checks for ordering, gaps, duplicates, and stale workers |
| `evidence/demo-output.txt` | Saved output of `python3 simulate.py` |
| `evidence/unittest-output.txt` | Saved output of the unit tests |

Each update carries `order_id`, `version`, and `status`. A version is applied only when it is the next one. A higher version is stored as pending, and the versions between the applied prefix and that pending version are recorded as missing. A version that was already applied, or already sitting in the buffer, is ignored.

Fencing tokens come from one issuer and only increase. The order stores the highest token it has accepted. A later worker holds the greater token and may write. A worker that presents a smaller token is rejected before its version is considered.

## Example of correct event ordering

Arrival order for `ord-100` is version 2, then version 1, then version 2 again. Version 2 is held with version 1 missing. When version 1 arrives, the processor applies CREATED and then PAID.

```text
>> deliver order_id=ord-100 version=2 status=PAID
   buffered version 2; waiting for version 1
   order_id=ord-100 version=0 status=(none) pending=2 missing=1 fencing_token=0

>> deliver order_id=ord-100 version=1 status=CREATED
   applied in order: version 1 CREATED, version 2 PAID
   order_id=ord-100 version=2 status=PAID pending=(none) missing=(none) fencing_token=0
```

```text
check: version 2 stayed pending until version 1 arrived, and the order stayed at version 0
check: version 1 CREATED and version 2 PAID were applied in that order
```

## Evidence of duplicate handling

The same version 2 is delivered a second time after it has already been applied. The order stays at version 2, status PAID.

```text
>> deliver order_id=ord-100 version=2 status=PAID (again)
   ignored duplicate version 2; already applied through version 2
   order_id=ord-100 version=2 status=PAID pending=(none) missing=(none) fencing_token=0

check: the second copy of version 2 was ignored and the order stayed at PAID
```

A duplicate of a version that is still buffered is also stored once. `test_duplicate_of_a_buffered_version_is_stored_once` covers that case.

## Evidence of the missing-event case

`ord-missing` receives only version 3 (SHIPPED). Versions 1 and 2 are never sent. The order stays at version 0, version 3 is pending, and versions 1 and 2 are recorded as missing.

```text
>> deliver order_id=ord-missing version=3 status=SHIPPED
   buffered version 3; waiting for versions 1, 2
   order_id=ord-missing version=0 status=(none) pending=3 missing=1, 2 fencing_token=0

check: the order stayed at version 0 with no status, so it did not jump to SHIPPED
check: version 3 is recorded as pending and versions 1 and 2 are recorded as missing
```

## Evidence of stale-worker rejection

`worker-a` is issued fencing token 1. `worker-b` is issued fencing token 2. Worker A creates the order. Worker B, holding the newer token, advances it to PAID. Worker A then tries to mark the order SHIPPED with token 1 and is rejected. The order remains version 2, status PAID, fencing token 2.

```text
issued fencing token 1 to worker-a
issued fencing token 2 to worker-b
worker-b holds the newer token (2 > 1)

>> worker-a token=1 deliver order_id=ord-fence version=1 status=CREATED
   applied in order: version 1 CREATED
   order_id=ord-fence version=1 status=CREATED pending=(none) missing=(none) fencing_token=1

>> worker-b token=2 deliver order_id=ord-fence version=2 status=PAID
   applied in order: version 2 PAID
   order_id=ord-fence version=2 status=PAID pending=(none) missing=(none) fencing_token=2

>> worker-a token=1 deliver order_id=ord-fence version=3 status=SHIPPED
   rejected stale worker: token 1 is older than 2
   order_id=ord-fence version=2 status=PAID pending=(none) missing=(none) fencing_token=2

check: worker-a updated the order while token 1 was the newest token
check: worker-b, holding the newer token, advanced the order to PAID
check: worker-a, holding the older token, was rejected and the order stayed at PAID
```

## Full terminal output

`python3 simulate.py` on this machine:

```text
Distributed order processing
Events may arrive late, out of order, or more than once.
Pipeline: version 1 CREATED -> version 2 PAID -> version 3 SHIPPED

================================================================
1. Versioned event ordering
Arrival order: version 2, version 1, version 2 again
================================================================

>> deliver order_id=ord-100 version=2 status=PAID
   buffered version 2; waiting for version 1
   order_id=ord-100 version=0 status=(none) pending=2 missing=1 fencing_token=0

>> deliver order_id=ord-100 version=1 status=CREATED
   applied in order: version 1 CREATED, version 2 PAID
   order_id=ord-100 version=2 status=PAID pending=(none) missing=(none) fencing_token=0

>> deliver order_id=ord-100 version=2 status=PAID (again)
   ignored duplicate version 2; already applied through version 2
   order_id=ord-100 version=2 status=PAID pending=(none) missing=(none) fencing_token=0

check: version 2 stayed pending until version 1 arrived, and the order stayed at version 0
check: version 1 CREATED and version 2 PAID were applied in that order
check: the second copy of version 2 was ignored and the order stayed at PAID

================================================================
2. Missing event test
Sent only version 3. Versions 1 and 2 were never sent.
================================================================

>> deliver order_id=ord-missing version=3 status=SHIPPED
   buffered version 3; waiting for versions 1, 2
   order_id=ord-missing version=0 status=(none) pending=3 missing=1, 2 fencing_token=0

check: the order stayed at version 0 with no status, so it did not jump to SHIPPED
check: version 3 is recorded as pending and versions 1 and 2 are recorded as missing

================================================================
3. Stale worker test
Two workers update ord-fence. The later worker receives the higher token.
================================================================

issued fencing token 1 to worker-a
issued fencing token 2 to worker-b
worker-b holds the newer token (2 > 1)

>> worker-a token=1 deliver order_id=ord-fence version=1 status=CREATED
   applied in order: version 1 CREATED
   order_id=ord-fence version=1 status=CREATED pending=(none) missing=(none) fencing_token=1

>> worker-b token=2 deliver order_id=ord-fence version=2 status=PAID
   applied in order: version 2 PAID
   order_id=ord-fence version=2 status=PAID pending=(none) missing=(none) fencing_token=2

>> worker-a token=1 deliver order_id=ord-fence version=3 status=SHIPPED
   rejected stale worker: token 1 is older than 2
   order_id=ord-fence version=2 status=PAID pending=(none) missing=(none) fencing_token=2

check: worker-a updated the order while token 1 was the newest token
check: worker-b, holding the newer token, advanced the order to PAID
check: worker-a, holding the older token, was rejected and the order stayed at PAID

All scenario checks passed.
```

The same text is in `evidence/demo-output.txt`. After the log, the program prints `saved evidence/demo-output.txt` to stderr.

Unit tests, also saved in `evidence/unittest-output.txt`:

```text
Ran 12 tests in 0.001s

OK
```

## Reflection

Clocks on different machines drift, and a time sync can step a clock forward or backward, so the timestamp on an update is not a reliable record of when that update happened. Two machines can also stamp different updates with the same time, which leaves no single order between them. The network can hold a message and deliver it after a newer one, so the time an update arrives is yet another clock, unrelated to the order the updates were produced. A version number gives each order one sequence: version 2 stays pending until version 1 arrives, and the updates are then applied as CREATED and then PAID. A fencing token works the same way for workers: one issuer hands out the next number, the order keeps the highest number it has accepted, and a worker still holding an older number cannot overwrite the worker that replaced it, no matter what timestamp its own clock would attach to the write.
