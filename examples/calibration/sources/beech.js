function normalizeAddress(value) {
  return {
    street: value.street.trim().toLowerCase(),
    city: value.city.trim().toLowerCase(),
    postalCode: value.postalCode.trim(),
  };
}

function prepareAddress(value) {
  return {
    street: value.street.trim().toLowerCase(),
    city: value.city.trim().toLowerCase(),
    postalCode: value.postalCode.trim(),
  };
}

export function ship(address) {
  return prepareAddress(address);
}
