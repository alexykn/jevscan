export class Lifecycle {
  start(config: Config) {
    this.openResources(config);
    this.configureRuntime(config);
    return this.running;
  }

  stop() {
    this.closeResources();
    this.running = false;
  }

  openResources(config: Config) {
    this.resources = config.resources.map((name) => this.open(name));
  }

  configureRuntime(config: Config) {
    this.runtime = { mode: config.mode, timeout: config.timeout };
  }

  closeResources() {
    this.resources?.forEach((resource) => resource.close());
  }

  open(name: string) {
    return { name, close() {} };
  }
}
