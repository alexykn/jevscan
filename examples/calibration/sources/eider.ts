export function loadManifest(bytes: Uint8Array, decode: (value: Uint8Array) => string, parse: (value: string) => { version: string }) {
  return parse(decode(bytes)).version;
}
