export class Screen {
  advance(input: { phase: string; online: boolean; dirty: boolean }) {
    if (input.phase === "opening") {
      if (input.online) {
        if (input.dirty) {
          this.flush();
          return "ready";
        }
        return "ready";
      }
      return "waiting";
    }
    switch (input.phase) {
      case "ready":
        return input.online ? "ready" : "waiting";
      case "closing":
        return input.dirty ? "saving" : "closed";
      default:
        return "reset";
    }
  }

  flush() {
    return true;
  }
}
