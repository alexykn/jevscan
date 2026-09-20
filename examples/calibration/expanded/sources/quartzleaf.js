function formatLabel(value) {
  return value.trim().toLowerCase().replace(/\s+/g, "-");
}

function formatTitle(value) {
  return formatLabel(value);
}

export function makePath(value) {
  return `/labels/${formatTitle(value)}`;
}
