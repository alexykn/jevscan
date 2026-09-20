type Profile = { id: string; display: string; region: string };

function persistProfile(
  response: { profile: Profile; permissions: string[] },
  database: { begin: () => Transaction },
) {
  const transaction = database.begin();
  try {
    transaction.stage(response);
    transaction.commit();
    return response.profile;
  } catch (error) {
    transaction.rollback();
    throw error;
  }
}

function encodeProfile(
  profile: Profile,
  renderer: {
    title: (display: string) => string;
    encode: (value: unknown) => string;
  },
) {
  const title = renderer.title(profile.display);
  return renderer.encode({ title, region: profile.region });
}

export async function hydrateProfile(
  response: { profile: Profile; permissions: string[] },
  database: { begin: () => Transaction },
  renderer: {
    title: (display: string) => string;
    encode: (value: unknown) => string;
  },
) {
  const profile = await persistProfile(response, database);
  return encodeProfile(profile, renderer);
}
