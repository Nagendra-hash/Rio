---
name: geomaster
description: Supports geospatial research workflows for remote sensing, vector and raster GIS, spatial statistics, terrain and network analysis, and machine learning for Earth observation. Use when processing satellite imagery, aligning coordinate systems and raster grids, accessing STAC catalogs, analyzing geospatial time series, or implementing scientific GIS workflows in Python, R, Julia, JavaScript, C++, Java, Go, or Rust.
license: MIT License
compatibility: Core examples require Python 3.12+ and GeoPandas, Rasterio, NumPy and PyProj. Optional workflows require their named packages, native GIS applications, GPU runtimes, or service credentials and network access.
metadata: 
  version: 1.5
  skill-author: K-Dense Inc.
  last-reviewed: 2026-10-01
---

# GeoMaster

Geospatial analysis across vector/raster GIS, remote sensing, spatial ML, terrain,
networks, and scientific applications. Start with the relevant workflow, inspect
input provenance, and load only the reference needed for the task.

## Tested scope and installation

The local recipe suite targets GeoPandas 1.2.0, Rasterio 1.5.2, Shapely 2.1.2,
PyProj 3.8.0, Rioxarray 0.23.0, Xarray 2026.9.0, OSMnx 2.1.1 and
PySTAC Client 0.9.0. See [review and source ledger](references/review.md) for
execution limits. Examples requiring actual input files are templates; authenticated
Earth Engine/CDS/commercial services, desktop GIS and GPU training remain illustrative.

```bash
# In a dedicated environment; install only the workflow's optional dependencies.
uv venv --python 3.13
uv pip install geopandas==1.2.0 rasterio==1.5.2 shapely==2.1.2 pyproj==3.8.0
uv pip install rioxarray==0.23.0 xarray==2026.9.0 'dask[array]' scikit-learn==1.9.1
uv pip install pystac-client==0.9.0 planetary-computer==1.0.0 odc-stac
# For osgeo/native CLI or PDAL, use a separate conda-forge environment:
# conda create -n geo-native -c conda-forge python=3.13 gdal pdal python-pdal
```

Install TorchGeo/PyTorch, RSGISLib, Py6S/6S, ArcPy, QGIS or other specialist
runtimes separately when required. A Rasterio wheel includes its own GDAL library;
it does not install `osgeo` or the GDAL command-line programs.

## Workflow

1. Record product/collection ID, acquisition time, processing baseline, license,
   band names, scale/offset, QA meaning, horizontal/vertical CRS and units.
2. Verify CRS from authoritative metadata. `set_crs` labels coordinates;
   `to_crs` transforms them. Never infer an unknown CRS from plausible bounds.
3. Align extent, affine transform, dimensions, pixel registration and resolution.
   Reproject categorical masks with nearest-neighbor resampling; choose an appropriate
   resampler for continuous data. Matching array shape alone does not prove alignment.
4. Preserve nodata, clouds, shadows and saturation masks. Convert unsigned integers
   to floating point before differences; apply the provider's radiometric transform once.
5. Run the analysis at a defensible support/resolution. Keep training labels and
   validation blocks separate. Terrain elevation and horizontal units must agree.
6. Export CRS, transform, valid-data mask, units, model settings and provenance.
   Check numeric expectations on a small known fixture before scaling up.

## Local raster recipes

Import the bundled [raster helper](scripts/raster_workflows.py) from its directory
(add that directory to `PYTHONPATH` or run from it). It implements small in-memory
recipes; use windows/Dask for larger data. Writers require a new output path. It does not infer band identities or masks.

### NDVI

```python
from raster_workflows import write_ndvi

# This example assumes a VERIFIED four-band B02/B03/B04/B08 stack whose mask
# already excludes clouds/shadows, and harmonized DN reflectance = DN * 0.0001.
write_ndvi('s2_masked_stack.tif', 'ndvi.tif', red_band=3, nir_band=4,
           scale=0.0001, offset=0.0)
```

Do not use those indices or calibration for an arbitrary `sentinel2.tif`.
SAFE products and STAC assets are often separate single-band rasters. Additive
radiometric offsets affect even NDVI. EVI/SAVI require physical reflectance.
`normalized_difference` keeps undefined ratios and invalid cells as NaN, not zero.

### Terrain

```python
import rasterio
from raster_workflows import terrain_metrics

with rasterio.open('dem_metres.tif') as src:
    slope_deg, aspect_deg, shade = terrain_metrics(
        src.read(1, masked=True), src.transform, src.crs)
```

The helper requires a north-up projected metric grid and elevations in metres.
Aspect points downslope clockwise from north; it is undefined for flat cells.
Slope is resolution-aware; nodata contaminating a derivative stencil stays invalid.
Hillshade is an illumination visualization, not hydrological flow or exposure risk.

### Classification

```python
import geopandas as gpd
from raster_workflows import classify_imagery

training = gpd.read_file('training.gpkg')  # polygons with class_id in 1..65534
model = classify_imagery('masked_features.tif', training, 'classified.tif')
```

The helper checks CRS, geometry, labels, valid training pixels and overlapping
labels, then preserves nodata in a uint16 output. It fits a small demonstration
model; it does not measure accuracy. Use spatial/temporal holdouts at the field,
scene or regional level before reporting predictive performance. See
[machine learning](references/machine-learning.md).

## Vector analysis

```python
import geopandas as gpd

zones = gpd.read_file('zones.geojson')
points = gpd.read_file('points.geojson')
if zones.crs is None or points.crs is None:
    raise ValueError('Resolve missing CRS before analysis')
points = points.to_crs(zones.crs)
joined = gpd.sjoin(points, zones, how='inner', predicate='within')
stats = joined.groupby('zone_id')['value'].agg(['count', 'mean', 'std'])

# Local data only: verify the estimated CRS area of use before accepting it.
metric = zones.to_crs(zones.estimate_utm_crs())
metric['area_m2'] = metric.area
buffers = metric.geometry.buffer(1000).to_crs(zones.crs)
```

`within` excludes boundary points; overlapping zones can duplicate observations.
Choose and document a boundary/overlap policy. A projected CRS need not use metres
or preserve area. UTM suits local regions, not every national, polar or global task.

## Cloud catalogs and Earth Engine

[Data sources](references/data-sources.md) documents current STAC, CDSE, CDS