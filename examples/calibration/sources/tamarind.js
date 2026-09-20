const sessions = new Map();

export class SessionStore {
  start(id, value) {
    sessions.set(id, value);
  }

  stop(id) {
    sessions.delete(id);
  }
}

export function expire(id) {
  sessions.delete(id);
}
