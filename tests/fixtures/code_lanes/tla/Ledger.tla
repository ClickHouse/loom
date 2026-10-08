------------------------------ MODULE Ledger --------------------------------
(* A toy replicated ledger: records are appended, quorum-stored, then      *)
(* durable. The invariant AckedDurable says nothing acked can be lost.     *)
EXTENDS Integers, FiniteSets

CONSTANTS MaxLsn, Quorum

Lsns == 1..MaxLsn

VARIABLES
  log,       \* [Lsns -> record]
  durable    \* the durable-LSN watermark

vars == <<log, durable>>

Free == [st |-> "free", acked |-> FALSE]

\* True when record l is stored on a quorum.
Stored(l) == log[l].st = "quorum"

Init ==
  /\ log = [l \in Lsns |-> Free]
  /\ durable = 0

Append(l) ==
  /\ log[l].st = "free"
  /\ log' = [log EXCEPT ![l].st = "pend"]
  /\ UNCHANGED durable

Store(l) ==
  /\ log[l].st = "pend"
  /\ log' = [log EXCEPT ![l].st = "quorum"]
  /\ UNCHANGED durable

Advance ==
  /\ durable < MaxLsn
  /\ Stored(durable + 1)
  /\ durable' = durable + 1
  /\ UNCHANGED log

Next == \E l \in Lsns : Append(l) \/ Store(l) \/ Advance

Spec == Init /\ [][Next]_vars

TypeOK == durable \in 0..MaxLsn

AckedDurable == \A l \in Lsns : log[l].acked => l <= durable
===============================================================================
