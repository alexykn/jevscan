function formatLabel(value) {
  return value.trim().toLowerCase().replace(/\s+/g, "-");
}

function formatTitle(value) {
  return value.trim().toLowerCase().replace(/\s+/g, "-");
}

export function makePath(value) {
  return `/labels/${formatLabel(value)}`;
}
