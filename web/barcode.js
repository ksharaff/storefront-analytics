// Code 128 barcodes as SVG, for the storage app's TEST labels (2026-09-29).
//
// Code 128 is what USB/Bluetooth scanners read out of the box, and it holds
// letters and digits, which is all a label code uses. Written here (about
// 40 lines) rather than adding a library for one small job.
//
// How Code 128 works: every character is one symbol of 3 bars and 3 spaces,
// 11 "modules" wide. The digits in PATTERNS are the widths of bar, space,
// bar, space, bar, space. A barcode is:
//   quiet zone | Start B | the characters | check symbol | Stop | quiet zone
// Code set B covers the printable ASCII characters: value = char code - 32.

const PATTERNS = [
  "212222", "222122", "222221", "121223", "121322", "131222", "122213", "122312", "132212", "221213",
  "221312", "231212", "112232", "122132", "122231", "113222", "123122", "123221", "223211", "221132",
  "221231", "213212", "223112", "312131", "311222", "321122", "321221", "312212", "322112", "322211",
  "212123", "212321", "232121", "111323", "131123", "131321", "112313", "132113", "132311", "211313",
  "231113", "231311", "112133", "112331", "132131", "113123", "113321", "133121", "313121", "211331",
  "231131", "213113", "213311", "213131", "311123", "311321", "331121", "312113", "312311", "332111",
  "314111", "221411", "431111", "111224", "111422", "121124", "121421", "141122", "141221", "112214",
  "112412", "122114", "122411", "142112", "142211", "241211", "221114", "413111", "241112", "134111",
  "111242", "121142", "121241", "114212", "124112", "124211", "411212", "421112", "421211", "212141",
  "214121", "412121", "111143", "111341", "131141", "114113", "114311", "411113", "411311", "113141",
  "114131", "311141", "411131", "211412", "211214", "211232", "2331112",
];
const START_B = 104, STOP = 106, QUIET = 10;   // quiet zone: 10 modules each side

/** The bar/space widths (in modules) for `text`, quiet zones included. */
export function code128Widths(text) {
  const values = [...text].map((ch) => {
    const v = ch.charCodeAt(0) - 32;
    if (v < 0 || v > 95) throw new Error(`Code 128 B cannot encode ${JSON.stringify(ch)}`);
    return v;
  });
  // Check symbol: start value + each value times its position, modulo 103.
  const check = values.reduce((sum, v, i) => sum + v * (i + 1), START_B) % 103;
  const symbols = [START_B, ...values, check, STOP];
  return [QUIET, ...symbols.flatMap((s) => [...PATTERNS[s]].map(Number)), QUIET];
}

/** An <svg> string of the barcode, black bars on white. Only rectangles and
 *  numbers go into it, never the text itself, so it is safe as innerHTML.
 *  It scales to its container (viewBox); `height` is in modules. */
export function code128Svg(text, height = 50) {
  const widths = code128Widths(text);
  let x = 0, rects = "";
  widths.forEach((w, i) => {
    // Odd positions are bars: index 0 is the quiet zone (a space), then
    // bar, space, bar, ... alternating to the closing quiet zone.
    if (i % 2 === 1) rects += `<rect x="${x}" y="0" width="${w}" height="${height}"/>`;
    x += w;
  });
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${x} ${height}" `
    + `preserveAspectRatio="none" shape-rendering="crispEdges" role="img">`
    + `<rect width="${x}" height="${height}" fill="#fff"/><g fill="#000">${rects}</g></svg>`;
}
