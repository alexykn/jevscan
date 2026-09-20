type Profile = { id: string; display: string; region: string };

export async function hydrateProfile(
  response: { profile: Profile; permissions: string[] },
  database: { begin: () => Transaction },
  renderer: {
    title: (display: string) => string;
    encode: (value: unknown) => string;
    failure: (id: string, error: unknown) => string;
  },
) {
  const { profile: { id, display, region }, permissions } = response;
  const transaction = database.begin();
  let markup = "";
  try {
    const title = renderer.title(display);
    transaction.stage({ id, permissions });
    markup = renderer.encode({ title, region, permissions });
    if (!permissions.includes("publish")) {
      transaction.rollback();
      return markup;
    }
    transaction.appendAudit({ id, permissionCount: permissions.length });
    transaction.commit();
    return markup;
  } catch (error) {
    transaction.rollback();
    return renderer.failure(id, error);
  }
}
