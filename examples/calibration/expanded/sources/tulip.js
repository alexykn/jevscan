export async function loadPalette(source) {
  try {
    const palette = await source.decode();
    if (palette.colors.length === 0) {
      throw new Error("empty palette");
    }
    return palette;
  } catch (error) {
    return { colors: [], reason: error.message };
  }
}
