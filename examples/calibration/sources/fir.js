export function load_manifest(bytes, decode, parse) {
  const text = decode(bytes);
  const document = parse(text);
  if (!document.version) {
    throw new Error("version missing");
  }
  const header = bytes.slice(0, 8);
  const length = header[0] * 256 + header[1];
  return { version: document.version, length };
}
