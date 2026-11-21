namespace py acme.machines
namespace java com.acme.machines

include "shared.thrift"

typedef string MachineId

enum State {
  PENDING = 1,
  RUNNING = 2,
  STOPPED = 3
}

/* A machine as the API returns it */
struct Machine {
  1: required MachineId id,
  2: optional State state = State.PENDING,
  3: shared.Timestamp created_at,
  4: map<string, string> labels
}

union Selector {
  1: MachineId id,
  2: list<string> label_keys
}

exception InvalidState {
  1: State current,
  2: string reason
}

service Machines extends shared.BaseService {
  Machine get(1: MachineId id) throws (1: shared.NotFound not_found),
  list<Machine> list_machines(1: shared.Page page, 2: Selector selector),
  void stop(1: MachineId id) throws (1: shared.NotFound not_found, 2: InvalidState invalid),
  oneway void touch(1: MachineId id)
}
