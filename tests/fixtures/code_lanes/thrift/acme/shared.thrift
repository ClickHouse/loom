namespace py acme.shared

// Types every acme service shares
typedef i64 Timestamp

struct Page {
  1: required i32 offset,
  2: required i32 limit
}

exception NotFound {
  1: string message
}

service BaseService {
  string version()
}
