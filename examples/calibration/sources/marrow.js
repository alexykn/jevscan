export function decode_session(text, hydrate) {
  const payload = JSON.parse(text);
  if (!payload.userId) {
    throw new Error("user id missing");
  }
  if (!Array.isArray(payload.permissions) || payload.permissions.length === 0) {
    throw new Error("permissions missing");
  }
  try {
    return hydrate(payload);
  } catch (error) {
    return { userId: payload.userId, permissions: [] };
  }
}
