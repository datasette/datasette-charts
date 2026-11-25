import { TIP, basePlot, mount, fmt, isThumbnail } from "./common.js";
import { annotate } from "./annotations.js";

// How to draw the map, and where a basemap comes from, is an open question
// in the spec. This first version draws points over a coarse world land
// outline (Natural Earth 1:110m), which needs no network access and no tiles.

let landPromise = null;

function loadLand(ctx) {
  if (!landPromise) {
    landPromise = Promise.all([
      ctx.loadScript(ctx.urls.topojson),
      fetch(ctx.urls.land).then((response) => response.json()),
    ]).then(([, topology]) => window.topojson.feature(topology, topology.objects.land));
  }
  return landPromise;
}

// rows: lat, lon (and size) in "points" mode; lat, lon, n (and size) in
// "grid" mode, where each row is a grid cell meta.cell degrees across.
export async function render(el, ctx) {
  const { Plot, data, meta, theme } = ctx;
  const thumbnail = isThumbnail(ctx);
  let land = null;
  try {
    land = await loadLand(ctx);
  } catch (e) {
    landPromise = null; // draw the points without a basemap
  }

  const lats = data.map((d) => d.lat);
  const lons = data.map((d) => d.lon);
  let south = Math.min(...lats);
  let north = Math.max(...lats);
  let west = Math.min(...lons);
  let east = Math.max(...lons);
  const world = !data.length || east - west > 180 || north - south > 100;

  let projection = "equal-earth";
  if (!world) {
    // Zoom to the data, with some padding and a sensible minimum extent
    const padLat = Math.max(0.5, (north - south) * 0.08);
    const padLon = Math.max(0.5, (east - west) * 0.08);
    south = Math.max(-85, south - padLat);
    north = Math.min(85, north + padLat);
    west = Math.max(-180, west - padLon);
    east = Math.min(180, east + padLon);
    projection = {
      type: "mercator",
      domain: {
        type: "MultiPoint",
        coordinates: [
          [west, south],
          [east, north],
        ],
      },
      inset: thumbnail ? 2 : 8,
    };
  }

  const grid = meta.mode === "grid";
  const sized = data.length && data[0].size !== undefined;
  const radius = sized ? "size" : grid ? "n" : thumbnail ? 1.3 : 2.5;
  const where = (d) => `${d.lat.toFixed(3)}, ${d.lon.toFixed(3)}`;
  const title = (d) => {
    const lines = [];
    if (grid) lines.push(`${fmt(d.n)} ${d.n === 1 ? "row" : "rows"} near ${where(d)}`);
    else lines.push(where(d));
    if (sized) lines.push(`${meta.size}: ${fmt(d.size)}`);
    return lines.join("\n");
  };

  const marks = [];
  if (world) marks.push(Plot.sphere({ stroke: theme.grid }));
  if (land) marks.push(Plot.geo(land, { fill: theme.land }));
  marks.push(
    Plot.dot(data, {
      x: "lon",
      y: "lat",
      r: radius,
      fill: theme.series[0],
      fillOpacity: 0.6,
      stroke: theme.surface,
      strokeWidth: thumbnail ? 0 : 0.4,
      ...(thumbnail ? {} : { title, tip: TIP }),
    })
  );

  const options = basePlot(ctx, {
    projection,
    r: { range: thumbnail ? [0.5, 3.5] : [1.2, 9] },
    marks,
  });
  if (!thumbnail) delete options.height; // Plot picks one to suit the projection
  const plot = Plot.plot(options);
  // No axes: only free-floating text notes
  annotate(el, ctx, plot, {});
  mount(el, ctx, plot);
}
