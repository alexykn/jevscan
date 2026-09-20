export class SessionRegistry {
  constructor() {
    this.sessions = new Map();
  }

  open(id, value) {
    this.sessions.set(id, { value, state: "open" });
  }

  close(id) {
    this.sessions.delete(id);
  }

  expire(id) {
    this.sessions.delete(id);
  }
}
