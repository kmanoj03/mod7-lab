# Distributed order processing

## Instructions for running the program

Python 3.10 or newer. No packages to install. From the repository root:

```bash
git clone https://github.com/kmanoj03/mod7-lab.git
cd mod7-lab
python3 simulate.py
```

## Example of correct event ordering

Arrival order is version 2, then version 1, then version 2 again. Version 2 waits, and the two updates are applied as CREATED then PAID.

```text
>> deliver order_id=ord-100 version=2 status=PAID
   buffered version 2; waiting for version 1
   order_id=ord-100 version=0 status=(none) pending=2 missing=1 fencing_token=0

>> deliver order_id=ord-100 version=1 status=CREATED
   applied in order: version 1 CREATED, version 2 PAID
   order_id=ord-100 version=2 status=PAID pending=(none) missing=(none) fencing_token=0

check: version 2 stayed pending until version 1 arrived, and the order stayed at version 0
check: version 1 CREATED and version 2 PAID were applied in that order
```

## Evidence of duplicate handling

The second delivery of version 2 is ignored. The order stays at version 2, status PAID.

```text
>> deliver order_id=ord-100 version=2 status=PAID (again)
   ignored duplicate version 2; already applied through version 2
   order_id=ord-100 version=2 status=PAID pending=(none) missing=(none) fencing_token=0

check: the second copy of version 2 was ignored and the order stayed at PAID
```

## Evidence of the missing-event case

Only version 3 (SHIPPED) is sent. The order stays at version 0. Version 3 is pending, and versions 1 and 2 are missing.

```text
>> deliver order_id=ord-missing version=3 status=SHIPPED
   buffered version 3; waiting for versions 1, 2
   order_id=ord-missing version=0 status=(none) pending=3 missing=1, 2 fencing_token=0

check: the order stayed at version 0 with no status, so it did not jump to SHIPPED
check: version 3 is recorded as pending and versions 1 and 2 are recorded as missing
```

## Evidence of stale-worker rejection

worker-a receives fencing token 1 and worker-b receives fencing token 2. The newer token advances the order to PAID. The older token is rejected, and the order stays at PAID.

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

## Terminal output

`python3 simulate.py`:

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

## Reflection

Clocks on different machines drift, and a time sync can step a clock forward or backward, so the timestamp on an update is not a reliable record of when that update happened. Two machines can also stamp different updates with the same time, which leaves no single order between them. The network can hold a message and deliver it after a newer one, so the time an update arrives is yet another clock, unrelated to the order the updates were produced. A version number gives each order one sequence: version 2 stays pending until version 1 arrives, and the updates are then applied as CREATED and then PAID. A fencing token works the same way for workers: one issuer hands out the next number, the order keeps the highest number it has accepted, and a worker still holding an older number cannot overwrite the worker that replaced it, no matter what timestamp its own clock would attach to the write.
