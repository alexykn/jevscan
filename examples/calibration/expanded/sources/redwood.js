const sessions = new Map();

export class SessionRegistry {
  open(id, value) {
    sessions.set(id, { value, state: "open" });
  }

  close(id) {
    sessions.set(id, { value: sessions.get(id).value, state: "closed" });
  }
}

export function expire(id) {
  sessions.delete(id);
}
