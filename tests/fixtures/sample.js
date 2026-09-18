export function standalone(value) { return value + 1; }
export const increment = (value) => value + 1;
export const normalize = function(value) { return Math.max(0, value); };
export class Service {
    constructor(value) { this.value = value; }
    static create(value) { return new Service(value); }
    async load() { return this.value; }
    transform = (value) => value + 1;
}
const widget = { render() { return 'widget'; } };
